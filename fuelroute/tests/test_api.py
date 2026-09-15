"""End-to-end tests for the HTTP surface.

The routing provider is mocked, so the suite is offline and deterministic. That
also lets each test assert exactly how many external calls a request makes,
which is one of the assessment's requirements.
"""

from __future__ import annotations

import json
import re

import numpy as np
import polyline
import pytest
import responses
from django.urls import reverse

from fuelroute.models import FuelStation, GeocodeCache

# The coordinates in the path depend on how each place geocodes, so match the
# provider by pattern rather than pinning the exact numbers.
OSRM_URL_PATTERN = re.compile(r"https://router\.project-osrm\.org/route/v1/driving/.*")
NOMINATIM_URL = re.compile(r"https://nominatim\.openstreetmap\.org/search.*")


def osrm_body(points, distance_meters, duration_seconds=36000.0):
    return {
        "code": "Ok",
        "routes": [
            {
                "geometry": polyline.encode(points, precision=6),
                "distance": distance_meters,
                "duration": duration_seconds,
                "legs": [],
            }
        ],
        "waypoints": [],
    }


@pytest.fixture
def mock_route():
    """A 600 mile line east along latitude 35, starting at Dallas' longitude."""
    lons = np.linspace(-96.8, -86.0, 600)
    points = [(35.0, float(lon)) for lon in lons]
    return points


@pytest.fixture
def priced_corridor(db):
    """Stations every 100 miles or so along latitude 35, cheapest in the middle."""
    specs = [(-96.6, 4.00), (-94.0, 3.50), (-91.0, 2.90), (-88.0, 3.80)]
    for index, (longitude, price) in enumerate(specs):
        FuelStation.objects.create(
            opis_id=f"S{index}", name=f"Stop {index}", address=f"{index} Road",
            city=f"Town{index}", state="TX", retail_price=price,
            latitude=35.0, longitude=longitude,
            geocode_source=FuelStation.GeocodeSource.GAZETTEER,
        )


@pytest.mark.django_db
@responses.activate
def test_a_plan_is_returned_with_one_external_call(client, mock_route, priced_corridor):
    responses.add(
        responses.GET, OSRM_URL_PATTERN,
        json=osrm_body(mock_route, 600 * 1609.344), status=200,
    )
    response = client.post(
        reverse("fuelroute:plan-route"),
        data=json.dumps({"start": "Dallas, TX", "finish": "35.0,-86.0"}),
        content_type="application/json",
    )
    assert response.status_code == 200, response.content
    body = response.json()

    assert body["route"]["distance_miles"] == pytest.approx(600.0, abs=0.5)
    assert body["meta"]["external_api_calls"]["total"] == 1
    assert body["meta"]["external_api_calls"]["geocoding"] == 0
    assert len(responses.calls) == 1

    plan = body["fuel_plan"]
    assert plan["stop_count"] >= 1
    # 600 miles at 10 mpg is 60 gallons, bought from an empty tank.
    assert plan["total_gallons_purchased"] == pytest.approx(60.0, abs=0.2)
    assert plan["fuel_consumed_gallons"] == pytest.approx(60.0, abs=0.2)
    assert plan["total_cost_usd"] > 0


@pytest.mark.django_db
@responses.activate
def test_the_plan_uses_the_cheapest_reachable_pumps(client, mock_route, priced_corridor):
    responses.add(responses.GET, OSRM_URL_PATTERN,
                  json=osrm_body(mock_route, 600 * 1609.344), status=200)
    body = client.post(
        reverse("fuelroute:plan-route"),
        data=json.dumps({"start": "Dallas, TX", "finish": "35.0,-86.0"}),
        content_type="application/json",
    ).json()

    prices = [stop["price_per_gallon"] for stop in body["fuel_plan"]["stops"]]
    gallons = [stop["gallons_purchased"] for stop in body["fuel_plan"]["stops"]]
    # The cheapest pump on the corridor must carry the largest single purchase.
    assert prices[gallons.index(max(gallons))] == min(prices)


@pytest.mark.django_db
@responses.activate
def test_a_full_tank_shorter_than_the_range_needs_no_stop(client, mock_route, priced_corridor):
    responses.add(responses.GET, OSRM_URL_PATTERN,
                  json=osrm_body(mock_route, 400 * 1609.344), status=200)
    body = client.post(
        reverse("fuelroute:plan-route"),
        data=json.dumps(
            {"start": "Dallas, TX", "finish": "35.0,-86.0", "start_fuel_gallons": 50}
        ),
        content_type="application/json",
    ).json()
    assert body["fuel_plan"]["stop_count"] == 0
    assert body["fuel_plan"]["total_cost_usd"] == 0


@pytest.mark.django_db
@responses.activate
def test_the_route_is_cached_so_a_repeat_costs_no_external_call(
    client, mock_route, priced_corridor
):
    responses.add(responses.GET, OSRM_URL_PATTERN,
                  json=osrm_body(mock_route, 600 * 1609.344), status=200)
    payload = json.dumps({"start": "Dallas, TX", "finish": "35.0,-86.0"})
    first = client.post(reverse("fuelroute:plan-route"), data=payload,
                        content_type="application/json").json()
    second = client.post(reverse("fuelroute:plan-route"), data=payload,
                         content_type="application/json").json()

    assert len(responses.calls) == 1
    assert second["meta"]["external_api_calls"]["total"] == 0
    assert second["meta"]["external_api_calls"]["routing_served_from_cache"] is True
    assert second["fuel_plan"]["total_cost_usd"] == first["fuel_plan"]["total_cost_usd"]


