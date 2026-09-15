from __future__ import annotations

import pytest

from fuelroute.services.gazetteer import (
    candidate_keys,
    is_available,
    lookup_city,
    lookup_zip,
    normalize_place,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Dallas city", "dallas"),
        ("Abanda CDP", "abanda"),
        ("St. Louis city", "saint louis"),
        ("Ft. Worth", "fort worth"),
        ("Mt. Vernon town", "mount vernon"),
        ("N. Little Rock", "north little rock"),
        ("Nashville-Davidson metropolitan government (balance)", "nashville davidson"),
        # Exactly one descriptor is stripped, so "Oklahoma City city" keeps the
        # "City" that belongs to the name itself.
        ("Oklahoma City city", "oklahoma city"),
        ("  OKLAHOMA   CITY  ", "oklahoma"),
    ],
)
def test_normalize_place(raw, expected):
    assert normalize_place(raw) == expected


def test_candidate_keys_cover_both_spellings():
    assert candidate_keys("Oklahoma City") == ["oklahoma city", "oklahoma"]


@pytest.mark.parametrize(
    ("city", "state"),
    [("Oklahoma City", "OK"), ("Kansas City", "MO"), ("Salt Lake City", "UT"),
     ("Jefferson City", "MO"), ("Sioux City", "IA"), ("Panama City", "FL")],
)
def test_a_city_whose_name_ends_in_city_is_not_truncated(city, state):
    """Greedy suffix stripping used to merge "X City" into a different town "X"."""
    assert lookup_city(city, state) is not None


def test_two_kansas_cities_resolve_to_different_states():
    missouri = lookup_city("Kansas City", "MO")
    kansas = lookup_city("Kansas City", "KS")
    assert missouri is not None and kansas is not None
    assert missouri != kansas


def test_the_gazetteer_ships_with_the_repository():
    assert is_available(), "data/us_places.csv.gz and data/us_zips.csv.gz must be committed"


@pytest.mark.parametrize(
    ("city", "state"),
    [("Dallas", "TX"), ("St. Louis", "MO"), ("Saint Louis", "MO"),
     ("Ft. Worth", "TX"), ("Albuquerque", "NM"), ("Effingham", "IL")],
)
def test_known_cities_resolve_offline(city, state):
    point = lookup_city(city, state)
    assert point is not None
    latitude, longitude = point
    assert 24.0 < latitude < 50.0
    assert -125.0 < longitude < -66.0


def test_an_unknown_city_returns_none():
    assert lookup_city("Nowhereville", "ZZ") is None
    assert lookup_city("", "TX") is None


def test_zip_lookup_tolerates_the_plus_four_suffix():
    assert lookup_zip("75201") == lookup_zip("75201-1234")
    assert lookup_zip("not a zip") is None
