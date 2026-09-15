"""Request validation for the planning endpoint."""

from __future__ import annotations

from django.conf import settings
from rest_framework import serializers


class RoutePlanSerializer(serializers.Serializer):
    """Inputs for a fuel-optimised route.

    ``start`` and ``finish`` accept a city and state ("Dallas, TX"), a five
    digit ZIP, a "latitude,longitude" pair, or any free-text US place name.
    The first three resolve offline and cost no external call.
    """

    start = serializers.CharField(max_length=255, trim_whitespace=True)
    finish = serializers.CharField(max_length=255, trim_whitespace=True)
    start_fuel_gallons = serializers.FloatField(
        required=False,
        allow_null=True,
        min_value=0.0,
        help_text=(
            "Fuel already in the tank at the origin. Defaults to 0, which "
            "prices the whole journey. Pass the tank size to price only the "
            "top-ups a vehicle that left with a full tank would need."
        ),
    )
    max_detour_miles = serializers.FloatField(
        required=False,
        allow_null=True,
        min_value=0.0,
        max_value=100.0,
        help_text="How far off the route a station may sit and still be used.",
    )

    min_stop_spacing_miles = serializers.FloatField(
        required=False,
        allow_null=True,
        min_value=0.0,
        max_value=250.0,
        help_text=(
            "Least distance between chosen stops. It suppresses a cheaper pump "
            "a mile past another one, which would otherwise produce a stop that "
            "buys a fraction of a gallon. Pass 0 for the unconstrained optimum."
        ),
    )

    def validate(self, attrs):
        if attrs["start"].casefold() == attrs["finish"].casefold():
            raise serializers.ValidationError(
                {"finish": "The start and the finish must be different places."}
            )
        tank = settings.FUEL_ROUTE["MAX_RANGE_MILES"] / settings.FUEL_ROUTE["MILES_PER_GALLON"]
        supplied = attrs.get("start_fuel_gallons")
        if supplied is not None and supplied > tank:
            raise serializers.ValidationError(
                {"start_fuel_gallons": f"The tank holds at most {tank:.1f} gallons."}
            )
        return attrs
