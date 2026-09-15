"""Geometry helpers.

Everything the planner does happens inside one country, so a local
equirectangular projection is accurate enough and far cheaper than repeated
great-circle maths. Distances stay in statute miles throughout.
"""

from __future__ import annotations

import numpy as np

EARTH_RADIUS_MILES = 3958.7613
METERS_PER_MILE = 1609.344


def haversine_miles(
    lat1: np.ndarray, lon1: np.ndarray, lat2: np.ndarray, lon2: np.ndarray
) -> np.ndarray:
    """Great-circle distance in miles between paired coordinate arrays."""
    p1, p2 = np.radians(lat1), np.radians(lat2)
    dphi = p2 - p1
    dlam = np.radians(lon2) - np.radians(lon1)
    a = np.sin(dphi / 2.0) ** 2 + np.cos(p1) * np.cos(p2) * np.sin(dlam / 2.0) ** 2
    return 2.0 * EARTH_RADIUS_MILES * np.arcsin(np.sqrt(np.clip(a, 0.0, 1.0)))


def project_to_miles(
    lat: np.ndarray, lon: np.ndarray, lat0: float
) -> tuple[np.ndarray, np.ndarray]:
    """Project coordinates onto a flat plane measured in miles.

    ``lat0`` is the reference latitude, normally the mid-latitude of the route.
    The east-west scale shrinks by cos(lat0), which keeps the error below about
    0.3 percent across a single US route.
    """
    deg_to_miles = np.pi * EARTH_RADIUS_MILES / 180.0
    x = np.asarray(lon, dtype=float) * deg_to_miles * np.cos(np.radians(lat0))
    y = np.asarray(lat, dtype=float) * deg_to_miles
    return x, y


def cumulative_miles(lats: np.ndarray, lons: np.ndarray) -> np.ndarray:
    """Distance from the first vertex to each vertex of a polyline, in miles."""
    if lats.size < 2:
        return np.zeros_like(lats, dtype=float)
    seg = haversine_miles(lats[:-1], lons[:-1], lats[1:], lons[1:])
    out = np.empty(lats.size, dtype=float)
    out[0] = 0.0
    np.cumsum(seg, out=out[1:])
    return out
