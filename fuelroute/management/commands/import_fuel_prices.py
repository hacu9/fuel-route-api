"""Load the assessment price file into the FuelStation table.

The header row is matched by keyword rather than by exact spelling, so the
command survives a renamed or reordered column. It reports the mapping it chose
before it writes anything.
"""

from __future__ import annotations

import csv
import re
from decimal import Decimal, InvalidOperation
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from fuelroute.models import FuelStation
from fuelroute.services import catalog

# Ordered candidate keywords per field. The first header that contains one of
# the keywords wins, so the more specific phrases are listed first.
FIELD_KEYWORDS: dict[str, tuple[str, ...]] = {
    "opis_id": ("opis truckstop id", "truckstop id", "opis id", "site id", "station id"),
    "name": ("truckstop name", "station name", "site name", "name"),
    "address": ("address", "street"),
    "city": ("city", "town"),
    "state": ("state", "province"),
    "rack_id": ("rack id", "rack"),
    "retail_price": ("retail price", "price", "cost"),
    "zip_code": ("zip", "postal"),
    "latitude": ("latitude", "lat"),
    "longitude": ("longitude", "longitude", "lng", "lon"),
}

_MONEY = re.compile(r"[^0-9.\-]")


def _normalize_header(value: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", " ", (value or "").strip().lower()).strip()


def detect_columns(headers: list[str]) -> dict[str, str]:
    """Map our field names onto the file's actual header names."""
    normalized = {_normalize_header(h): h for h in headers}
    mapping: dict[str, str] = {}
    taken: set[str] = set()

    for field, keywords in FIELD_KEYWORDS.items():
        for keyword in keywords:
            # Exact match first, so "state" never steals "real estate".
            if keyword in normalized and normalized[keyword] not in taken:
                mapping[field] = normalized[keyword]
                taken.add(normalized[keyword])
                break
        else:
            for norm, original in normalized.items():
                if original in taken:
                    continue
                if any(keyword in norm for keyword in keywords):
                    mapping[field] = original
                    taken.add(original)
                    break
    return mapping


def parse_price(raw: str) -> Decimal | None:
    cleaned = _MONEY.sub("", (raw or "").strip())
    if not cleaned:
        return None
    try:
        value = Decimal(cleaned)
    except InvalidOperation:
        return None
    return value if value > 0 else None


class Command(BaseCommand):
    help = "Import the fuel price CSV supplied with the assessment."

    def add_arguments(self, parser):
        parser.add_argument("csv_path", type=str, help="Path to the price CSV.")
        parser.add_argument(
            "--replace",
            action="store_true",
            help="Delete existing stations before importing.",
        )
        parser.add_argument(
            "--encoding", default="utf-8-sig", help="Source file encoding."
        )

    def handle(self, *args, **options):
        path = Path(options["csv_path"])
        if not path.exists():
            raise CommandError(f"No such file: {path}")

        with path.open("r", encoding=options["encoding"], newline="") as handle:
            reader = csv.DictReader(handle)
            if not reader.fieldnames:
                raise CommandError("The CSV has no header row.")
            mapping = detect_columns(list(reader.fieldnames))

            self.stdout.write("Column mapping:")
            for field in FIELD_KEYWORDS:
                source = mapping.get(field)
                marker = self.style.SUCCESS("OK   ") if source else self.style.WARNING("none ")
                self.stdout.write(f"  {marker} {field:<14} <- {source or '(absent)'}")

            missing = [f for f in ("city", "state", "retail_price") if f not in mapping]
            if missing:
                raise CommandError(
                    "The file lacks required columns: " + ", ".join(missing)
                )

            rows = list(reader)

        if options["replace"]:
            deleted, _ = FuelStation.objects.all().delete()
            self.stdout.write(self.style.WARNING(f"Deleted {deleted} existing rows."))

        stations: dict[tuple, FuelStation] = {}
        skipped = 0
        def cell(row: dict, field: str) -> str:
            source = mapping.get(field)
            return (row.get(source) or "").strip() if source else ""

        for row in rows:
            def value(field: str, _row=row) -> str:
                return cell(_row, field)

            price = parse_price(value("retail_price"))
            city, state = value("city"), value("state").upper()[:2]
            if price is None or not city or not state:
                skipped += 1
                continue

            latitude = longitude = None
            if "latitude" in mapping and "longitude" in mapping:
                try:
                    latitude = float(value("latitude"))
                    longitude = float(value("longitude"))
                except ValueError:
                    latitude = longitude = None

            station = FuelStation(
                opis_id=value("opis_id")[:32],
                name=(value("name") or f"{city}, {state} truck stop")[:255],
                address=value("address")[:255],
                city=city[:128],
                state=state,
                rack_id=value("rack_id")[:32],
                retail_price=price,
                latitude=latitude,
                longitude=longitude,
                geocode_source=(
                    FuelStation.GeocodeSource.SOURCE_FILE if latitude else ""
                ),
            )
            # The unique constraint is (opis_id, city, state, name). A file that
            # lists a site once per fuel rack collapses to the cheapest row.
            key = (station.opis_id, station.city, station.state, station.name)
            existing = stations.get(key)
            if existing is None or station.retail_price < existing.retail_price:
                stations[key] = station

        with transaction.atomic():
            FuelStation.objects.bulk_create(
                list(stations.values()),
                update_conflicts=True,
                update_fields=[
                    "address", "rack_id", "retail_price", "latitude",
                    "longitude", "geocode_source", "updated_at",
                ],
                unique_fields=["opis_id", "city", "state", "name"],
                batch_size=1000,
            )

        catalog.invalidate()
        located = FuelStation.objects.filter(latitude__isnull=False).count()
        total = FuelStation.objects.count()
        self.stdout.write(
            self.style.SUCCESS(
                f"Read {len(rows)} rows, skipped {skipped}, stored {len(stations)} "
                f"stations. Table now holds {total}, of which {located} have "
                f"coordinates."
            )
        )
        if located < total:
            self.stdout.write(
                self.style.WARNING(
                    "Run 'python manage.py geocode_stations' to locate the rest."
                )
            )
