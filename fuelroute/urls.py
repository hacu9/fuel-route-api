from django.urls import path

from fuelroute import views

app_name = "fuelroute"

urlpatterns = [
    path("route/", views.plan_route, name="plan-route"),
    path("health/", views.health, name="health"),
    path("catalog/", views.catalog_stats, name="catalog"),
    path("map/", views.route_map, name="map"),
]
