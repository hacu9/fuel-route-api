"""Routing provider client.

The planner makes exactly ONE call to this service per request. The response
carries the full road geometry, so every fuel decision downstream is computed
locally from that single payload. No second call is ever made to place a stop.

The default provider is the public OSRM demo server, which needs no API key.
Point ``OSRM_BASE_URL`` at your own OSRM container for production traffic.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import numpy as np
import polyline
import requests
from django.conf import settings
from django.core.cache import cache

from fuelroute.exceptions import NoRouteError, RoutingError
from fuelroute.services.geo import METERS_PER_MILE, cumulative_miles

logger = logging.getLogger(__name__)

# OSRM returns polyline6 as a compact string. Precision 6 keeps roughly 0.1 m
# of resolution, which is far finer than anything the planner needs.
_POLYLINE_PRECISION = 6

# Roads change on a scale of months, so a cached route stays valid for a day.
ROUTE_CACHE_SECONDS = 60 * 60 * 24


@dataclass(frozen=True, slots=True)
class Route:
    """A single drivable route with its geometry pre-measured.

    ``lats``/``lons`` are the polyline vertices. ``cumulative_miles`` holds the
    distance from the origin to each vertex, which is what turns a nearby
    station into a position along the route.
    """

    lats: np.ndarray
    lons: np.ndarray
    cumulative_miles: np.ndarray
    total_miles: float
    duration_hours: float
    encoded_polyline: str

    @property
    def mid_latitude(self) -> float:
        return float((self.lats.min() + self.lats.max()) / 2.0)

    def bounding_box(self, pad_miles: float = 0.0) -> tuple[float, float, float, float]:
        """Return (min_lat, min_lon, max_lat, max_lon), optionally padded."""
        pad_lat = pad_miles / 69.0
        cos_lat = max(np.cos(np.radians(self.mid_latitude)), 0.2)
        pad_lon = pad_miles / (69.0 * cos_lat)
        return (
            float(self.lats.min() - pad_lat),
            float(self.lons.min() - pad_lon),
            float(self.lats.max() + pad_lat),
            float(self.lons.max() + pad_lon),
        )

    def geojson(self) -> dict:
        """The route as a GeoJSON LineString feature."""
        return {
            "type": "Feature",
            "properties": {
                "distance_miles": round(self.total_miles, 2),
                "duration_hours": round(self.duration_hours, 2),
            },
            "geometry": {
                "type": "LineString",
                "coordinates": [
                    [round(float(lon), 6), round(float(lat), 6)]
                    for lat, lon in zip(self.lats, self.lons, strict=True)
                ],
            },
        }


class OSRMClient:
    """Thin client over the OSRM ``route`` service."""

    last_call_was_cached: bool = False

    def __init__(self, base_url: str | None = None, timeout: float | None = None):
        config = settings.FUEL_ROUTE
        self.base_url = (base_url or config["OSRM_BASE_URL"]).rstrip("/")
        self.timeout = timeout or config["OSRM_TIMEOUT_SECONDS"]

    def route(
        self,
        start: tuple[float, float],
        finish: tuple[float, float],
        profile: str = "driving",
    ) -> Route:
        """Fetch one route. ``start`` and ``finish`` are (latitude, longitude).

        Identical endpoints are served from the cache, so a repeated request
        makes no external call at all. Roads do not move, so a long time to
        live is safe.
        """
        coordinates = f"{start[1]:.6f},{start[0]:.6f};{finish[1]:.6f},{finish[0]:.6f}"
        cache_key = f"osrm:{profile}:{coordinates}"
        cached = cache.get(cache_key)
        if cached is not None:
            self.last_call_was_cached = True
            return cached
        self.last_call_was_cached = False
        url = f"{self.base_url}/route/v1/{profile}/{coordinates}"
        params = {
            "overview": "full",
            "geometries": f"polyline{_POLYLINE_PRECISION}",
            "alternatives": "false",
            "steps": "false",
            "annotations": "false",
        }

        try:
            response = requests.get(
                url,
                params=params,
                timeout=self.timeout,
                headers={"User-Agent": settings.FUEL_ROUTE["NOMINATIM_USER_AGENT"]},
            )
        except requests.Timeout as exc:
            raise RoutingError(
                "The routing provider did not answer in time.", provider="osrm"
            ) from exc
        except requests.RequestException as exc:
            raise RoutingError(
                "The routing provider could not be reached.", provider="osrm"
            ) from exc

        if response.status_code >= 500:
            raise RoutingError(
                "The routing provider returned a server error.",
                provider="osrm",
                status=response.status_code,
            )

        try:
            payload = response.json()
        except ValueError as exc:
            raise RoutingError(
                "The routing provider returned a malformed response.", provider="osrm"
            ) from exc

        code = payload.get("code")
        if code == "NoRoute":
            raise NoRouteError(
                "No drivable route connects those two locations.",
                provider="osrm",
            )
        if code != "Ok":
            raise RoutingError(
                f"The routing provider rejected the request: {code or 'unknown error'}.",
                provider="osrm",
                detail=payload.get("message"),
            )

        routes = payload.get("routes") or []
        if not routes:
            raise NoRouteError("The routing provider returned no route.", provider="osrm")

        best = routes[0]
        # A provider can answer "Ok" and still omit or mangle a field. Treat that
        # as a provider fault, not as an unhandled server error.
        try:
            encoded = best["geometry"]
            distance_meters = float(best["distance"])
            duration_seconds = float(best["duration"])
            points = polyline.decode(encoded, precision=_POLYLINE_PRECISION)
        except (KeyError, TypeError, ValueError) as exc:
            raise RoutingError(
                "The routing provider returned a route it did not fill in.",
                provider="osrm",
            ) from exc
        if len(points) < 2:
            raise NoRouteError("The returned route has no usable geometry.")

        lats = np.fromiter((p[0] for p in points), dtype=float, count=len(points))
        lons = np.fromiter((p[1] for p in points), dtype=float, count=len(points))

        total_miles = distance_meters / METERS_PER_MILE
        # Integrating the polyline locally and the distance the provider reports
        # differ by a few hundredths of a mile, because the polyline is a
        # sampled path and the provider measures the road. The provider is
        # authoritative, so rescale the integration to end exactly on it.
        # Without this a station just short of the finish can land beyond
        # total_miles and be discarded.
        measured = cumulative_miles(lats, lons)
        if measured[-1] > 0.0 and total_miles > 0.0:
            measured *= total_miles / measured[-1]

        route = Route(
            lats=lats,
            lons=lons,
            cumulative_miles=measured,
            total_miles=total_miles,
            duration_hours=duration_seconds / 3600.0,
            encoded_polyline=encoded,
        )
        cache.set(cache_key, route, timeout=ROUTE_CACHE_SECONDS)
        return route
