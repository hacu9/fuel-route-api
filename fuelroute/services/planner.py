"""Orchestration: location in, priced route with fuel stops out.

External call budget, which the assessment asks to keep as low as possible:

    geocoding   0 calls for "City, ST", a ZIP, "lat,lon", or any cached string
                1 call otherwise, and the result is cached for next time
    routing     1 call, always

So the ordinary request costs exactly ONE external call, and a cold request for
an unusual free-text place costs at most three. Everything after the routing
response -- matching stations to the road, ordering them, choosing stops and
pricing the trip -- happens in this process against in-memory data.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from django.conf import settings

from fuelroute.exceptions import CatalogEmptyError, InfeasibleRouteError
from fuelroute.services import geocoding
from fuelroute.services.catalog import get_catalog
from fuelroute.services.matcher import match_stations, prune_dominated
from fuelroute.services.optimizer import Candidate, plan_fuel_stops
from fuelroute.services.routing import OSRMClient, Route

logger = logging.getLogger(__name__)

# How far along the route the planner will look for the tank-filling stop that
# the vehicle makes before it really sets off.
ORIGIN_GRACE_MILES = 50.0


@dataclass(frozen=True, slots=True)
class PlanResult:
    payload: dict
    route: Route


def _vehicle_settings(overrides: dict | None = None) -> dict:
    config = settings.FUEL_ROUTE
    values = {
        "max_range_miles": config["MAX_RANGE_MILES"],
        "miles_per_gallon": config["MILES_PER_GALLON"],
        "max_detour_miles": config["MAX_DETOUR_MILES"],
        "min_stop_spacing_miles": config["MIN_STOP_SPACING_MILES"],
    }
    values.update({k: v for k, v in (overrides or {}).items() if v is not None})
    return values


def plan(
    start_query: str,
    finish_query: str,
    start_fuel_gallons: float | None = None,
    max_detour_miles: float | None = None,
    min_stop_spacing_miles: float | None = None,
) -> PlanResult:
    """Build a priced route between two US locations."""
    started_at = time.perf_counter()
    geocode_calls = 0

    origin = geocoding.resolve(start_query)
    geocode_calls += origin.source == "nominatim"
    destination = geocoding.resolve(finish_query)
    geocode_calls += destination.source == "nominatim"

    vehicle = _vehicle_settings(
        {
            "max_detour_miles": max_detour_miles,
            "min_stop_spacing_miles": min_stop_spacing_miles,
        }
    )
    range_miles = vehicle["max_range_miles"]
    mpg = vehicle["miles_per_gallon"]
    tank_gallons = range_miles / mpg

    client = OSRMClient()
    routing_started = time.perf_counter()
    route = client.route(
        (origin.latitude, origin.longitude),
        (destination.latitude, destination.longitude),
    )
    routing_ms = (time.perf_counter() - routing_started) * 1000.0

    catalog = get_catalog()
    if len(catalog) == 0:
        raise CatalogEmptyError(
            "No fuel stations are loaded. Run 'import_fuel_prices' and "
            "'geocode_stations' before asking for a plan."
        )

    matched = match_stations(
        route,
        catalog,
        max_detour_miles=vehicle["max_detour_miles"],
        stride_meters=settings.FUEL_ROUTE["ROUTE_SAMPLE_STRIDE_METERS"],
    )
    spacing = vehicle["min_stop_spacing_miles"]
    pruned = prune_dominated(matched, spacing)

    # The default is an empty tank, so the reported total is the cost of fuel
    # for the whole journey rather than the cost of topping up a tank that was
    # already paid for. A caller who wants the "leave with a full tank" reading
    # passes start_fuel_gallons explicitly.
    initial_gallons = 0.0 if start_fuel_gallons is None else float(start_fuel_gallons)
    initial_gallons = max(0.0, min(initial_gallons, tank_gallons))

    candidates, origin_fill_index = _build_candidates(
        pruned, route, initial_gallons, vehicle["max_detour_miles"]
    )

    try:
        fuel_plan = plan_fuel_stops(
            candidates=candidates,
            total_miles=route.total_miles,
            range_miles=range_miles,
            miles_per_gallon=mpg,
            start_fuel_gallons=initial_gallons,
        )
        spacing_applied = spacing
    except InfeasibleRouteError:
        # Pruning can remove the one station that bridged a long empty stretch.
        # Retry against every matched station before reporting failure.
        if not pruned or len(pruned) == len(matched):
            raise
        logger.info("Pruned candidate set was infeasible; retrying with all stations")
        candidates, origin_fill_index = _build_candidates(
            matched, route, initial_gallons, vehicle["max_detour_miles"]
        )
        fuel_plan = plan_fuel_stops(
            candidates=candidates,
            total_miles=route.total_miles,
            range_miles=range_miles,
            miles_per_gallon=mpg,
            start_fuel_gallons=initial_gallons,
        )
        spacing_applied = 0.0

    stops = []
    for purchase in fuel_plan.purchases:
        matched_station = purchase.candidate.payload
        record = catalog.record(matched_station.catalog_index)
        record.update(
            {
                "route_mile_marker": round(matched_station.position_miles, 1),
                "detour_miles": round(matched_station.detour_miles, 2),
                "gallons_purchased": round(purchase.gallons, 3),
                "cost_usd": round(purchase.cost, 2),
                "tank_gallons_on_arrival": round(purchase.arrival_fuel_gallons, 2),
                "tank_gallons_on_departure": round(purchase.departure_fuel_gallons, 2),
                "is_origin_fill": matched_station.catalog_index == origin_fill_index
                and purchase.candidate.position_miles == 0.0,
            }
        )
        if record["is_origin_fill"]:
            record["note"] = (
                "Departure fill-up. The vehicle starts with an empty tank, so this "
                "stop is priced as mile zero and covers the whole journey."
            )
        stops.append(record)

    consumed_gallons = route.total_miles / mpg
    elapsed_ms = (time.perf_counter() - started_at) * 1000.0

    payload = {
        "start": _location_payload(origin),
        "finish": _location_payload(destination),
        "route": {
            "distance_miles": round(route.total_miles, 2),
            "duration_hours": round(route.duration_hours, 2),
            "geometry_polyline6": route.encoded_polyline,
            "bounds": [round(v, 6) for v in route.bounding_box()],
        },
        "vehicle": {
            "max_range_miles": range_miles,
            "miles_per_gallon": mpg,
            "tank_capacity_gallons": round(tank_gallons, 2),
            "start_fuel_gallons": round(initial_gallons, 2),
        },
        "fuel_plan": {
            "stops": stops,
            "stop_count": len(stops),
            "total_gallons_purchased": round(fuel_plan.total_gallons, 3),
            "total_cost_usd": round(fuel_plan.total_cost, 2),
            "average_price_per_gallon": (
                round(fuel_plan.total_cost / fuel_plan.total_gallons, 3)
                if fuel_plan.total_gallons > 0
                else None
            ),
            "fuel_consumed_gallons": round(consumed_gallons, 2),
        },
        "meta": {
            "external_api_calls": {
                "routing": 0 if client.last_call_was_cached else 1,
                "geocoding": geocode_calls,
                "total": (0 if client.last_call_was_cached else 1) + geocode_calls,
                "routing_served_from_cache": client.last_call_was_cached,
            },
            "stations_in_catalog": len(catalog),
            "stations_near_route": len(matched),
            "stations_considered": len(pruned),
            "max_detour_miles": vehicle["max_detour_miles"],
            "min_stop_spacing_miles": spacing_applied,
            "timing_ms": {
                "routing_provider": round(routing_ms, 1),
                "local_computation": round(elapsed_ms - routing_ms, 1),
                "total": round(elapsed_ms, 1),
            },
            "elapsed_ms": round(elapsed_ms, 1),
        },
    }
    return PlanResult(payload=payload, route=route)


def _location_payload(location: geocoding.Location) -> dict:
    return {
        "query": location.label,
        "latitude": round(location.latitude, 6),
        "longitude": round(location.longitude, 6),
        "resolved_by": location.source,
    }


def _build_candidates(matched, route: Route, initial_gallons: float, detour_miles: float):
    """Turn matched stations into optimizer candidates.

    With an empty tank the vehicle cannot cover even the first mile, so the
    planner nominates one station near the origin as the departure fill-up and
    treats it as sitting at mile zero. That station is reported with
    ``is_origin_fill`` set, so the choice is visible rather than hidden.
    """
    candidates = [
        Candidate(
            position_miles=station.position_miles,
            price_per_gallon=station.price_per_gallon,
            payload=station,
        )
        for station in matched
    ]
    if initial_gallons > 0.0 or not candidates:
        return candidates, None

    grace = min(ORIGIN_GRACE_MILES, route.total_miles)
    near_origin = [c for c in candidates if c.position_miles <= grace]
    if not near_origin:
        fallback = settings.FUEL_ROUTE["ORIGIN_FALLBACK_RADIUS_MILES"]
        near_origin = [c for c in candidates if c.position_miles <= fallback]
    if not near_origin:
        raise InfeasibleRouteError(
            "No fuel station sits near the start of this route, so the vehicle "
            "cannot set off with an empty tank.",
            nearest_station_mile=round(min(c.position_miles for c in candidates), 1),
        )

    chosen = min(near_origin, key=lambda c: (c.price_per_gallon, c.position_miles))
    relocated = Candidate(0.0, chosen.price_per_gallon, chosen.payload)
    candidates = [relocated] + [c for c in candidates if c is not chosen]
    return candidates, chosen.payload.catalog_index
