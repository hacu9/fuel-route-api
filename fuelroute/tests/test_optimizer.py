"""Tests for the fuel stop optimizer, including a randomised optimality check."""

from __future__ import annotations

import functools
import math
import random

import pytest

from fuelroute.exceptions import InfeasibleRouteError
from fuelroute.services.optimizer import Candidate, plan_fuel_stops

MPG = 10.0
RANGE = 500.0


def cost(stations, total_miles, start_gallons=0.0, range_miles=RANGE):
    plan = plan_fuel_stops(
        [Candidate(p, q) for p, q in stations], total_miles, range_miles, MPG, start_gallons
    )
    return plan.total_cost


def test_full_tank_covers_a_short_route_without_buying():
    plan = plan_fuel_stops([Candidate(10.0, 3.0)], 400.0, RANGE, MPG, start_fuel_gallons=50.0)
    assert plan.purchases == []
    assert plan.total_cost == 0.0


def test_single_station_prices_the_whole_journey():
    plan = plan_fuel_stops([Candidate(0.0, 4.0)], 300.0, RANGE, MPG, 0.0)
    assert len(plan.purchases) == 1
    assert plan.total_gallons == pytest.approx(30.0)
    assert plan.total_cost == pytest.approx(120.0)


def test_it_buys_only_enough_to_reach_a_cheaper_pump():
    # Expensive at mile 0, cheap at mile 100, destination at 400. The optimum
    # buys 10 gallons at the dear pump and the remaining 30 at the cheap one.
    plan = plan_fuel_stops(
        [Candidate(0.0, 5.0), Candidate(100.0, 2.0)], 400.0, RANGE, MPG, 0.0
    )
    assert [round(p.gallons, 6) for p in plan.purchases] == [10.0, 30.0]
    assert plan.total_cost == pytest.approx(10 * 5.0 + 30 * 2.0)


def test_it_fills_the_tank_when_nothing_cheaper_is_in_range():
    # Cheapest pump sits at mile 0 and the next is 600 miles on, beyond range.
    plan = plan_fuel_stops(
        [Candidate(0.0, 2.0), Candidate(450.0, 9.0), Candidate(600.0, 1.0)],
        900.0, RANGE, MPG, 0.0,
    )
    assert plan.purchases[0].departure_fuel_gallons == pytest.approx(50.0)
    assert plan.purchases[0].candidate.price_per_gallon == 2.0


def test_it_never_carries_fuel_past_a_cheaper_pump():
    plan = plan_fuel_stops(
        [Candidate(0.0, 4.0), Candidate(200.0, 3.0), Candidate(400.0, 2.0)],
        600.0, RANGE, MPG, 0.0,
    )
    arrivals = [round(p.arrival_fuel_gallons, 6) for p in plan.purchases]
    assert arrivals == [0.0, 0.0, 0.0]


def test_a_gap_longer_than_the_range_is_infeasible():
    with pytest.raises(InfeasibleRouteError) as caught:
        plan_fuel_stops(
            [Candidate(0.0, 3.0), Candidate(700.0, 3.0)], 900.0, RANGE, MPG, 0.0
        )
    assert caught.value.context["gap_miles"] == pytest.approx(700.0)


def test_an_empty_tank_with_no_pump_at_the_origin_is_infeasible():
    with pytest.raises(InfeasibleRouteError) as caught:
        plan_fuel_stops([Candidate(40.0, 3.0)], 300.0, RANGE, MPG, 0.0)
    assert caught.value.context["first_station_mile"] == pytest.approx(40.0)


def test_no_station_at_all_is_infeasible():
    with pytest.raises(InfeasibleRouteError):
        plan_fuel_stops([], 300.0, RANGE, MPG, 0.0)


def test_stations_beyond_the_destination_are_ignored():
    plan = plan_fuel_stops(
        [Candidate(0.0, 3.0), Candidate(500.0, 0.5)], 200.0, RANGE, MPG, 0.0
    )
    assert len(plan.purchases) == 1
    assert plan.total_gallons == pytest.approx(20.0)


