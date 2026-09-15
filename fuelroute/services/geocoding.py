"""Turn a caller-supplied location into coordinates.

The resolver is ordered cheapest first, because the assessment asks the API to
touch the external map service as little as possible:

1. A literal ``"lat,lon"`` pair is used as is.                  0 network calls
2. A ``"City, ST"`` string is resolved from the offline Census
   gazetteer that ships with this repository.                   0 network calls
3. A previously seen free-text query is served from the database
   cache.                                                       0 network calls
4. Anything else falls through to Nominatim and is then cached.  1 network call

So the ordinary case -- a US city and state -- reaches the routing provider
having made no geocoding request at all, and the whole plan costs exactly one
external call.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

import requests
from django.conf import settings
from django.db import IntegrityError

from fuelroute.exceptions import GeocodingError, OutsideCoverageError
from fuelroute.models import GeocodeCache
from fuelroute.services.gazetteer import lookup_city, lookup_zip

logger = logging.getLogger(__name__)

# A generous box around the fifty states plus DC. Alaska and Hawaii are inside
# it, though no drivable route connects them to the mainland.
US_BOUNDS = (18.0, -179.9, 72.0, -66.0)

_COORD_RE = re.compile(
    r"^\s*(?P<lat>-?\d+(?:\.\d+)?)\s*[,;\s]\s*(?P<lon>-?\d+(?:\.\d+)?)\s*$"
)
_CITY_STATE_RE = re.compile(r"^\s*(?P<city>[^,]+?)\s*,\s*(?P<state>[A-Za-z]{2})\s*$")
_ZIP_RE = re.compile(r"^\s*(?P<zip>\d{5})(?:-\d{4})?\s*$")


@dataclass(frozen=True, slots=True)
class Location:
    latitude: float
    longitude: float
    label: str
    source: str


def within_us(latitude: float, longitude: float) -> bool:
    min_lat, min_lon, max_lat, max_lon = US_BOUNDS
    return min_lat <= latitude <= max_lat and min_lon <= longitude <= max_lon


def _guard_coverage(location: Location) -> Location:
    if not within_us(location.latitude, location.longitude):
        raise OutsideCoverageError(
            f"'{location.label}' resolves outside the United States.",
            latitude=location.latitude,
            longitude=location.longitude,
        )
    return location


def resolve(query: str) -> Location:
    """Resolve a location string to coordinates inside the United States."""
    text = (query or "").strip()
    if not text:
        raise GeocodingError("A location is required.")

    coordinate_match = _COORD_RE.match(text)
    if coordinate_match:
        location = Location(
            latitude=float(coordinate_match["lat"]),
            longitude=float(coordinate_match["lon"]),
            label=text,
            source="coordinates",
        )
        return _guard_coverage(location)

    city_match = _CITY_STATE_RE.match(text)
    if city_match:
        hit = lookup_city(city_match["city"], city_match["state"])
        if hit:
            return _guard_coverage(
                Location(hit[0], hit[1], text, source="gazetteer")
            )

    zip_match = _ZIP_RE.match(text)
    if zip_match:
        hit = lookup_zip(zip_match["zip"])
        if hit:
            return _guard_coverage(Location(hit[0], hit[1], text, source="gazetteer"))

    cache_key = text.casefold()
    cached = GeocodeCache.objects.filter(query=cache_key).first()
    if cached:
        return _guard_coverage(
            Location(
                cached.latitude,
                cached.longitude,
                cached.display_name or text,
                source="cache",
            )
        )

    location = _nominatim(text)
    try:
        GeocodeCache.objects.create(
            query=cache_key,
            latitude=location.latitude,
            longitude=location.longitude,
            display_name=location.label[:512],
        )
    except IntegrityError:  # another worker cached it first; harmless
        logger.debug("Geocode cache race for %r", cache_key)
    return _guard_coverage(location)


def _nominatim(text: str) -> Location:
    config = settings.FUEL_ROUTE
    try:
        response = requests.get(
            f"{config['NOMINATIM_BASE_URL']}/search",
            params={
                "q": text,
                "format": "jsonv2",
                "limit": 1,
                "countrycodes": "us",
                "addressdetails": 0,
            },
            headers={"User-Agent": config["NOMINATIM_USER_AGENT"]},
            timeout=config["NOMINATIM_TIMEOUT_SECONDS"],
        )
    except requests.RequestException as exc:
        raise GeocodingError(
            f"The geocoder could not be reached while resolving '{text}'."
        ) from exc

    if response.status_code != 200:
        raise GeocodingError(
            f"The geocoder rejected the lookup for '{text}'.",
            status=response.status_code,
        )

    try:
        results = response.json()
    except ValueError as exc:
        raise GeocodingError("The geocoder returned a malformed response.") from exc

    if not results:
        raise GeocodingError(
            f"No US location matches '{text}'. Try 'City, ST' or 'latitude,longitude'."
        )

    top = results[0]
    return Location(
        latitude=float(top["lat"]),
        longitude=float(top["lon"]),
        label=top.get("display_name") or text,
        source="nominatim",
    )
