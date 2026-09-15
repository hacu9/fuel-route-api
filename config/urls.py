from django.contrib import admin
from django.shortcuts import redirect
from django.urls import include, path

urlpatterns = [
    path("", lambda request: redirect("/api/v1/health/", permanent=False)),
    path("admin/", admin.site.urls),
    path("api/v1/", include("fuelroute.urls", namespace="fuelroute")),
]