@pytest.mark.django_db
@responses.activate
def test_a_geocoder_lookup_is_cached_in_the_database(client, mock_route, priced_corridor):
    responses.add(responses.GET, NOMINATIM_URL, status=200,
                  json=[{"lat": "35.0", "lon": "-96.8", "display_name": "Somewhere, USA"}])
    responses.add(responses.GET, OSRM_URL_PATTERN,
                  json=osrm_body(mock_route, 600 * 1609.344), status=200)

    payload = json.dumps({"start": "somewhere odd", "finish": "35.0,-86.0"})
    first = client.post(reverse("fuelroute:plan-route"), data=payload,
                        content_type="application/json").json()
    assert first["meta"]["external_api_calls"]["geocoding"] == 1
    assert GeocodeCache.objects.filter(query="somewhere odd").exists()

    second = client.post(reverse("fuelroute:plan-route"), data=payload,
                         content_type="application/json").json()
    assert second["meta"]["external_api_calls"]["geocoding"] == 0
    assert second["start"]["resolved_by"] == "cache"


@pytest.mark.django_db
@responses.activate
def test_an_unroutable_pair_returns_422(client, priced_corridor):
    responses.add(responses.GET, OSRM_URL_PATTERN,
                  json={"code": "NoRoute", "message": "no route"}, status=200)
    response = client.post(
        reverse("fuelroute:plan-route"),
        data=json.dumps({"start": "Dallas, TX", "finish": "35.0,-86.0"}),
        content_type="application/json",
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "no_route"


@pytest.mark.django_db
@responses.activate
def test_a_provider_outage_returns_502(client, priced_corridor):
    responses.add(responses.GET, OSRM_URL_PATTERN,
                  json={}, status=503)
    response = client.post(
        reverse("fuelroute:plan-route"),
        data=json.dumps({"start": "Dallas, TX", "finish": "35.0,-86.0"}),
        content_type="application/json",
    )
    assert response.status_code == 502
    assert response.json()["error"]["code"] == "routing_failed"


@pytest.mark.django_db
def test_an_empty_catalogue_returns_503(client, db):
    with responses.RequestsMock() as mocked:
        mocked.add(responses.GET, OSRM_URL_PATTERN,
                   json=osrm_body([(35.0, -96.8), (35.0, -86.0)], 600 * 1609.344), status=200)
        response = client.post(
            reverse("fuelroute:plan-route"),
            data=json.dumps({"start": "Dallas, TX", "finish": "35.0,-86.0"}),
            content_type="application/json",
        )
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "catalog_empty"


@pytest.mark.django_db
def test_an_unknown_place_returns_422(client, priced_corridor):
    with responses.RequestsMock() as mocked:
        mocked.add(responses.GET, NOMINATIM_URL, json=[], status=200)
        response = client.post(
            reverse("fuelroute:plan-route"),
            data=json.dumps({"start": "zzzz nowhere zzzz", "finish": "Dallas, TX"}),
            content_type="application/json",
        )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "geocoding_failed"


@pytest.mark.django_db
def test_a_point_outside_the_united_states_returns_422(client, priced_corridor):
    response = client.post(
        reverse("fuelroute:plan-route"),
        data=json.dumps({"start": "48.85,2.35", "finish": "Dallas, TX"}),
        content_type="application/json",
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "outside_coverage"


@pytest.mark.django_db
@pytest.mark.parametrize(
    "payload",
    [
        {"start": "Dallas, TX"},
        {"start": "Dallas, TX", "finish": "Dallas, TX"},
        {"start": "Dallas, TX", "finish": "Chicago, IL", "start_fuel_gallons": 500},
        {"start": "Dallas, TX", "finish": "Chicago, IL", "start_fuel_gallons": -1},
        {"start": "", "finish": "Chicago, IL"},
    ],
)
def test_invalid_requests_are_rejected_with_400(client, payload):
    response = client.post(
        reverse("fuelroute:plan-route"),
        data=json.dumps(payload),
        content_type="application/json",
    )
    assert response.status_code == 400


@pytest.mark.django_db
def test_health_reports_the_catalogue_state(client, priced_corridor):
    response = client.get(reverse("fuelroute:health"))
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["stations_located"] == 4
    assert body["vehicle"]["miles_per_gallon"] == 10.0


@pytest.mark.django_db
def test_health_reports_an_empty_catalogue(client, db):
    response = client.get(reverse("fuelroute:health"))
    assert response.status_code == 503
    assert response.json()["status"] == "catalog_empty"


@pytest.mark.django_db
@responses.activate
def test_the_map_page_renders(client, mock_route, priced_corridor):
    responses.add(responses.GET, OSRM_URL_PATTERN,
                  json=osrm_body(mock_route, 600 * 1609.344), status=200)
    response = client.get(
        reverse("fuelroute:map"), {"start": "Dallas, TX", "finish": "35.0,-86.0"}
    )
    assert response.status_code == 200
    assert b"leaflet" in response.content.lower()
    assert b"LineString" in response.content
