from __future__ import annotations

from decimal import Decimal
from io import StringIO

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from fuelroute.management.commands.import_fuel_prices import (
    detect_columns,
    parse_price,
)
from fuelroute.models import FuelStation

ASSESSMENT_HEADERS = [
    "OPIS Truckstop ID", "Truckstop Name", "Address",
    "City", "State", "Rack ID", "Retail Price",
]


def test_it_recognises_the_assessment_headers():
    mapping = detect_columns(ASSESSMENT_HEADERS)
    assert mapping["opis_id"] == "OPIS Truckstop ID"
    assert mapping["name"] == "Truckstop Name"
    assert mapping["city"] == "City"
    assert mapping["state"] == "State"
    assert mapping["retail_price"] == "Retail Price"


def test_it_recognises_renamed_headers():
    mapping = detect_columns(["site_id", "station name", "town", "province", "cost"])
    assert mapping["opis_id"] == "site_id"
    assert mapping["city"] == "town"
    assert mapping["state"] == "province"
    assert mapping["retail_price"] == "cost"


def test_one_header_is_never_claimed_twice():
    mapping = detect_columns(ASSESSMENT_HEADERS)
    assert len(set(mapping.values())) == len(mapping)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("3.250", Decimal("3.250")), ("$3.25", Decimal("3.25")),
     (" 3.25 ", Decimal("3.25")), ("", None), ("n/a", None), ("0", None), ("-1", None)],
)
def test_parse_price(raw, expected):
    assert parse_price(raw) == expected


@pytest.fixture
def price_csv(tmp_path):
    path = tmp_path / "prices.csv"
    path.write_text(
        "OPIS Truckstop ID,Truckstop Name,Address,City,State,Rack ID,Retail Price\n"
        "1,Alpha Stop,1 Road,Dallas,TX,10,3.500\n"
        "2,Beta Stop,2 Road,Effingham,IL,11,3.100\n"
        "3,Bad Row,3 Road,,TX,12,3.000\n"
        "4,No Price,4 Road,Austin,TX,13,\n"
        "1,Alpha Stop,1 Road,Dallas,TX,20,3.200\n",
        encoding="utf-8",
    )
    return path


@pytest.mark.django_db
def test_import_skips_unusable_rows_and_keeps_the_cheapest_duplicate(price_csv):
    out = StringIO()
    call_command("import_fuel_prices", str(price_csv), stdout=out)

    assert FuelStation.objects.count() == 2
    alpha = FuelStation.objects.get(opis_id="1")
    # The same site listed twice collapses to the cheaper rack price.
    assert alpha.retail_price == Decimal("3.200")
    assert "skipped 2" in out.getvalue()


@pytest.mark.django_db
def test_import_then_geocode_locates_every_station(price_csv):
    call_command("import_fuel_prices", str(price_csv), stdout=StringIO())
    assert FuelStation.objects.filter(latitude__isnull=True).count() == 2

    call_command("geocode_stations", stdout=StringIO())
    assert FuelStation.objects.filter(latitude__isnull=True).count() == 0
    assert all(
        station.geocode_source == FuelStation.GeocodeSource.GAZETTEER
        for station in FuelStation.objects.all()
    )


@pytest.mark.django_db
def test_a_file_without_a_price_column_is_rejected(tmp_path):
    path = tmp_path / "bad.csv"
    path.write_text("City,State\nDallas,TX\n", encoding="utf-8")
    with pytest.raises(CommandError, match="retail_price"):
        call_command("import_fuel_prices", str(path), stdout=StringIO())


@pytest.mark.django_db
def test_a_missing_file_is_rejected():
    with pytest.raises(CommandError, match="No such file"):
        call_command("import_fuel_prices", "/nonexistent/prices.csv", stdout=StringIO())
