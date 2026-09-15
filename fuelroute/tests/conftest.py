from __future__ import annotations

import numpy as np
import polyline
import pytest
from django.core.cache import cache

from fuelroute.models import FuelStation
from fuelroute.services import catalog


@pytest.fixture(autouse=True)
def clear_caches():
    """Each test starts with an empty catalogue and an empty route cache.

    Both caches live in process memory and would otherwise leak between tests:
    a route cached by one test would stop the next from calling the provider.
    """
    catalog.invalidate()
    cache.clear()
    yield
    catalog.invalidate()
    cache.clear()


@pytest.fixture
def corridor_stations(db):
    """Five stations spaced along a straight west-to-east line at latitude 35."""
    prices = [3.50, 3.20, 4.10, 2.80, 3.90]
    stations = [
        FuelStation.objects.create(
            opis_id=f"T{index}",
            name=f"Test Stop {index}",
            address=f"{index} Test Road",
            city=f"Town{index}",
            state="TX",
            retail_price=price,
            latitude=35.0,
            longitude=-100.0 + index * 1.0,
            geocode_source=FuelStation.GeocodeSource.GAZETTEER,
        )
        for index, price in enumerate(prices)
    ]
    return stations


@pytest.fixture
def straight_route_polyline():
    """An encoded polyline running east along latitude 35, one point per mile."""
    longitudes = np.linspace(-100.0, -95.0, 400)
    points = [(35.0, float(lon)) for lon in longitudes]
    return polyline.encode(points, precision=6)
