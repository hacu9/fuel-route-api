"""Persistent data for the fuel route planner."""

from django.db import models


class FuelStation(models.Model):
    """One truck stop from the assessment price file, with resolved coordinates.

    The source file lists a postal address but no coordinates. ``latitude`` and
    ``longitude`` stay null until the ``geocode_stations`` command fills them in.
    Only rows with coordinates enter the route matcher.
    """

    class GeocodeSource(models.TextChoices):
        SOURCE_FILE = "source_file", "Source file"
        GAZETTEER = "gazetteer", "US Census gazetteer"
        NOMINATIM = "nominatim", "Nominatim"

    opis_id = models.CharField(max_length=32, db_index=True)
    name = models.CharField(max_length=255)
    address = models.CharField(max_length=255, blank=True)
    city = models.CharField(max_length=128, db_index=True)
    state = models.CharField(max_length=2, db_index=True)
    rack_id = models.CharField(max_length=32, blank=True)
    retail_price = models.DecimalField(max_digits=7, decimal_places=3)

    latitude = models.FloatField(null=True, blank=True)
    longitude = models.FloatField(null=True, blank=True)
    geocode_source = models.CharField(
        max_length=16, choices=GeocodeSource.choices, blank=True
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=["latitude", "longitude"], name="station_latlon_idx"),
            models.Index(fields=["state", "city"], name="station_state_city_idx"),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["opis_id", "city", "state", "name"],
                name="uniq_station_identity",
            )
        ]

    def __str__(self) -> str:
        return f"{self.name} ({self.city}, {self.state}) @ ${self.retail_price}"

    @property
    def is_located(self) -> bool:
        return self.latitude is not None and self.longitude is not None


class GeocodeCache(models.Model):
    """Resolved coordinates for a free-text place, so we call Nominatim once.

    A warm cache lets a request reach the routing provider with zero geocoder
    calls, which is what keeps the external call count at one.
    """

    query = models.CharField(max_length=255, unique=True)
    latitude = models.FloatField()
    longitude = models.FloatField()
    display_name = models.CharField(max_length=512, blank=True)
    provider = models.CharField(max_length=32, default="nominatim")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name_plural = "geocode cache entries"

    def __str__(self) -> str:
        return f"{self.query} -> ({self.latitude}, {self.longitude})"
