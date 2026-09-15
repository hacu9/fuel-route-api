"""In-memory snapshot of the fuel station catalogue.

The price file holds a few thousand rows and changes only when an operator
re-imports it, so the whole catalogue lives in process memory as numpy arrays.
A request therefore never touches the database to price a route.

The snapshot carries a cheap fingerprint -- the row count and the newest
``updated_at`` -- which is re-checked per request. An import invalidates the
snapshot automatically, with no restart and no manual cache flush.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass

import numpy as np
from django.db.models import Count, Max

from fuelroute.models import FuelStation

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_snapshot: StationCatalog | None = None


@dataclass(frozen=True, slots=True)
class StationCatalog:
    """Located stations as parallel arrays, ordered as loaded."""

    ids: np.ndarray
    latitudes: np.ndarray
    longitudes: np.ndarray
    prices: np.ndarray
    names: list[str]
    addresses: list[str]
    cities: list[str]
    states: list[str]
    opis_ids: list[str]
    fingerprint: tuple[int, str]

    def __len__(self) -> int:
        return int(self.ids.size)

    def record(self, index: int) -> dict:
        """The station at ``index`` as a plain dictionary."""
        return {
            "id": int(self.ids[index]),
            "opis_id": self.opis_ids[index],
            "name": self.names[index],
            "address": self.addresses[index],
            "city": self.cities[index],
            "state": self.states[index],
            "latitude": float(self.latitudes[index]),
            "longitude": float(self.longitudes[index]),
            "price_per_gallon": float(self.prices[index]),
        }


def _fingerprint() -> tuple[int, str]:
    """Cheap signature of the located rows, checked once per request."""
    stats = FuelStation.objects.filter(
        latitude__isnull=False, longitude__isnull=False
    ).aggregate(
        located=Count("id"), latest=Max("updated_at")
    )
    return (stats["located"] or 0, str(stats["latest"]))


def _load() -> StationCatalog:
    # Read the fingerprint first. If an import commits between this call and the
    # row fetch, the fingerprint is older than the rows, so the next request
    # sees a mismatch and reloads. Fingerprinting afterwards would stamp stale
    # rows as current and serve them until the table changed again.
    fingerprint = _fingerprint()
    rows = list(
        FuelStation.objects.filter(latitude__isnull=False, longitude__isnull=False)
        .values_list(
            "id", "latitude", "longitude", "retail_price",
            "name", "address", "city", "state", "opis_id",
        )
    )
    count = len(rows)
    logger.info("Loading %d located fuel stations into memory", count)
    return StationCatalog(
        ids=np.fromiter((r[0] for r in rows), dtype=np.int64, count=count),
        latitudes=np.fromiter((r[1] for r in rows), dtype=np.float64, count=count),
        longitudes=np.fromiter((r[2] for r in rows), dtype=np.float64, count=count),
        prices=np.fromiter((float(r[3]) for r in rows), dtype=np.float64, count=count),
        names=[r[4] for r in rows],
        addresses=[r[5] for r in rows],
        cities=[r[6] for r in rows],
        states=[r[7] for r in rows],
        opis_ids=[r[8] for r in rows],
        fingerprint=fingerprint,
    )


def get_catalog() -> StationCatalog:
    """Return the cached catalogue, reloading it when the table has changed."""
    global _snapshot
    current = _fingerprint()
    snapshot = _snapshot
    if snapshot is not None and snapshot.fingerprint == current:
        return snapshot
    with _lock:
        if _snapshot is None or _snapshot.fingerprint != current:
            _snapshot = _load()
        return _snapshot


def invalidate() -> None:
    """Drop the cached catalogue. Used by the import command and by tests."""
    global _snapshot
    with _lock:
        _snapshot = None
