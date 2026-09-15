"""Place fuel stations along a route.

The routing provider hands back one polyline. Every station has to be turned
into a distance-from-origin so the optimizer can reason about range, and this
module does that entirely locally -- no second call to the map service.

Method:

1. Discard stations outside the route bounding box, padded by the detour
   radius. That removes almost the whole country in one vectorised pass.
2. Resample the polyline to a fixed spacing and build a k-d tree over the
   survivors' projected coordinates.
3. For each remaining station, find the nearest route vertex. If it is inside
   the detour radius the station is usable, and that vertex's cumulative
   distance becomes the station's position along the route.

Step 3 measures to the nearest polyline *vertex* rather than the nearest point
on a segment. With the default 250 m resampling the largest possible error is
125 m, which is immaterial against a detour radius measured in miles.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import numpy as np
from scipy.spatial import cKDTree

from fuelroute.services.catalog import StationCatalog
from fuelroute.services.geo import METERS_PER_MILE, project_to_miles
from fuelroute.services.routing import Route

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class MatchedStation:
    catalog_index: int
    position_miles: float
    detour_miles: float
    price_per_gallon: float


def _resample(route: Route, stride_meters: float) -> np.ndarray:
    """Indices of route vertices spaced at least ``stride_meters`` apart.

    The first and last vertices are always kept, so the route keeps its exact
    endpoints.
    """
    stride_miles = stride_meters / METERS_PER_MILE
    if stride_miles <= 0 or route.cumulative_miles[-1] <= stride_miles:
        return np.arange(route.lats.size)

    marks = np.arange(0.0, route.cumulative_miles[-1], stride_miles)
    picked = np.searchsorted(route.cumulative_miles, marks, side="left")
    picked = np.append(picked, route.lats.size - 1)
    return np.unique(np.clip(picked, 0, route.lats.size - 1))


def match_stations(
    route: Route,
    catalog: StationCatalog,
    max_detour_miles: float,
    stride_meters: float = 250.0,
) -> list[MatchedStation]:
    """Return every station within ``max_detour_miles`` of the route."""
    if len(catalog) == 0:
        return []

    min_lat, min_lon, max_lat, max_lon = route.bounding_box(pad_miles=max_detour_miles)
    inside = (
        (catalog.latitudes >= min_lat)
        & (catalog.latitudes <= max_lat)
        & (catalog.longitudes >= min_lon)
        & (catalog.longitudes <= max_lon)
    )
    candidate_indices = np.flatnonzero(inside)
    if candidate_indices.size == 0:
        return []

    sample = _resample(route, stride_meters)
    reference_latitude = route.mid_latitude

    route_x, route_y = project_to_miles(
        route.lats[sample], route.lons[sample], reference_latitude
    )
    station_x, station_y = project_to_miles(
        catalog.latitudes[candidate_indices],
        catalog.longitudes[candidate_indices],
        reference_latitude,
    )

    tree = cKDTree(np.column_stack((route_x, route_y)))
    distances, vertices = tree.query(
        np.column_stack((station_x, station_y)),
        k=1,
        distance_upper_bound=max_detour_miles,
    )

    # cKDTree marks a miss with an infinite distance and an out-of-range index.
    hit = np.isfinite(distances)
    if not hit.any():
        return []

    positions = route.cumulative_miles[sample][vertices[hit]]
    matched_indices = candidate_indices[hit]
    detours = distances[hit]
    prices = catalog.prices[matched_indices]

    logger.debug(
        "Matched %d of %d stations within %.1f mi of the route",
        matched_indices.size, len(catalog), max_detour_miles,
    )

    return [
        MatchedStation(
            catalog_index=int(index),
            position_miles=float(position),
            detour_miles=float(detour),
            price_per_gallon=float(price),
        )
        for index, position, detour, price in zip(
            matched_indices, positions, detours, prices, strict=True
        )
    ]


def prune_dominated(
    stations: list[MatchedStation], min_spacing_miles: float
) -> list[MatchedStation]:
    """Drop stations that a cheaper neighbour makes pointless.

    Without this the optimizer is free to stop twice within a mile to save a
    fraction of a cent, which is mathematically optimal and operationally
    absurd -- a driver does not leave the interstate to buy a tenth of a gallon.

    The sweep takes stations cheapest first and accepts one only when no
    already-accepted station sits within ``min_spacing_miles`` of it. Every
    survivor is therefore the cheapest pump in its own neighbourhood, and the
    survivors are at least that far apart.

    This narrows the search space, so the plan it produces can cost marginally
    more than the unconstrained optimum. The planner falls back to the full set
    if the narrowed one turns out to be infeasible.
    """
    if min_spacing_miles <= 0 or len(stations) < 2:
        return stations

    import bisect

    accepted_positions: list[float] = []
    kept: list[MatchedStation] = []
    for station in sorted(stations, key=lambda s: (s.price_per_gallon, s.position_miles)):
        slot = bisect.bisect_left(accepted_positions, station.position_miles)
        crowded = (
            slot > 0
            and station.position_miles - accepted_positions[slot - 1] < min_spacing_miles
        ) or (
            slot < len(accepted_positions)
            and accepted_positions[slot] - station.position_miles < min_spacing_miles
        )
        if not crowded:
            accepted_positions.insert(slot, station.position_miles)
            kept.append(station)

    kept.sort(key=lambda s: s.position_miles)
    return kept
