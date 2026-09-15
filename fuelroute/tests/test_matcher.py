from __future__ import annotations

import numpy as np
import pytest

from fuelroute.services.catalog import get_catalog
from fuelroute.services.geo import cumulative_miles, haversine_miles
from fuelroute.services.matcher import MatchedStation, match_stations, prune_dominated
from fuelroute.services.routing import Route


def straight_route(start_lon=-100.0, end_lon=-95.0, latitude=35.0, points=400):
    lons = np.linspace(start_lon, end_lon, points)
    lats = np.full(points, latitude)
    cumulative = cumulative_miles(lats, lons)
    return Route(
        lats=lats, lons=lons, cumulative_miles=cumulative,
        total_miles=float(cumulative[-1]), duration_hours=float(cumulative[-1]) / 60.0,
        encoded_polyline="",
    )


def test_cumulative_distance_tracks_the_path_not_the_shortcut():
    """A line of constant latitude is longer than the great circle beneath it.

    Summing the polyline must therefore give slightly more than the direct
    distance between the endpoints, and the excess must stay tiny.
    """
    route = straight_route()
    direct = haversine_miles(
        np.array([35.0]), np.array([-100.0]), np.array([35.0]), np.array([-95.0])
    )[0]
    assert route.total_miles > direct
    assert route.total_miles == pytest.approx(direct, rel=1e-3)


@pytest.mark.django_db
def test_stations_on_the_route_are_matched_and_placed(corridor_stations):
    route = straight_route()
    matched = match_stations(route, get_catalog(), max_detour_miles=15.0)

    assert len(matched) == 5
    positions = sorted(m.position_miles for m in matched)
    # Stations sit one degree of longitude apart at latitude 35, about 57 miles.
    assert positions[0] == pytest.approx(0.0, abs=1.0)
    for earlier, later in zip(positions, positions[1:], strict=False):
        assert later - earlier == pytest.approx(56.9, abs=1.5)


@pytest.mark.django_db
def test_a_station_beyond_the_detour_radius_is_excluded(corridor_stations):
    from fuelroute.models import FuelStation
    from fuelroute.services import catalog

    FuelStation.objects.create(
        opis_id="FAR", name="Far Stop", city="Faraway", state="TX",
        retail_price=1.00, latitude=38.0, longitude=-98.0,
        geocode_source=FuelStation.GeocodeSource.GAZETTEER,
    )
    catalog.invalidate()
    matched = match_stations(straight_route(), get_catalog(), max_detour_miles=15.0)
    assert len(matched) == 5
    assert all(m.detour_miles <= 15.0 for m in matched)


@pytest.mark.django_db
def test_an_empty_catalogue_matches_nothing(db):
    assert match_stations(straight_route(), get_catalog(), max_detour_miles=15.0) == []


def test_pruning_keeps_the_cheapest_of_a_close_pair():
    stations = [
        MatchedStation(0, 100.0, 1.0, 3.50),
        MatchedStation(1, 102.0, 1.0, 3.20),
        MatchedStation(2, 300.0, 1.0, 3.90),
    ]
    kept = prune_dominated(stations, min_spacing_miles=10.0)
    assert [s.catalog_index for s in kept] == [1, 2]


def test_pruning_leaves_well_spaced_stations_alone():
    stations = [MatchedStation(i, i * 50.0, 1.0, 3.0 + i) for i in range(5)]
    assert prune_dominated(stations, 10.0) == stations


def test_pruning_can_be_switched_off():
    stations = [MatchedStation(0, 100.0, 1.0, 3.5), MatchedStation(1, 100.5, 1.0, 3.2)]
    assert prune_dominated(stations, 0.0) == stations


def test_pruned_output_stays_in_route_order():
    stations = [MatchedStation(i, i * 3.0, 1.0, 5.0 - i * 0.1) for i in range(40)]
    kept = prune_dominated(stations, 10.0)
    assert kept == sorted(kept, key=lambda s: s.position_miles)
