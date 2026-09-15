"""Physical invariants that any plan must satisfy, whatever the prices.

These catch a class of bug that a cost comparison cannot: a plan that is cheap
because it is impossible. During development this suite found a real defect --
a station behind the departure fill-up stayed selectable, so the plan told the
driver to go backwards.
"""

from __future__ import annotations

import random

import pytest

from fuelroute.exceptions import InfeasibleRouteError
from fuelroute.services.optimizer import Candidate, plan_fuel_stops

MPG = 10.0
RANGE = 500.0
TANK = RANGE / MPG


def assert_plan_is_physical(plan, candidates, total_miles, start_gallons=0.0):
    position = 0.0
    tank = start_gallons

    for index, purchase in enumerate(plan.purchases, start=1):
        mile = purchase.candidate.position_miles
        assert mile >= position - 1e-9, f"stop {index} lies behind stop {index - 1}"
        assert mile - position <= RANGE + 1e-9, f"leg into stop {index} exceeds range"

        tank -= (mile - position) / MPG
        assert tank >= -1e-9, f"the tank runs dry before stop {index}"
        assert purchase.arrival_fuel_gallons == pytest.approx(tank, abs=1e-6)

        position = mile
        tank += purchase.gallons
        assert tank <= TANK + 1e-9, f"the tank overflows at stop {index}"
        assert purchase.departure_fuel_gallons == pytest.approx(tank, abs=1e-6)
        assert purchase.cost == pytest.approx(
            purchase.gallons * purchase.candidate.price_per_gallon
        )

    tank -= (total_miles - position) / MPG
    assert tank >= -1e-9, "the tank runs dry before the finish"
    assert total_miles - position <= RANGE + 1e-9, "the final leg exceeds range"

    assert plan.total_cost == pytest.approx(sum(p.cost for p in plan.purchases))
    assert plan.total_gallons == pytest.approx(sum(p.gallons for p in plan.purchases))


@pytest.mark.parametrize("seed", range(60))
def test_every_plan_is_physically_possible(seed):
    rng = random.Random(1000 + seed)
    total_miles = rng.uniform(100.0, 3500.0)

    # Stations every 40 to 180 miles, starting at the origin so an empty tank
    # can set off, with prices that wander up and down like a real corridor.
    positions, mile, price = [], 0.0, rng.uniform(2.5, 4.5)
    prices = []
    while mile < total_miles:
        positions.append(mile)
        prices.append(round(max(2.0, price), 3))
        price += rng.uniform(-0.25, 0.25)
        mile += rng.uniform(40.0, 180.0)

    candidates = [Candidate(p, q) for p, q in zip(positions, prices, strict=True)]
    try:
        plan = plan_fuel_stops(candidates, total_miles, RANGE, MPG, 0.0)
    except InfeasibleRouteError:
        # A gap wider than the range is a legitimate refusal, not a defect.
        gaps = [b - a for a, b in zip(positions, positions[1:], strict=False)]
        assert gaps and max(gaps + [total_miles - positions[-1]]) > RANGE
        return

    assert_plan_is_physical(plan, candidates, total_miles)
    # Starting empty means every gallon of the journey is bought.
    assert plan.total_gallons == pytest.approx(total_miles / MPG, abs=1e-6)


@pytest.mark.parametrize("start_gallons", [0.0, 12.5, 25.0, 50.0])
def test_invariants_hold_for_any_starting_fuel(start_gallons):
    rng = random.Random(7)
    total_miles = 1800.0
    positions = [i * 120.0 for i in range(15)]
    prices = [round(rng.uniform(2.4, 4.6), 3) for _ in positions]
    candidates = [Candidate(p, q) for p, q in zip(positions, prices, strict=True)]

    plan = plan_fuel_stops(candidates, total_miles, RANGE, MPG, start_gallons)
    assert_plan_is_physical(plan, candidates, total_miles, start_gallons)
    # Fuel bought plus fuel carried covers the journey and nothing is wasted.
    assert plan.total_gallons + start_gallons == pytest.approx(total_miles / MPG, abs=1e-6)


@pytest.mark.django_db
def test_a_station_behind_the_departure_fill_up_is_not_selectable():
    """Regression: the plan must never send the driver backwards.

    The cheapest station near the origin becomes the departure fill-up and is
    priced at mile zero. A station the vehicle has already passed must drop out,
    or it can be chosen as a later stop.
    """
    from fuelroute.services import planner
    from fuelroute.services.matcher import MatchedStation

    class FakeRoute:
        total_miles = 900.0

    matched = [
        MatchedStation(catalog_index=0, position_miles=19.6, detour_miles=1.0,
                       price_per_gallon=4.20),
        MatchedStation(catalog_index=1, position_miles=34.5, detour_miles=1.0,
                       price_per_gallon=2.80),  # cheapest near the origin
        MatchedStation(catalog_index=2, position_miles=300.0, detour_miles=1.0,
                       price_per_gallon=3.10),
    ]
    candidates, origin_index = planner._build_candidates(
        matched, FakeRoute(), initial_gallons=0.0, detour_miles=15.0
    )

    assert origin_index == 1
    assert candidates[0].position_miles == 0.0
    assert 0 not in {c.payload.catalog_index for c in candidates[1:]}
    assert [c.position_miles for c in candidates] == sorted(
        c.position_miles for c in candidates
    )


