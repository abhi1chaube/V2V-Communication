# =========================================================
# ACTIVITY LOG, V2V COMMUNICATION AND J2735 MESSAGES
# =========================================================

from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Query

from services import events
from services import fleet
from services import j2735
from services import state
from services import v2v
from services.timeutil import utc_now

from .common import error, success


router = APIRouter()

LIMIT_QUERY = Query(None, description="1-200, default 50.")


@router.get("/api/events", tags=["J2735"], summary="Activity log events")
def get_events(limit: Optional[str] = LIMIT_QUERY):
    """Newest first; at most 200 are kept in memory (cleared on restart).
    J2735 events have event_type "j2735" and the BSM in `details.j2735`;
    V2V handovers carry `details.v2v_message`."""

    try:
        return success(
            "Events retrieved.",
            events.get_events(limit if limit is not None else 50)
        )
    except Exception as exc:
        return error(str(exc), 500, "EVENTS_ERROR")


@router.post("/api/events/clear", tags=["J2735"], summary="Clear the activity log")
def clear_events():

    try:
        events.clear_events()
        return success("Events cleared.")
    except Exception as exc:
        return error(str(exc), 500, "EVENT_CLEAR_ERROR")


@router.get("/api/events/{vehicle}", tags=["J2735"], summary="Activity log events for one vehicle")
def get_vehicle_events(vehicle: str, limit: Optional[str] = LIMIT_QUERY):

    vehicle = fleet.resolve(vehicle)

    if vehicle is None:
        return error("Invalid vehicle.", 400, "INVALID_VEHICLE")

    try:
        return success(
            "Vehicle events retrieved.",
            events.get_vehicle_events(vehicle, limit if limit is not None else 50)
        )
    except Exception as exc:
        return error(str(exc), 500, "EVENTS_ERROR")


def _last_seen_age(vehicle_id, now_ms):
    """(last_seen, age in seconds) of the vehicle's reported heartbeat."""

    last_seen = state.get_vehicle_state(vehicle_id).get("last_seen")

    seen_ms = _parse_utc_ms(last_seen)

    if seen_ms is None:
        return None, None

    return last_seen, round(max(0.0, (now_ms - seen_ms) / 1000), 1)


def _parse_utc_ms(value):
    """Epoch ms from an ISO timestamp (zone-less = UTC) or an epoch number."""

    if value in (None, ""):
        return None

    if isinstance(value, (int, float)):
        return value if value > 1e12 else value * 1000

    text = str(value).strip()

    try:
        number = float(text)
        return number if number > 1e12 else number * 1000
    except ValueError:
        pass

    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None

    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)

    return moment.timestamp() * 1000


@router.get("/api/communication", tags=["Fleet"], summary="Vehicle communication status")
def communication():
    """Per vehicle, measured on the simulated V2V radio (services/v2v.py):

    - `connected`: hears its neighbours in range (or the roadside unit)
    - `degraded`: some neighbour in range is lost, or a link is poor
    - `lost`: every vehicle in range went silent; the vehicle holds (comm_hold)
    - `isolated`: nobody in radio range and no roadside unit
    - `unknown`: the bus has not run for this vehicle yet

    Each vehicle also reports its neighbour count, average link quality,
    messages per second and whether a roadside unit hears it.
    `connected` (top level) is true when at least one vehicle is connected."""

    now_ms = utc_now().timestamp() * 1000

    vehicle_ids = fleet.vehicle_ids()

    details = v2v.bus.communication_status()

    for vehicle_id in vehicle_ids:
        last_seen, age = _last_seen_age(vehicle_id, now_ms)
        details.setdefault(vehicle_id, {"status": "unknown"})
        details[vehicle_id]["last_seen"] = last_seen
        details[vehicle_id]["last_seen_age_seconds"] = age

    connected = [v for v in vehicle_ids if details[v]["status"] == "connected"]

    return success(
        "Vehicle communication status.",
        {
            "connected": bool(connected),
            "vehicles": vehicle_ids,
            "vehicle_statuses": {v: details[v]["status"] for v in vehicle_ids},
            "details": {v: details[v] for v in vehicle_ids},
            "connected_count": len(connected),
            "comm_range_m": v2v.bus.config["comm_range_m"],
            "protocol": "V2V radio (simulated): CAM heartbeats, range-limited, with latency and loss",
            "message": (
                f"{len(connected)} of {len(vehicle_ids)} vehicles connected."
                if vehicle_ids
                else "No vehicles registered."
            )
        }
    )


@router.get("/api/j2735/bsm", tags=["J2735"], summary="J2735-styled BasicSafetyMessage for a vehicle")
def get_bsm(
    vehicle: Optional[str] = Query(None, description="Vehicle name or alias. Default is the first vehicle.")
):
    """Same JSON as the BSM rows in the Activity Log (built by
    services/j2735.py). J2735-styled only: not UPER-encoded and not sent over
    V2X. Fields without a sensor (lat, long, heading, accel) carry J2735
    "unavailable" codes. `msgCnt` is not advanced by this call."""

    resolved = (
        fleet.resolve(vehicle)
        if vehicle
        else next(iter(fleet.vehicle_ids()), None)
    )

    if resolved is None:
        return error(
            f"Provide vehicle as one of: {fleet.describe()}.",
            400,
            "INVALID_VEHICLE"
        )

    current = state.get_vehicle_state(resolved)

    return success(
        "Basic Safety Message generated.",
        j2735.build_bsm(resolved, current)
    )