def test_unsorted_input_is_handled():
    unsorted_plan = plan_fuel_stops(
        [Candidate(300.0, 2.0), Candidate(0.0, 5.0), Candidate(100.0, 4.0)],
        600.0, RANGE, MPG, 0.0,
    )
    sorted_plan = plan_fuel_stops(
        [Candidate(0.0, 5.0), Candidate(100.0, 4.0), Candidate(300.0, 2.0)],
        600.0, RANGE, MPG, 0.0,
    )
    assert unsorted_plan.total_cost == pytest.approx(sorted_plan.total_cost)


def test_the_tank_is_never_overfilled():
    # Cheap at mile 0, dear at mile 400, destination at 800. Rule 2 fills the
    # tank at the cheap pump, and a full tank is exactly 50 gallons.
    plan = plan_fuel_stops(
        [Candidate(0.0, 2.0), Candidate(400.0, 5.0)], 800.0, RANGE, MPG, 0.0
    )
    assert plan.purchases[0].departure_fuel_gallons == pytest.approx(50.0)
    for purchase in plan.purchases:
        assert purchase.departure_fuel_gallons <= 50.0 + 1e-9


# ---------------------------------------------------------------------------
# Randomised optimality check.
#
# An independent exact solver searches every reachable stop sequence. At each
# stop it departs either with a full tank or with exactly enough fuel to reach
# the next stop, which an exchange argument shows is sufficient to contain an
# optimum. The greedy must match it on every feasible instance.
# ---------------------------------------------------------------------------


def _exhaustive_cost(positions, prices, total_miles, start_miles):
    count = len(positions)

    @functools.cache
    def best_from(index, fuel_miles):
        here = 0.0 if index < 0 else positions[index]
        price = math.inf if index < 0 else prices[index]
        if fuel_miles >= total_miles - here - 1e-9:
            return 0.0

        best = math.inf
        if total_miles - here <= RANGE + 1e-9 and index >= 0:
            best = ((total_miles - here) - fuel_miles) / MPG * price

        for nxt in range(index + 1, count):
            leg = positions[nxt] - here
            if leg > RANGE + 1e-9:
                break
            for target in (leg, RANGE):
                if target < leg - 1e-9 or target > RANGE + 1e-9:
                    continue
                buy = max(0.0, target - fuel_miles)
                if buy > 1e-9 and index < 0:
                    continue  # the vehicle cannot buy fuel at the origin
                # Departing fuel is whatever is in the tank; buying nothing
                # does not drain it down to `target`.
                onward = best_from(nxt, round(max(target, fuel_miles) - leg, 6))
                if onward < math.inf:
                    best = min(best, (buy / MPG * price if buy > 1e-9 else 0.0) + onward)
        return best

    return best_from(-1, round(start_miles, 6))


@pytest.mark.parametrize("seed", range(25))
def test_greedy_matches_an_exhaustive_search(seed):
    rng = random.Random(seed)
    count = rng.randint(1, 6)
    total_miles = rng.uniform(200.0, 900.0)
    positions = sorted(round(rng.uniform(0.0, total_miles), 3) for _ in range(count))
    prices = [round(rng.uniform(2.5, 5.5), 3) for _ in range(count)]
    start_gallons = rng.choice([0.0, 10.0, 50.0])

    expected = _exhaustive_cost(
        tuple(positions), tuple(prices), total_miles, start_gallons * MPG
    )
    try:
        actual = cost(list(zip(positions, prices, strict=True)), total_miles, start_gallons)
    except InfeasibleRouteError:
        assert math.isinf(expected), "greedy gave up on a solvable route"
        return

    assert not math.isinf(expected), "greedy solved a route the exhaustive search could not"
    assert actual == pytest.approx(expected, abs=1e-6)
