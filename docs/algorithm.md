# The fuel stop optimizer

## The problem

A vehicle drives a fixed route of `D` miles. It holds 50 gallons, burns a gallon
every 10 miles, and so travels at most 500 miles on a full tank. Stations sit at
known distances along the route, each with its own price per gallon. Choose
where to stop and how much to buy at each stop, so the vehicle arrives and the
bill is as small as possible.

This is the **gas station problem** with a single fixed tank size.

## The solution

Two rules produce the optimum.

* **Rule 1 — if a cheaper station is within range, buy only enough fuel to reach
  it.** Never carry expensive fuel past a cheaper pump.
* **Rule 2 — if no cheaper station is within range, fill the tank here and drive
  to the cheapest station that is still reachable.**

The destination is a terminal case. Once it is within range, the vehicle buys
exactly enough to arrive and nothing more.

Khuller, Malekian and Mestre proved this greedy optimal for the uniform tank
case in *To Fill or Not to Fill: The Gas Station Problem*, ACM Transactions on
Algorithms, 2011.

### Why the rules are right

Suppose rule 1 is broken: the vehicle buys a gallon at station `A` for $4.00 and
still has it in the tank when it passes station `B`, where fuel costs $3.00.
Buy one gallon less at `A` and one more at `B`. The vehicle reaches exactly the
same places with exactly the same fuel, and the trip costs a dollar less. So an
optimal plan never does this.

Rule 2 follows from the same exchange. If nothing cheaper is reachable, every
gallon bought here is cheaper than every gallon that could replace it, so the
vehicle should carry as much as it can.

## The implementation

`fuelroute/services/optimizer.py`, in O(n log n):

* a **monotonic stack** computes, for every station, the next station to its
  right with a lower price. That answers rule 1 in constant time.
* a **sparse table** answers "which station in this window is cheapest" in
  constant time. That answers rule 2.
* a **binary search** finds the last station within range of the current one.

A route across the United States yields a few thousand candidates, so a plan is
built in well under a millisecond. The cost is dominated by the routing
provider, never by the optimizer.

### The origin

The vehicle cannot buy fuel at the origin, only at a station. The optimizer
models the origin as a station priced at infinity. Every real station is then
cheaper, so rule 1 always moves the vehicle forward, and any purchase the rules
would demand at the origin is impossible by construction and surfaces as an
explicit infeasibility rather than a silently wrong answer.

The planner handles the practical consequence: with an empty tank the vehicle
cannot reach a pump 30 miles away. It nominates the cheapest station near the
origin as the departure fill-up and prices it at mile zero. That stop is flagged
`is_origin_fill` in the response, so the choice is visible.

## How the optimality claim is verified

Reasoning about an algorithm is not evidence that the code implements it. The
test suite checks the implementation against an **independent exhaustive
search** on random instances.

The exhaustive solver explores every reachable sequence of stops. At each stop
it departs either with a full tank or with exactly enough fuel to reach the next
stop — an exchange argument shows one of those is always optimal, so the search
cannot miss the optimum. It shares no code with the greedy.

`test_greedy_matches_an_exhaustive_search` runs 25 seeded instances. During
development the same harness ran 400 instances, and a second solver that
discretises the tank into quarter-mile units — and therefore assumes nothing
about the structure of an optimal solution — cross-checked 60 more.

**This found two real defects:**

1. At the last station on a route, with the destination within range, the
   planner declared the trip infeasible. The reachability guard ran before the
   destination check, so running out of stations was treated as failure even
   when no more stations were needed.
2. A test case asserting the tank is never overfilled was itself infeasible: it
   placed the second station 600 miles from the first, beyond a 500 mile range.

Neither would have been caught by reading the code.

## The invariant suite

A cost comparison cannot catch a plan that is cheap because it is impossible.
`test_invariants.py` therefore replays each plan as a drive and asserts the
physics: stops advance along the route, the tank never goes below empty at any
stop or at the finish, it never exceeds 50 gallons, no leg exceeds the range,
and the gallons and costs add up.

**This found a third defect.** The departure fill-up is relocated to mile zero,
but stations the vehicle had already passed stayed selectable, so a plan could
put its second stop *behind* its first. The suite reported stop markers of
`[34.5, 19.6, 53.9, ...]` -- a route that goes backwards.

A fourth came from an independent review: a station 15 miles off the road costs
30 miles of driving that the route geometry does not contain, so a 490 mile gap
inside a 500 mile range really needs 520 and the tank runs 0.5 gallons short.
See the detour section in [architecture.md](architecture.md).

Every fix above is pinned by a test that was confirmed to fail when the fix is
reverted. A test that stays green without the fix is not coverage.

## Where the result is deliberately not the pure optimum

The unconstrained optimum will stop twice within a mile to save a fraction of a
cent, because the rules are indifferent to how much trouble a stop is. A driver
is not indifferent, and a stop that buys a tenth of a gallon is not a plan
anybody would follow.

`prune_dominated` therefore drops a station when a cheaper one sits within
`min_stop_spacing_miles` (default 10). Each survivor is the cheapest pump in its
own neighbourhood.

This narrows the search space, so the answer can cost marginally more than the
unconstrained optimum. Two things keep that honest:

* the applied spacing is reported in `meta.min_stop_spacing_miles`;
* passing `"min_stop_spacing_miles": 0` disables it entirely.

Pruning can in principle remove the one station bridging a long empty stretch.
If the narrowed set turns out to be infeasible, the planner retries against
every matched station before reporting failure.
