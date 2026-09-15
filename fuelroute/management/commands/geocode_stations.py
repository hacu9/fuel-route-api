"""Give every imported station a latitude and a longitude.

The price file names a city and a state but carries no coordinates, and the
route matcher cannot use a station it cannot place. This command resolves the
whole table against the offline Census gazetteer, which costs no network calls
and finishes in seconds.

A handful of sites sit in unincorporated places the gazetteer does not list.
Pass ``--use-nominatim`` to send only those stragglers to the public geocoder,
one request per second as its usage policy requires.
"""

from __future__ import annotations

import time

import requests
from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import transaction

from fuelroute.models import FuelStation
from fuelroute.services import catalog
from fuelroute.services.gazetteer import is_available, lookup_city

NOMINATIM_DELAY_SECONDS = 1.0


class Command(BaseCommand):
    help = "Resolve coordinates for imported fuel stations."

    def add_arguments(self, parser):
        parser.add_argument(
            "--use-nominatim",
            action="store_true",
            help="Send gazetteer misses to Nominatim at one request per second.",
        )
        parser.add_argument(
            "--limit", type=int, default=0, help="Cap the Nominatim lookups."
        )
        parser.add_argument(
            "--refresh",
            action="store_true",
            help="Re-resolve stations that already have coordinates.",
        )

    def handle(self, *args, **options):
        if not is_available():
            self.stderr.write(
                self.style.ERROR(
                    "The gazetteer files are missing. Run 'build_gazetteer' first."
                )
            )
            return

        queryset = FuelStation.objects.all()
        if not options["refresh"]:
            queryset = queryset.filter(latitude__isnull=True)

        pending = list(queryset)
        if not pending:
            self.stdout.write(self.style.SUCCESS("Every station already has coordinates."))
            return

        self.stdout.write(f"Resolving {len(pending)} stations from the offline gazetteer ...")

        resolved, unresolved = [], []
        # One city serves many stations, so cache the lookup per city and state.
        seen: dict[tuple[str, str], tuple[float, float] | None] = {}
        for station in pending:
            key = (station.city, station.state)
            if key not in seen:
                seen[key] = lookup_city(station.city, station.state)
            hit = seen[key]
            if hit:
                station.latitude, station.longitude = hit
                station.geocode_source = FuelStation.GeocodeSource.GAZETTEER
                resolved.append(station)
            else:
                unresolved.append(station)

        self._save(resolved)
        self.stdout.write(
            self.style.SUCCESS(f"  gazetteer resolved {len(resolved)} stations")
        )

        if unresolved and options["use_nominatim"]:
            unresolved = self._nominatim_pass(unresolved, options["limit"])
        elif unresolved:
            towns = sorted({f"{s.city}, {s.state}" for s in unresolved})
            self.stdout.write(
                self.style.WARNING(
                    f"  {len(unresolved)} stations in {len(towns)} unlisted places "
                    f"remain. Re-run with --use-nominatim to place them."
                )
            )
            for town in towns[:15]:
                self.stdout.write(f"    - {town}")

        catalog.invalidate()
        total = FuelStation.objects.count()
        located = FuelStation.objects.filter(
            latitude__isnull=False, longitude__isnull=False
        ).count()
        percent = (located / total * 100.0) if total else 0.0
        self.stdout.write(
            self.style.SUCCESS(f"{located} of {total} stations located ({percent:.1f}%).")
        )

    def _nominatim_pass(self, stations, limit):
        config = settings.FUEL_ROUTE
        session = requests.Session()
        session.headers["User-Agent"] = config["NOMINATIM_USER_AGENT"]

        by_town: dict[tuple[str, str], list[FuelStation]] = {}
        for station in stations:
            by_town.setdefault((station.city, station.state), []).append(station)

        towns = sorted(by_town)
        if limit:
            towns = towns[:limit]
        self.stdout.write(f"  querying Nominatim for {len(towns)} places ...")

        resolved, still_missing = [], []
        for index, (city, state) in enumerate(towns, start=1):
            if index > 1:
                time.sleep(NOMINATIM_DELAY_SECONDS)
            point = self._lookup(session, city, state, config)
            if point:
                for station in by_town[(city, state)]:
                    station.latitude, station.longitude = point
                    station.geocode_source = FuelStation.GeocodeSource.NOMINATIM
                    resolved.append(station)
            else:
                still_missing.extend(by_town[(city, state)])
            if index % 25 == 0:
                self.stdout.write(f"    {index}/{len(towns)} places")

        self._save(resolved)
        self.stdout.write(self.style.SUCCESS(f"  Nominatim resolved {len(resolved)}"))
        return still_missing

    @staticmethod
    def _lookup(session, city, state, config):
        try:
            response = session.get(
                f"{config['NOMINATIM_BASE_URL']}/search",
                params={
                    "city": city,
                    "state": state,
                    "country": "USA",
                    "format": "jsonv2",
                    "limit": 1,
                },
                timeout=config["NOMINATIM_TIMEOUT_SECONDS"],
            )
            if response.status_code != 200:
                return None
            results = response.json()
        except (requests.RequestException, ValueError):
            return None
        if not results:
            return None
        return float(results[0]["lat"]), float(results[0]["lon"])

    @staticmethod
    def _save(stations):
        if not stations:
            return
        with transaction.atomic():
            FuelStation.objects.bulk_update(
                stations, ["latitude", "longitude", "geocode_source"], batch_size=1000
            )
