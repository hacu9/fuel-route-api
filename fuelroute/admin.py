from django.contrib import admin

from .models import FuelStation, GeocodeCache


@admin.register(FuelStation)
class FuelStationAdmin(admin.ModelAdmin):
    list_display = ("name", "city", "state", "retail_price", "latitude", "longitude")
    list_filter = ("state", "geocode_source")
    search_fields = ("name", "city", "opis_id")


@admin.register(GeocodeCache)
class GeocodeCacheAdmin(admin.ModelAdmin):
    list_display = ("query", "latitude", "longitude", "provider", "created_at")
    search_fields = ("query",)
