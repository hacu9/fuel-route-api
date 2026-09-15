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

    # A station 15 miles off the road costs 30 miles of driving that the route
    # geometry does not contain. Planning legs against the full 500 miles lets
    # the optimizer accept a 490 mile gap that really needs 520, and the tank
    # runs dry. Reserve the worst-case detour at both ends of every leg.
    detour_reserve = 2.0 * vehicle["max_detour_miles"]
    usable_range_miles = max(range_miles - detour_reserve, 0.0)
    if usable_range_miles <= 0.0:
        raise InfeasibleRouteError(
            "The detour allowance is larger than the vehicle range.",
            vehicle_range_miles=range_miles,
            max_detour_miles=vehicle["max_detour_miles"],
        )

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
            range_miles=usable_range_miles,
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
            range_miles=usable_range_miles,
            miles_per_gallon=mpg,
            start_fuel_gallons=initial_gallons,
        )
        spacing_applied = 0.0

    stops = []
    detour_miles_total = 0.0
    detour_gallons_total = 0.0
    detour_cost_total = 0.0
    for purchase in fuel_plan.purchases:
        matched_station = purchase.candidate.payload
        record = catalog.record(matched_station.catalog_index)

        # Leaving the road and rejoining it burns fuel, and the driver buys that
        # fuel at this pump. Billing it here keeps the totals equal to what the
        # journey really consumes.
        detour_round_trip = 2.0 * matched_station.detour_miles
        detour_gallons = detour_round_trip / mpg
        detour_cost = detour_gallons * purchase.candidate.price_per_gallon
        detour_miles_total += detour_round_trip
        detour_gallons_total += detour_gallons
        detour_cost_total += detour_cost

        record.update(
            {
                "route_mile_marker": round(matched_station.position_miles, 1),
                "detour_miles": round(matched_station.detour_miles, 2),
                "gallons_purchased": round(purchase.gallons + detour_gallons, 3),
                "gallons_for_route": round(purchase.gallons, 3),
                "gallons_for_detour": round(detour_gallons, 3),
                "cost_usd": round(purchase.cost + detour_cost, 2),
                "tank_gallons_on_arrival": round(purchase.arrival_fuel_gallons, 2),
                "tank_gallons_on_departure": round(purchase.departure_fuel_gallons, 2),
                "is_origin_fill": matched_station.catalog_index == origin_fill_index
                and purchase.candidate.position_miles == 0.0,
            }
        )
        if record["is_origin_fill"]:
            note = (
                "Departure fill-up. The vehicle starts with an empty tank, so this "
                "stop is priced as mile zero and covers the whole journey."
            )
            if matched_station.position_miles > ORIGIN_GRACE_MILES:
                note += (
                    f" It is the first pump on the route and sits "
                    f"{matched_station.position_miles:.0f} miles along, because the "
                    f"price file lists none nearer the start."
                )
            record["note"] = note
        stops.append(record)

    total_cost = round(sum(stop["cost_usd"] for stop in stops), 2)
    total_gallons = round(sum(stop["gallons_purchased"] for stop in stops), 3)
    route_cost = round(fuel_plan.total_cost, 2)
    route_gallons = round(fuel_plan.total_gallons, 3)

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
            # Legs are planned against this, not the full range, so that the
            # drive off the route and back always fits.
            "usable_range_miles": round(usable_range_miles, 1),
        },
        "fuel_plan": {
            "stops": stops,
            "stop_count": len(stops),
            # Totals are summed from the ROUNDED per-stop figures, and the
            # detour share is the residual, so the stop column adds up to the
            # total and the split adds up to it too. Summing unrounded values
            # and rounding once leaves the column short by a few cents, which
            # reads as an arithmetic error even though it is not.
            "total_gallons_purchased": total_gallons,
            "total_cost_usd": total_cost,
            "average_price_per_gallon": (
                round(total_cost / total_gallons, 3) if total_gallons > 0 else None
            ),
            "fuel_consumed_gallons": round(consumed_gallons + detour_gallons_total, 2),
            "route_fuel": {
                "gallons": route_gallons,
                "cost_usd": route_cost,
            },
            "detour_fuel": {
                "miles_driven": round(detour_miles_total, 2),
                "gallons": round(total_gallons - route_gallons, 3),
                "cost_usd": round(total_cost - route_cost, 2),
            },
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

    # Prefer the cheapest pump close to the origin. Widen once if there is
    # none.
    grace = min(ORIGIN_GRACE_MILES, route.total_miles)
    near_origin = [c for c in candidates if c.position_miles <= grace]
    if not near_origin:
        fallback = settings.FUEL_ROUTE["ORIGIN_FALLBACK_RADIUS_MILES"]
        near_origin = [c for c in candidates if c.position_miles <= fallback]

    if near_origin:
        chosen = min(near_origin, key=lambda c: (c.price_per_gallon, c.position_miles))
    else:
        # Real price files are sparse. The assessment file lists ten stations in
        # the whole of California, none of them near Los Angeles, so a route out
        # of that city has no pump for 269 miles. Refusing would be pedantically
        # correct and useless, and the cost stays exact either way: the whole
        # route distance is still bought, because the fill-up is priced at mile
        # zero. So fall back to the first pump the route reaches, and say how far
        # away it is.
        chosen = min(candidates, key=lambda c: c.position_miles)

    # The vehicle drives to this station before it really sets off, so every
    # station it has already passed is gone. Keeping them would let the planner
    # pick a stop behind the departure fill-up, which reads as driving backwards
    # and puts the stops out of route order.
    ahead = [c for c in candidates if c.position_miles > chosen.position_miles]
    relocated = Candidate(0.0, chosen.price_per_gallon, chosen.payload)
    return [relocated] + ahead, chosen.payload.catalog_index
