"""HTTP surface.

``POST /api/v1/route/`` and ``GET /api/v1/route/`` both plan a route. The GET
form exists so a plan can be opened straight from a browser or a Loom demo
without a body, and so the map page can link to its own JSON.
"""

from __future__ import annotations

import json

from django.conf import settings
from django.http import HttpResponse
from django.shortcuts import render
from django.views.decorators.http import require_GET
from rest_framework import status
from rest_framework.decorators import api_view
from rest_framework.response import Response

from fuelroute.models import FuelStation
from fuelroute.serializers import RoutePlanSerializer
from fuelroute.services import planner
from fuelroute.services.catalog import get_catalog


@api_view(["GET", "POST"])
def plan_route(request):
    """Plan a route and price its fuel stops."""
    payload = request.data if request.method == "POST" else request.query_params
    serializer = RoutePlanSerializer(data=payload)
    serializer.is_valid(raise_exception=True)
    data = serializer.validated_data

    result = planner.plan(
        start_query=data["start"],
        finish_query=data["finish"],
        start_fuel_gallons=data.get("start_fuel_gallons"),
        max_detour_miles=data.get("max_detour_miles"),
        min_stop_spacing_miles=data.get("min_stop_spacing_miles"),
    )
    return Response(result.payload, status=status.HTTP_200_OK)


@api_view(["GET"])
def health(request):
    """Readiness probe that also reports how much of the catalogue is usable."""
    total = FuelStation.objects.count()
    located = FuelStation.objects.filter(latitude__isnull=False).count()
    ready = located > 0
    return Response(
        {
            "status": "ok" if ready else "catalog_empty",
            "stations_total": total,
            "stations_located": located,
            "vehicle": {
                "max_range_miles": settings.FUEL_ROUTE["MAX_RANGE_MILES"],
                "miles_per_gallon": settings.FUEL_ROUTE["MILES_PER_GALLON"],
            },
        },
        status=status.HTTP_200_OK if ready else status.HTTP_503_SERVICE_UNAVAILABLE,
    )


@require_GET
def route_map(request):
    """Render the planned route and its fuel stops on a Leaflet map.

    This is a plain Django view, so the inputs come from ``request.GET``.
    """
    serializer = RoutePlanSerializer(data=request.GET.dict())
    if not serializer.is_valid():
        return render(
            request,
            "fuelroute/map.html",
            {"error": json.dumps(serializer.errors), "plan_json": "null"},
            status=400,
        )

    data = serializer.validated_data
    result = planner.plan(
        start_query=data["start"],
        finish_query=data["finish"],
        start_fuel_gallons=data.get("start_fuel_gallons"),
        max_detour_miles=data.get("max_detour_miles"),
        min_stop_spacing_miles=data.get("min_stop_spacing_miles"),
    )

    payload = dict(result.payload)
    payload["route"] = dict(payload["route"])
    payload["route"]["geojson"] = result.route.geojson()
    return render(
        request,
        "fuelroute/map.html",
        {"plan_json": json.dumps(payload), "error": ""},
    )


@require_GET
def catalog_stats(request):
    """A quick look at what is loaded, useful while demonstrating the API."""
    catalog = get_catalog()
    prices = catalog.prices
    body = {
        "stations_located": len(catalog),
        "price_per_gallon": {
            "min": round(float(prices.min()), 3) if len(catalog) else None,
            "max": round(float(prices.max()), 3) if len(catalog) else None,
            "mean": round(float(prices.mean()), 3) if len(catalog) else None,
        },
        "states": sorted(set(catalog.states)),
    }
    return HttpResponse(json.dumps(body, indent=2), content_type="application/json")