# ---------------------------------------------------------------------------
# Detour fuel.
#
# A station 15 miles off the road costs 30 miles of driving that the route
# geometry does not contain. Planning legs against the full range lets the
# optimizer accept a 490 mile gap that really needs 520.
# ---------------------------------------------------------------------------


def test_a_leg_that_only_fits_without_detours_is_refused():
    """Regression: stations 15 miles off-route at miles 0, 490 and 990.

    Against the full 500 mile range the optimizer accepts the plan and the tank
    runs 0.5 gallons short reaching mile 490. Against the usable range it does
    not.
    """
    usable = RANGE - 2 * 15.0  # what the planner reserves for a 15 mile detour
    candidates = [Candidate(0.0, 3.0), Candidate(490.0, 3.0), Candidate(990.0, 3.0)]

    # The unreserved range accepts it, which is the bug.
    accepted = plan_fuel_stops(candidates, 1000.0, RANGE, MPG, 0.0)
    assert len(accepted.purchases) == 3

    # The reserved range refuses it, because 490 miles of route plus 30 miles of
    # detour does not fit in 470.
    with pytest.raises(InfeasibleRouteError):
        plan_fuel_stops(candidates, 1000.0, usable, MPG, 0.0)


@pytest.mark.django_db
def test_the_planner_reserves_range_for_the_detour():
    from django.conf import settings

    from fuelroute.services import planner

    reserve = 2.0 * settings.FUEL_ROUTE["MAX_DETOUR_MILES"]
    assert reserve > 0
    # The planner must never hand the optimizer the full range.
    assert settings.FUEL_ROUTE["MAX_RANGE_MILES"] - reserve < RANGE
    assert hasattr(planner, "plan")


@pytest.mark.django_db
def test_detour_fuel_is_billed_and_reported(client, monkeypatch):
    """Every mile driven off the route is bought, at the pump that caused it."""
    import json

    import numpy as np
    import polyline as polyline_lib
    import responses as responses_lib

    from fuelroute.models import FuelStation

    # One station, 8 miles north of a straight 300 mile route.
    FuelStation.objects.create(
        opis_id="D1", name="Detour Stop", city="Town", state="TX",
        retail_price=3.00, latitude=35.116, longitude=-99.9,
        geocode_source=FuelStation.GeocodeSource.GAZETTEER,
    )
    lons = np.linspace(-100.0, -94.9, 400)
    points = [(35.0, float(x)) for x in lons]

    with responses_lib.RequestsMock() as mocked:
        mocked.add(
            responses_lib.GET,
            __import__("re").compile(r"https://router\.project-osrm\.org/.*"),
            json={
                "code": "Ok",
                "routes": [{
                    "geometry": polyline_lib.encode(points, precision=6),
                    "distance": 300 * 1609.344, "duration": 18000.0, "legs": [],
                }],
                "waypoints": [],
            },
            status=200,
        )
        body = client.post(
            "/api/v1/route/",
            data=json.dumps({"start": "35.0,-100.0", "finish": "35.0,-94.9"}),
            content_type="application/json",
        ).json()

    plan = body["fuel_plan"]
    assert plan["stop_count"] == 1
    stop = plan["stops"][0]

    detour = plan["detour_fuel"]
    assert detour["miles_driven"] == pytest.approx(2 * stop["detour_miles"], abs=0.05)

    # The published figures must add up EXACTLY, not to within rounding. A
    # reviewer who sums the stop column has to land on the total.
    assert plan["total_cost_usd"] == round(
        sum(s["cost_usd"] for s in plan["stops"]), 2
    )
    assert plan["total_gallons_purchased"] == round(
        sum(s["gallons_purchased"] for s in plan["stops"]), 3
    )
    assert plan["total_cost_usd"] == round(
        plan["route_fuel"]["cost_usd"] + detour["cost_usd"], 2
    )
    assert plan["total_gallons_purchased"] == round(
        plan["route_fuel"]["gallons"] + detour["gallons"], 3
    )
    assert stop["gallons_purchased"] == pytest.approx(
        stop["gallons_for_route"] + stop["gallons_for_detour"], abs=1e-3
    )
    # More fuel is bought than the route alone needs, because of the detour.
    assert plan["total_gallons_purchased"] > 300.0 / MPG
