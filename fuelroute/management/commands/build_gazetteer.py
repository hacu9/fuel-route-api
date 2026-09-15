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
        best: dict[tuple[str, str], tuple[tuple[int, float], float, float]] = {}
        for row in places:
            # The Census appends exactly one descriptor, so strip exactly one.
            key = (row["USPS"].upper(), normalize_place(row["NAME"], max_suffixes=1))
            if not key[1]:
                continue
            try:
                lat = float(row["INTPTLAT"])
                lon = float(row[next(k for k in row if k.startswith("INTPTLONG"))])
                land = float(row["ALAND"])
            except (ValueError, StopIteration, KeyError):
                continue
            # An incorporated place beats a CDP; a larger place beats a smaller
            # one. That resolves the duplicate names inside a single state.
            rank = (1 if row.get("FUNCSTAT") == "A" else 0, land)
            if key not in best or rank > best[key][0]:
                best[key] = (rank, lat, lon)

        self._write(PLACES_FILE, ["state", "city_key", "lat", "lon"],
                    ([s, c, f"{lat:.6f}", f"{lon:.6f}"]
                     for (s, c), (_, lat, lon) in sorted(best.items())))
        self.stdout.write(self.style.SUCCESS(f"  wrote {len(best)} places"))

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
    def _write(path: Path, header: list[str], rows) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with gzip.open(path, "wt", encoding="utf-8", newline="", compresslevel=9) as fh:
            writer = csv.writer(fh)
            writer.writerow(header)
            writer.writerows(rows)
