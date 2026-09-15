"""Offline US place and ZIP lookup.

The price file names a city and a state but carries no coordinates. Geocoding
roughly eight thousand rows through a public geocoder would take hours and
would breach its usage policy. Instead the repository ships two derived
extracts of the US Census Bureau 2023 Gazetteer, which is public domain:

* ``data/us_places.csv.gz`` -- 32k incorporated places and CDPs.
* ``data/us_zips.csv.gz``   -- 34k ZIP Code Tabulation Areas.

Both load once per process and resolve a station in constant time with no
network call at all.
"""

from __future__ import annotations

import csv
import gzip
import re
import unicodedata
from functools import lru_cache
from pathlib import Path

from django.conf import settings

DATA_DIR = Path(settings.BASE_DIR) / "data"
PLACES_FILE = DATA_DIR / "us_places.csv.gz"
ZIPS_FILE = DATA_DIR / "us_zips.csv.gz"

_PARENTHETICAL = re.compile(r"\s*\((?:balance|pt\.?)\)\s*$", re.IGNORECASE)
_NON_ALNUM = re.compile(r"[^a-z0-9 ]+")
_WHITESPACE = re.compile(r"\s+")

# Suffixes the Census appends to a place name. They are noise for matching.
_LSAD_SUFFIXES = (
    "consolidated government",
    "metropolitan government",
    "metro government",
    "unified government",
    "charter township",
    "city and borough",
    "urban county",
    "municipality",
    "township",
    "comunidad",
    "zona urbana",
    "plantation",
    "reservation",
    "corporation",
    "precinct",
    "district",
    "purchase",
    "borough",
    "village",
    "location",
    "balance",
    "parish",
    "county",
    "grant",
    "town",
    "city",
    "gore",
    "cdp",
)

# Written-out directionals and common abbreviations seen in truck stop records.
_ALIASES = {
    "ft": "fort",
    "mt": "mount",
    "st": "saint",
    "ste": "sainte",
    "n": "north",
    "s": "south",
    "e": "east",
    "w": "west",
}


def normalize_place(name: str, max_suffixes: int = 1) -> str:
    """Reduce a place name to a stable comparison key.

    The Census appends exactly one legal/statistical descriptor to a name, so
    exactly one is stripped by default. Stripping greedily would turn
    "Oklahoma City city" into "oklahoma" and let it collide with a different
    town named Oklahoma, which is why ``max_suffixes`` is bounded.

    A name that is nothing but a descriptor, such as "City", is left alone.
    """
    text = unicodedata.normalize("NFKD", name or "")
    text = text.encode("ascii", "ignore").decode()
    text = _PARENTHETICAL.sub("", text).strip().lower()
    text = _NON_ALNUM.sub(" ", text)
    text = _WHITESPACE.sub(" ", text).strip()

    for _ in range(max_suffixes):
        for suffix in _LSAD_SUFFIXES:
            if text.endswith(" " + suffix):
                text = text[: -(len(suffix) + 1)].strip()
                break
        else:
            break

    return " ".join(_ALIASES.get(word, word) for word in text.split())


def candidate_keys(name: str) -> list[str]:
    """Every key a caller's spelling of ``name`` might be stored under.

    A caller may type the bare name ("Oklahoma City") or the Census spelling
    ("Oklahoma City city"), so both are tried, longest first.
    """
    keys = []
    for depth in (0, 1, 2):
        key = normalize_place(name, max_suffixes=depth)
        if key and key not in keys:
            keys.append(key)
    return keys


@lru_cache(maxsize=1)
def _places() -> dict[tuple[str, str], tuple[float, float]]:
    table: dict[tuple[str, str], tuple[float, float]] = {}
    if not PLACES_FILE.exists():
        return table
    with gzip.open(PLACES_FILE, "rt", encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            table[(row["state"].upper(), row["city_key"])] = (
                float(row["lat"]),
                float(row["lon"]),
            )
    return table


@lru_cache(maxsize=1)
def _zips() -> dict[str, tuple[float, float]]:
    table: dict[str, tuple[float, float]] = {}
    if not ZIPS_FILE.exists():
        return table
    with gzip.open(ZIPS_FILE, "rt", encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            table[row["zip"].zfill(5)] = (float(row["lat"]), float(row["lon"]))
    return table


def lookup_city(city: str, state: str) -> tuple[float, float] | None:
    """Return coordinates for a city in a state, or None when it is unknown."""
    if not city or not state:
        return None
    table = _places()
    code = state.strip().upper()
    for key in candidate_keys(city):
        hit = table.get((code, key))
        if hit:
            return hit
    return None


def lookup_zip(zip_code: str) -> tuple[float, float] | None:
    """Return coordinates for a five digit ZIP, or None when it is unknown."""
    if not zip_code:
        return None
    digits = re.sub(r"\D", "", str(zip_code))[:5]
    if len(digits) != 5:
        return None
    return _zips().get(digits)


def is_available() -> bool:
    """True when both gazetteer extracts are present on disk."""
    return PLACES_FILE.exists() and ZIPS_FILE.exists()
