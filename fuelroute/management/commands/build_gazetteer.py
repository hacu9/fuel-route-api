"""Rebuild the offline gazetteer extracts from the US Census source files.

The output files are committed to the repository, so a reviewer never has to
run this. Re-run it only to refresh the Census vintage. It deliberately reuses
``gazetteer.normalize_place`` so the keys written here and the keys looked up
at request time can never drift apart.
"""

from __future__ import annotations

import csv
import gzip
import io
import urllib.request
import zipfile
from pathlib import Path

from django.core.management.base import BaseCommand

from fuelroute.services.gazetteer import (
    PLACES_FILE,
    ZIPS_FILE,
    normalize_place,
)

CENSUS_ROOT = "https://www2.census.gov/geo/docs/maps-data/data/gazetteer"
USER_AGENT = "fuel-route-api/1.0 (+gazetteer build)"


def _download(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=120) as response:
        return response.read()


def _rows(payload: bytes) -> list[dict[str, str]]:
    archive = zipfile.ZipFile(io.BytesIO(payload))
    text = archive.read(archive.namelist()[0]).decode("latin-1").splitlines()
    header = [column.strip() for column in text[0].split("\t")]
    out = []
    for line in text[1:]:
        parts = [value.strip() for value in line.split("\t")]
        if len(parts) >= len(header):
            out.append(dict(zip(header, parts, strict=False)))
    return out


class Command(BaseCommand):
    help = "Rebuild data/us_places.csv.gz and data/us_zips.csv.gz from Census data."

    def add_arguments(self, parser):
        parser.add_argument("--year", default="2023", help="Census gazetteer vintage.")

    def handle(self, *args, **options):
        year = options["year"]
        base = f"{CENSUS_ROOT}/{year}_Gazetteer"

        self.stdout.write(f"Downloading {year} place gazetteer ...")
        places = _rows(_download(f"{base}/{year}_Gaz_place_national.zip"))
        # Two tables. `primary` holds the Census spelling with its one trailing
        # descriptor removed. `alias` holds the shorter names people actually
        # type. A primary key always wins, so "Oklahoma City" can never be
        # swallowed by a different town called Oklahoma.
        primary: dict[tuple[str, str], tuple[tuple[int, float], float, float]] = {}
        alias: dict[tuple[str, str], tuple[tuple[int, float], float, float]] = {}

        for row in places:
            state = row["USPS"].upper()
            name = row["NAME"]
            try:
                lat = float(row["INTPTLAT"])
                lon = float(row[next(k for k in row if k.startswith("INTPTLONG"))])
                land = float(row["ALAND"])
            except (ValueError, StopIteration, KeyError):
                continue
            # An incorporated place beats a CDP; a larger place beats a smaller
            # one. That resolves the duplicate names inside a single state.
            rank = (1 if row.get("FUNCSTAT") == "A" else 0, land)

            # The Census appends exactly one descriptor, so strip exactly one.
            key = normalize_place(name, max_suffixes=1)
            if not key:
                continue
            if (state, key) not in primary or rank > primary[(state, key)][0]:
                primary[(state, key)] = (rank, lat, lon)

            for candidate in self._alias_names(name):
                slot = (state, candidate)
                if not candidate or candidate == key:
                    continue
                if slot not in alias or rank > alias[slot][0]:
                    alias[slot] = (rank, lat, lon)

        # Third tier: county subdivisions. In New England a town is a Minor
        # Civil Division and never appears in the place file at all, which is
        # why Berlin MA, Auburn NH and Branford CT were unplaceable. Townships
        # elsewhere, such as Bensalem PA, have the same problem.
        #
        # These only ever fill an empty slot. A real incorporated place always
        # wins, because a statistical division can share its name.
        self.stdout.write(f"Downloading {year} county subdivision gazetteer ...")
        subdivisions = _rows(_download(f"{base}/{year}_Gaz_cousubs_national.zip"))
        subdivision: dict[tuple[str, str], tuple[tuple[int, float], float, float]] = {}
        for row in subdivisions:
            state = row["USPS"].upper()
            try:
                lat = float(row["INTPTLAT"])
                lon = float(row[next(k for k in row if k.startswith("INTPTLONG"))])
                land = float(row["ALAND"])
            except (ValueError, StopIteration, KeyError):
                continue
            # A governing town beats a statistical division of the same name.
            rank = (1 if row.get("FUNCSTAT") == "A" else 0, land)
            for candidate in {normalize_place(row["NAME"], max_suffixes=1),
                              *self._alias_names(row["NAME"])}:
                if not candidate:
                    continue
                slot = (state, candidate)
                if slot not in subdivision or rank > subdivision[slot][0]:
                    subdivision[slot] = (rank, lat, lon)

        merged = dict(primary)
        added = 0
        for slot, value in alias.items():
            if slot not in merged:
                merged[slot] = value
                added += 1
        towns = 0
        for slot, value in subdivision.items():
            if slot not in merged:
                merged[slot] = value
                towns += 1

        self._write(PLACES_FILE, ["state", "city_key", "lat", "lon"],
                    ([s, c, f"{lat:.6f}", f"{lon:.6f}"]
                     for (s, c), (_, lat, lon) in sorted(merged.items())))
        self.stdout.write(self.style.SUCCESS(
            f"  wrote {len(merged)} places ({len(primary)} Census spellings "
            f"+ {added} aliases + {towns} towns and townships)"
        ))

        self.stdout.write(f"Downloading {year} ZCTA gazetteer ...")
        zctas = _rows(_download(f"{base}/{year}_Gaz_zcta_national.zip"))
        zip_rows = []
        for row in zctas:
            try:
                code = row["GEOID"].zfill(5)
                lat = float(row["INTPTLAT"])
                lon = float(row[next(k for k in row if k.startswith("INTPTLONG"))])
            except (ValueError, StopIteration, KeyError):
                continue
            zip_rows.append([code, f"{lat:.6f}", f"{lon:.6f}"])

        self._write(ZIPS_FILE, ["zip", "lat", "lon"], sorted(zip_rows))
        self.stdout.write(self.style.SUCCESS(f"  wrote {len(zip_rows)} ZIP areas"))

    @staticmethod
    def _alias_names(name: str) -> list[str]:
        """Shorter spellings a caller is likely to type for this place.

        Two cases the Census spelling hides:

        * "Boise City city" is simply Boise to everyone who lives there, so the
          more aggressive strip is indexed too.
        * "Athens-Clarke County unified government" is a consolidated city and
          county. The part before the hyphen is the city people name.
        """
        out = [normalize_place(name, max_suffixes=2)]
        if "-" in name:
            out.append(normalize_place(name.split("-", 1)[0], max_suffixes=1))
            out.append(normalize_place(name.split("-", 1)[0], max_suffixes=0))
        return out

    @staticmethod
    def _write(path: Path, header: list[str], rows) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with gzip.open(path, "wt", encoding="utf-8", newline="", compresslevel=9) as fh:
            writer = csv.writer(fh)
            writer.writerow(header)
            writer.writerows(rows)
