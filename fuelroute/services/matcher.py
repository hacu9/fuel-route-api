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

Step 3 measures to the nearest sample point rather than to the nearest point on
a segment, so the sample spacing bounds the error at half a stride -- 125 m at
the default 250 m.

That bound only holds because the polyline is *densified*, not thinned. The
routing provider emits a vertex only where the road changes direction, so a
straight interstate stretch can run 7 km between consecutive vertices. Picking
existing vertices would have left a worst-case error of 2.2 miles, wide enough
to push a station across the detour boundary. Interpolating along each segment
at a fixed arc length removes that: no two samples are ever more than one
stride apart, whatever the road does.
"""

from __future__ import annotations

import bisect
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


def _sample_route(
    route: Route, stride_meters: float
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Points along the route at a fixed arc length, with their mile markers.

    Returns ``(latitudes, longitudes, miles_from_origin)``. Samples are
    interpolated along the polyline rather than selected from it, so the spacing
    is uniform however far apart the provider's vertices happen to be. The
    marker array is the interpolation parameter itself, so a station's position
    along the route needs no further lookup.
    """
    cumulative = route.cumulative_miles
    total = float(cumulative[-1])
    stride_miles = stride_meters / METERS_PER_MILE

    if stride_miles <= 0 or total <= 0:
        return route.lats, route.lons, cumulative

    marks = np.arange(0.0, total, stride_miles)
    if marks.size == 0 or marks[-1] < total:
        marks = np.append(marks, total)

    return (
        np.interp(marks, cumulative, route.lats),
        np.interp(marks, cumulative, route.lons),
        marks,
    )


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

    sample_lats, sample_lons, sample_miles = _sample_route(route, stride_meters)
    reference_latitude = route.mid_latitude

    route_x, route_y = project_to_miles(sample_lats, sample_lons, reference_latitude)
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

    positions = sample_miles[vertices[hit]]
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
