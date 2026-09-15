# Loom recording guide

Five minutes is the cap, and it is tight. The plan below runs to about 4:40 and
covers both halves of what the brief asks for: the API working in Postman, and a
quick tour of the code.

## Verified routes

Run against the real price file. Use one of these on camera. The figures are
what the API returns today, so you can say them out loud before they appear.

| Route | Miles | Stops | Total |
|---|---:|---:|---:|
| Dallas, TX to Chicago, IL | 961 | 6 | $289.43 |
| Los Angeles, CA to New York, NY | 2811 | 17 | $899.12 |
| Seattle, WA to Miami, FL | 3303 | 20 | $1,050.80 |
| New York, NY to Chicago, IL | 796 | 4 | $255.36 |
| Denver, CO to Chicago, IL | 1003 | 9 | $304.36 |

**Two routes refuse, and both are correct.** Phoenix to Portland and Los Angeles
to Seattle return `422 infeasible_route`, because the price file has no station
at all across Nevada and eastern Oregon -- a 1008 mile gap against a 500 mile
range. The refusal is the right answer, but do not discover it live. If you want
to show a refusal, use Honolulu to Los Angeles instead: `no_route` is instantly
understandable and needs no explanation about data coverage.

## Before you hit record

```bash
cd ~/code/backend-djano

# 1. Load the real price file.
uv run python manage.py import_fuel_prices data/fuel-prices-for-be-assessment.csv --replace
uv run python manage.py geocode_stations

# Optional, about four minutes, and it needs the network. It lifts US coverage
# from 96.7 to 99.6 percent by sending the few hundred unlisted crossroads to
# the public geocoder. The route figures above already assume you ran it.
uv run python manage.py geocode_stations --use-nominatim

# 2. Start the server.
uv run python manage.py runserver
```

Then, in a second terminal, warm the demo route so the recording does not stall
on the public OSRM server:

```bash
curl -s -X POST http://127.0.0.1:8000/api/v1/route/ \
  -H 'Content-Type: application/json' \
  -d '{"start":"Dallas, TX","finish":"Chicago, IL"}' > /dev/null
```

Import `docs/fuel-route-api.postman_collection.json` into Postman. Open these
tabs and nothing else:

1. Postman, with the collection expanded
2. A browser on `http://127.0.0.1:8000/api/v1/map/?start=Dallas,+TX&finish=Chicago,+IL`
3. Your editor, with `fuelroute/services/optimizer.py` already open

Close Slack, email and notifications. Record the screen at 1440x900 or larger so
the JSON is readable.

---

## The script

### 0:00 - 0:20  What it is

> "This is a Django 6.1 API. You give it a start and a finish in the US, and it
> returns the route, the cheapest places to buy fuel for a 500 mile range at
> 10 miles per gallon, and the total fuel bill. It's running on the price file
> you sent - about seven thousand truck stops."

Do not read out the brief. They wrote it.

### 0:20 - 1:30  The main request

Run **2. Dallas to Chicago**. While it returns, say what to look at:

> "961 miles, six stops, and there's the total."

Then scroll to `meta` and stop there. This is the part they are checking.

> "Two things I want to point out. `external_api_calls` is **one** - the routing
> provider, and nothing else. And `timing_ms` splits the provider's time from my
> own: local computation is about six milliseconds, the rest is the public OSRM
> demo server."

Run **3. Same route again**.

> "Second time, zero external calls and single digit milliseconds. Routes are
> cached."

### 1:30 - 2:10  The map

Switch to the browser tab and reload.

> "Same plan on a map. Three numbered stops, the price and the gallons at each."

Hover one marker so the popup opens. Keep this short - it is the easy part.

### 2:10 - 2:40  It holds up

Run **4. Los Angeles to New York**.

> "Coast to coast, 2811 miles, seventeen stops, still one external call."

Run **8. Outside the USA** or **9. No drivable route**.

> "Errors are typed, not prose - a code, a message and a status you can branch on."

### 2:40 - 4:00  The code

Switch to the editor. Show **three files only**. Resist opening more.

**`fuelroute/services/optimizer.py`**

> "This is the interesting part. It's the gas station problem, and the optimum is
> two rules: if a cheaper station is in range, buy just enough to reach it -
> never carry expensive fuel past a cheaper pump. If nothing cheaper is in range,
> fill up and go to the cheapest one you can reach. Monotonic stack for the first
> rule, sparse table for the second, so it's O(n log n) and runs in under a
> millisecond."

**`fuelroute/tests/test_optimizer.py`** - scroll to the bottom.

> "I didn't want to just assert that it's optimal, so this checks the greedy
> against an independent exhaustive search on random instances. It found a real
> bug: at the last station with the destination in range, it was declaring the
> route infeasible."

**`fuelroute/services/gazetteer.py`** - just the docstring.

> "The one-call number comes from here. The price file has no coordinates, and
> its address column is a highway exit, not a street address. So rather than push
> seven thousand rows through a public geocoder, the repo ships a US Census
> gazetteer and places 96.7 percent of them offline. The same file resolves the
> caller's 'Dallas, TX', which is why a normal request never calls a geocoder at
> all."

### 4:00 - 4:40  Close

> "170 tests, all offline - the routing provider and the geocoder are both
> mocked. I also ran the whole thing past a second, non-Anthropic model as a
> blind review, and it found real defects I'd missed - the biggest was that I
> wasn't accounting for the fuel burned driving off the route to a station and
> back, which made one range calculation optimistic enough to run the tank dry.
> That's fixed and pinned by a test. The README documents every assumption, and
> the two docs files cover the algorithm and the architecture."

Stop. Do not add a summary of the summary.

---

## What not to do

* Do not run `pytest` on camera. It is ten seconds of scrolling dots and it
  proves nothing a reviewer can read at speed. Say the number instead.
* Do not walk the file tree. Three files, named and purposeful.
* Do not apologise for the timing, explain the delay, or mention the deadline.
  If they raise it, answer then.
* Do not read the JSON field by field. Point at `meta` and move.
* Do not open `planner.py`. It is orchestration and it will eat ninety seconds.

## If a question comes up afterwards

* **"Why OSRM?"** Free, no API key, so the repo runs for a reviewer with nothing
  but `uv sync`. `OSRM_BASE_URL` points at your own container in production, and
  that is also the whole latency budget.
* **"Why does the tank start empty?"** So the total is the cost of fuel for the
  journey, not the cost of topping up a tank somebody else paid for. Pass
  `start_fuel_gallons` for the other reading. It is assumption 1 in the README.
* **"Is it really optimal?"** Yes, against the candidate set, and it is checked
  against an exhaustive search. The default 10 mile stop spacing narrows that set
  deliberately, because the pure optimum will stop twice in a mile to save a
  fraction of a cent. Worst case I could construct, it costs 0.9 percent more;
  on real routes it is 0.009 percent. Pass 0 to switch it off.
