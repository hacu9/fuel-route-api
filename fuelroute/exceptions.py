"""Error types and the DRF exception handler.

Every failure the planner can produce maps to one class here, so the API always
answers with the same JSON envelope and a status code that tells the caller
whose problem it is.
"""

from __future__ import annotations

from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import exception_handler as drf_exception_handler


class PlannerError(Exception):
    """Base class for a failure the planner understands."""

    status_code = status.HTTP_400_BAD_REQUEST
    code = "planner_error"

    def __init__(self, message: str, **context):
        super().__init__(message)
        self.message = message
        self.context = context


class GeocodingError(PlannerError):
    """The API could not turn a location string into coordinates."""

    status_code = status.HTTP_422_UNPROCESSABLE_ENTITY
    code = "geocoding_failed"


class OutsideCoverageError(PlannerError):
    """A supplied point sits outside the United States."""

    status_code = status.HTTP_422_UNPROCESSABLE_ENTITY
    code = "outside_coverage"


class RoutingError(PlannerError):
    """The routing provider refused or could not answer."""

    status_code = status.HTTP_502_BAD_GATEWAY
    code = "routing_failed"


class NoRouteError(PlannerError):
    """The provider found no drivable road between the two points."""

    status_code = status.HTTP_422_UNPROCESSABLE_ENTITY
    code = "no_route"


class CatalogEmptyError(PlannerError):
    """No fuel stations are loaded, so no plan can be priced."""

    status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    code = "catalog_empty"


class InfeasibleRouteError(PlannerError):
    """A gap between usable stations is longer than the vehicle range."""

    status_code = status.HTTP_422_UNPROCESSABLE_ENTITY
    code = "infeasible_route"


def api_exception_handler(exc, context):
    """Render PlannerError subclasses; defer everything else to DRF."""
    if isinstance(exc, PlannerError):
        body = {"error": {"code": exc.code, "message": exc.message}}
        if exc.context:
            body["error"]["details"] = exc.context
        return Response(body, status=exc.status_code)

    response = drf_exception_handler(exc, context)
    if response is not None and not isinstance(response.data, dict):
        response.data = {"error": {"code": "invalid_request", "message": response.data}}
    elif response is not None and "error" not in response.data:
        response.data = {
            "error": {
                "code": "invalid_request",
                "message": "The request could not be processed.",
                "details": response.data,
            }
        }
    return response
