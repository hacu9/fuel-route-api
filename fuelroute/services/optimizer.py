"""Choose the cheapest set of fuel stops along a route.

This is the classic gas station problem with a single fixed tank size. The
optimal policy is a two-rule greedy, and it is provably optimal when the tank
capacity is the same at every stop (Khuller, Malekian and Mestre, "To Fill or
Not to Fill: The Gas Station Problem", ACM Transactions on Algorithms, 2011):

* Rule 1 -- if a cheaper station sits within range, buy only enough fuel to
  reach it. Never carry expensive fuel past a cheaper pump.
* Rule 2 -- if no cheaper station sits within range, fill the tank here and
  drive to the cheapest station that is still reachable.

The destination acts as a terminal: once it is within range, the vehicle buys
exactly enough to arrive and nothing more.

The implementation runs in O(n log n): a monotonic stack precomputes the next
cheaper station for every index, and a sparse table answers "cheapest station
in this window" in constant time. A route across the United States produces a
few thousand candidates, so a plan is computed in well under a millisecond.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from fuelroute.exceptions import InfeasibleRouteError

# Positions closer together than this are treated as the same point. It keeps
# the strictly-increasing assumption true without discarding real stations.
POSITION_EPSILON_MILES = 1e-6


@dataclass(frozen=True, slots=True)
class Candidate:
    """A station that the vehicle could use, placed along the route."""

    position_miles: float
    price_per_gallon: float
    payload: Any = None


@dataclass(frozen=True, slots=True)
class Purchase:
    """Fuel bought at one stop."""

    candidate: Candidate
    gallons: float
    cost: float
    arrival_fuel_gallons: float
    departure_fuel_gallons: float


@dataclass(slots=True)
class FuelPlan:
    purchases: list[Purchase] = field(default_factory=list)
    total_cost: float = 0.0
    total_gallons: float = 0.0

    @property
    def stop_count(self) -> int:
        return len(self.purchases)


def _next_cheaper(prices: list[float]) -> list[int]:
    """For each index, the next index to its right with a strictly lower price."""
    result = [-1] * len(prices)
    stack: list[int] = []
    for index, price in enumerate(prices):
        while stack and prices[stack[-1]] > price:
            result[stack.pop()] = index
        stack.append(index)
    return result


class _RangeMinimum:
    """Sparse table returning the index of the lowest price in a window."""

    __slots__ = ("_prices", "_table", "_log")

    def __init__(self, prices: list[float]):
        self._prices = prices
        size = len(prices)
        self._log = [0] * (size + 1)
        for value in range(2, size + 1):
            self._log[value] = self._log[value >> 1] + 1
        levels = self._log[size] + 1 if size else 1
        self._table = [list(range(size))] + [[0] * size for _ in range(levels - 1)]
        for level in range(1, levels):
            span = 1 << level
            half = span >> 1
            previous, current = self._table[level - 1], self._table[level]
            for start in range(size - span + 1):
                left, right = previous[start], previous[start + half]
                current[start] = left if prices[left] <= prices[right] else right

    def argmin(self, low: int, high: int) -> int:
        """Index of the cheapest price in the inclusive range [low, high]."""
        level = self._log[high - low + 1]
        left = self._table[level][low]
        right = self._table[level][high - (1 << level) + 1]
        return left if self._prices[left] <= self._prices[right] else right


def _last_reachable(positions: list[float], start: int, limit: float) -> int:
    """Largest index at or after ``start`` whose position is at most ``limit``."""
    low, high, found = start, len(positions) - 1, start - 1
    while low <= high:
        mid = (low + high) // 2
        if positions[mid] <= limit + POSITION_EPSILON_MILES:
            found, low = mid, mid + 1
        else:
            high = mid - 1
    return found


def plan_fuel_stops(
    candidates: list[Candidate],
    total_miles: float,
    range_miles: float,
    miles_per_gallon: float,
    start_fuel_gallons: float = 0.0,
) -> FuelPlan:
    """Return the cheapest feasible sequence of fuel purchases.

    ``candidates`` need not be sorted. Any candidate beyond the destination is
    ignored, because buying there cannot help the vehicle arrive.

    Raises ``InfeasibleRouteError`` when a gap between usable stations exceeds
    the vehicle range.
    """
    if range_miles <= 0 or miles_per_gallon <= 0:
        raise ValueError("Range and fuel economy must both be positive.")

    tank_gallons = range_miles / miles_per_gallon
    fuel_miles = min(start_fuel_gallons, tank_gallons) * miles_per_gallon

    # The vehicle already carries enough fuel to arrive, so it buys nothing.
    if fuel_miles >= total_miles - POSITION_EPSILON_MILES:
        return FuelPlan()

    usable = sorted(
        (c for c in candidates if 0.0 <= c.position_miles <= total_miles),
        key=lambda c: (c.position_miles, c.price_per_gallon),
    )
    if not usable:
        raise InfeasibleRouteError(
            "No fuel station lies near this route, so the trip cannot be priced.",
            route_miles=round(total_miles, 1),
        )

    positions = [c.position_miles for c in usable]
    prices = [c.price_per_gallon for c in usable]
    next_cheaper = _next_cheaper(prices)
    window_minimum = _RangeMinimum(prices)

    plan = FuelPlan()
    # Index -1 is the origin. The vehicle cannot buy there, which an infinite
    # price expresses naturally: every real station is cheaper, so rule 1 always
    # moves the vehicle forward, and any purchase the rules demand at the origin
    # is impossible and surfaces as an explicit infeasibility.
    index = -1
    position = 0.0
    price = math.inf

    while True:
        remaining = total_miles - position
        if fuel_miles >= remaining - POSITION_EPSILON_MILES:
            break

        horizon = position + range_miles
        last = _last_reachable(positions, index + 1, horizon)
        destination_in_range = remaining <= range_miles + POSITION_EPSILON_MILES

        # Rule 1 only fires for a cheaper station that is actually reachable, so
        # the window bound is part of the test.
        cheaper = next_cheaper[index] if index >= 0 else index + 1
        has_cheaper = 0 <= cheaper <= last

        if has_cheaper:
            target_miles = positions[cheaper] - position
            next_index = cheaper
        elif destination_in_range:
            # Nothing cheaper ahead and the destination is within range, so buy
            # exactly enough to arrive. This is the terminal case, and it must be
            # tested before the reachability guard below: running out of stations
            # is only a failure when the destination is still too far away.
            target_miles = remaining
            next_index = None
        elif last < index + 1:
            gap_target = positions[index + 1] if index + 1 < len(positions) else total_miles
            raise InfeasibleRouteError(
                "The next fuel station is beyond the vehicle range.",
                at_mile=round(position, 1),
                next_station_mile=round(gap_target, 1),
                gap_miles=round(gap_target - position, 1),
                vehicle_range_miles=range_miles,
            )
        else:
            # Rule 2 -- fill the tank and press on to the cheapest pump in range.
            target_miles = range_miles
            next_index = window_minimum.argmin(index + 1, last)

        purchase_miles = target_miles - fuel_miles
        if purchase_miles > POSITION_EPSILON_MILES:
            if index < 0:
                raise InfeasibleRouteError(
                    "The vehicle starts with too little fuel to reach the first station.",
                    first_station_mile=round(positions[0], 1),
                    start_fuel_gallons=round(start_fuel_gallons, 2),
                    gallons_required=round(positions[0] / miles_per_gallon, 2),
                )
            gallons = purchase_miles / miles_per_gallon
            cost = gallons * price
            plan.purchases.append(
                Purchase(
                    candidate=usable[index],
                    gallons=gallons,
                    cost=cost,
                    arrival_fuel_gallons=fuel_miles / miles_per_gallon,
                    departure_fuel_gallons=target_miles / miles_per_gallon,
                )
            )
            plan.total_cost += cost
            plan.total_gallons += gallons
            fuel_miles = target_miles

        if next_index is None:
            break

        fuel_miles -= positions[next_index] - position
        index = next_index
        position = positions[index]
        price = prices[index]

        # Floating point drift only, never a real shortfall.
        if fuel_miles < 0.0:
            fuel_miles = 0.0

    return plan
