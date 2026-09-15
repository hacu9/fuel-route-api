# Architecture

## A request, end to end

```
POST /api/v1/route/  {"start": "Dallas, TX", "finish": "Chicago, IL"}
      |
      v
  serializers.RoutePlanSerializer      validate the input
      |
      v
  services/geocoding.resolve()         "Dallas, TX" -> (32.79, -96.77)
      |                                0 network calls: offline gazetteer
      v
  services/routing.OSRMClient.route()  THE one external call
      |                                returns 9144 polyline vertices
      v                                plus the distance to each one
  services/catalog.get_catalog()       8000 stations, already in memory
      |
      v
  services/matcher.match_stations()    bounding box, then a k-d tree
      |                                318 stations within 15 mi of the road
      v
  services/matcher.prune_dominated()   318 -> 55 usable candidates
      |
      v
  services/optimizer.plan_fuel_stops() the two rules; 3 stops, $246.43
      |
      v
  JSON
```

Everything below the routing call is local. That is the whole design.

## Why each piece is where it is

### The offline gazetteer

The price file names a city and a state but carries no coordinates, and a
station that cannot be placed cannot be used. Geocoding 8000 addresses through a
public geocoder would take over two hours and would breach its usage policy.

So the repository ships two derived extracts of the **US Census Bureau 2023
Gazetteer**, which is public domain:

* `data/us_places.csv.gz` — 32104 places, 457 KB
* `data/us_zips.csv.gz` — 33791 ZIP areas, 369 KB

`geocode_stations` places the whole catalogue from these in seconds, with no
network call. The same files resolve a caller's `"Dallas, TX"`, which is why an
ordinary request reaches the routing provider having made no geocoding request
at all.

`build_gazetteer` regenerates them. It imports `normalize_place` from the
runtime module rather than reimplementing it, so the keys written at build time
and the keys looked up at request time cannot drift apart. They did drift during
development, and `St. Louis, MO` stopped resolving.

### The in-memory catalogue

Prices change when an operator re-imports the file, not per request. The
catalogue therefore lives in process memory as numpy arrays.

It carries a fingerprint — the located row count and the newest `updated_at` —
which is re-checked per request. An import invalidates the snapshot
automatically. There is no restart and no manual cache flush.

### Matching stations to the road

1. **Bounding box.** One vectorised comparison drops every station outside the
   route's box padded by the detour radius. 8000 becomes a few hundred.
2. **Resample.** The polyline is thinned to one vertex every 250 m, which bounds
   the work on a route with dense urban geometry.
3. **k-d tree.** Each surviving station finds its nearest route vertex. That
   vertex's cumulative distance becomes the station's position along the route.

Step 3 measures to the nearest *vertex*, not the nearest point on a *segment*.
At 250 m spacing the largest possible error is 125 m, against a detour radius
measured in miles.

The cumulative distances are integrated locally with the haversine formula. On
Dallas to Chicago that sum reproduces the road distance OSRM reports —
966.6 miles — to the tenth of a mile, which is a useful check that the geometry
and the reported distance describe the same road.

### Why OSRM

The brief asks for a free map and routing API. The public OSRM demo server needs
no API key and no signup, so this repository runs for a reviewer with nothing
but `uv sync`.

It is a demo server with no uptime guarantee. `OSRM_BASE_URL` points at your own
container for real traffic:

```bash
docker run -p 5000:5000 osrm/osrm-backend osrm-routed /data/us-latest.osrm
```

Nothing else changes.

### Error handling

Every failure the planner understands is a subclass of `PlannerError` carrying
its own status code, and a single DRF exception handler renders them all. A
caller can tell a bad request from an unroutable pair from a provider outage
without parsing prose.

## Data model

**`FuelStation`** — one row per site. `latitude` and `longitude` are nullable,
because a station exists in the table before it is placed; only located rows
enter the matcher. A unique constraint on `(opis_id, city, state, name)` makes
the import idempotent, and a file listing one site once per fuel rack collapses
to the cheapest row.

**`GeocodeCache`** — resolved coordinates for a free-text place, so an unusual
place name costs one network call ever rather than one per request.

## What I would change with more time

* **Run OSRM locally.** It is the entire latency budget: 700 to 1400 ms against
  6 to 70 ms of local computation. A container on the same host would cut a
  request to well under 100 ms cold.
* **Geocode to the street address, not the city.** The gazetteer places a
  station at its city centre, so a `detour_miles` figure is accurate to within a
  few miles. That is immaterial for choosing stops against a 500 mile range, but
  it is wrong for turn-by-turn directions to the pump.
* **Persist the route cache.** It is per-process today, so it does not survive a
  restart and is not shared between workers. Redis would fix both.
* **Carry a price date.** The file has no timestamps, so every price is treated
  as current. Real prices move daily.
