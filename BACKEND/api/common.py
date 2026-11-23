# =========================================================
# SHARED API HELPERS
# =========================================================
#
# Response envelope, request parsing and access control used
# by every router. The envelope is the contract the dashboard
# depends on (FRONTEND/script.js and vehicle-manager.js read
# `success`, `message` and `code`), so keep it unchanged:
#
#   success: {"success": true,  "message": ..., "data"?: ...}
#   error:   {"success": false, "message": ..., "code"?: ..., "data"?: ...}
#
# =========================================================

import json

import requests
from fastapi import Request
from fastapi.responses import JSONResponse

from services import fleet
from services import thingsboard
from services.settings import settings


API_VERSION = "2.1.0"


# =========================================================
# RESPONSE ENVELOPE
# =========================================================

def success(message, data=None, status=200):

    response = {
        "success": True,
        "message": message
    }

    if data is not None:
        response["data"] = data

    return JSONResponse(response, status_code=status)


def error(message, status=400, code=None, data=None):

    response = {
        "success": False,
        "message": message
    }

    if code is not None:
        response["code"] = code

    if data is not None:
        response["data"] = data

    return JSONResponse(response, status_code=status)


class ApiError(Exception):
    """Raise from a route or dependency to return an error envelope."""

    def __init__(self, message, status=400, code=None, data=None):

        super().__init__(message)

        self.message = message

        self.status = status

        self.code = code

        self.data = data

    def to_response(self):

        return error(self.message, self.status, self.code, self.data)


# =========================================================
# REQUEST PARSING
# =========================================================

async def json_body(request: Request) -> dict:
    """The JSON request body, or {} when absent or invalid.

    Mirrors Flask's request.get_json(silent=True) or {}: only a
    JSON content type is parsed, and anything unparseable or
    not a JSON object becomes an empty dict, so each route
    reports its own error code (INVALID_VEHICLE, ...) instead
    of a generic validation error.
    """

    content_type = request.headers.get("content-type", "")
    mimetype = content_type.split(";", 1)[0].strip().lower()

    if not (mimetype == "application/json" or mimetype.endswith("+json")):
        return {}

    raw = await request.body()

    if not raw:
        return {}

    try:
        data = json.loads(raw)
    except ValueError:
        return {}

    return data if isinstance(data, dict) else {}


def vehicle_from_body(data):
    """Canonical vehicle ID from body["vehicle"], or None."""

    return fleet.resolve(data.get("vehicle"))


def parse_bool(value, default=False):
    """Parse JSON/query boolean values without bool("false") bugs."""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in ("true", "1", "yes", "on", "detected", "obstacle", "applied"):
        return True
    if text in ("false", "0", "no", "off", "not_detected", "clear", "released"):
        return False
    return default


def invalid_vehicle():

    return error(
        f"Provide vehicle as one of: {fleet.describe()}.",
        400,
        "INVALID_VEHICLE"
    )


# =========================================================
# ACCESS CONTROL
# =========================================================
#
# Vehicle add/edit/delete can create and delete ThingsBoard
# devices, so it is accepted only from this computer. Set
# ALLOW_REMOTE_FLEET_ADMIN=true to allow it from the network.
# Vehicle commands and sensor endpoints are not restricted.
# =========================================================

ALLOW_REMOTE_FLEET_ADMIN = settings.allow_remote_fleet_admin

LOCAL_ADDRESSES = {"127.0.0.1", "::1", "::ffff:127.0.0.1"}


def require_local(request: Request):
    """Dependency: reject the request unless it comes from this computer."""

    host = request.client.host if request.client else None

    if not ALLOW_REMOTE_FLEET_ADMIN and host not in LOCAL_ADDRESSES:

        raise ApiError(
            "Vehicle management is only allowed from the computer "
            "running the backend. Set ALLOW_REMOTE_FLEET_ADMIN=true "
            "to allow it from the network.",
            403,
            "REMOTE_ADMIN_DISABLED"
        )


# =========================================================
# REGISTRY ERRORS
# =========================================================

FLEET_ERRORS = (
    fleet.FleetError,
    thingsboard.ThingsBoardError,
    requests.RequestException,
    OSError
)


def fleet_error(exc):

    if isinstance(exc, fleet.FleetError):

        return error(
            str(exc),
            exc.status,
            exc.code
        )

    if isinstance(exc, thingsboard.ThingsBoardError):

        return error(
            str(exc),
            502,
            "THINGSBOARD_ERROR",
            {
                "thingsboard_status": exc.status
            }
        )

    if isinstance(exc, requests.RequestException):

        return error(
            "Could not reach ThingsBoard. Nothing was changed.",
            502,
            "THINGSBOARD_UNREACHABLE",
            {
                "error": str(exc)
            }
        )

    return error(
        "Unable to save vehicles.json.",
        500,
        "FLEET_SAVE_FAILED",
        {
            "error": str(exc)
        }
    )


# =========================================================
# OPENAPI REQUEST-BODY DOCUMENTATION
# =========================================================

def documented_body(model, required=True):
    """openapi_extra that documents a JSON body from a Pydantic model.

    Bodies are still parsed leniently by json_body() so the
    existing error codes and accepted spellings (for example
    "applied": "released") keep working. The model only feeds
    the API docs.
    """

    return {
        "requestBody": {
            "required": required,
            "content": {
                "application/json": {
                    "schema": model.model_json_schema()
                }
            }
        }
    }
