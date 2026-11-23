# =========================================================
# HISTORY AND REPLAY (services/db.py) — /api/v1/*
# =========================================================

from typing import Optional

from fastapi import APIRouter, Query

from services import db
from services import fleet
from services import v2v

from .common import error, success


router = APIRouter(prefix="/api/v1", tags=["Operations"])


@router.get("/events/history", summary="Activity log history (SQLite, beyond the in-memory 200)")
def events_history(
    start: Optional[str] = Query(None, alias="from", description="ISO 8601 UTC, inclusive."),
    end: Optional[str] = Query(None, alias="to", description="ISO 8601 UTC, inclusive."),
    vehicle: Optional[str] = Query(None),
    type: Optional[str] = Query(None, description="event_type, e.g. safety, v2v, autopilot, scenario, alert."),
    limit: int = Query(200, ge=1, le=5000),
):

    vehicle_id = fleet.resolve(vehicle) if vehicle else None

    if vehicle and vehicle_id is None:
        return error("Unknown vehicle.", 400, "INVALID_VEHICLE")

    try:
        items = db.query_events(start=start, end=end, vehicle=vehicle_id, event_type=type, limit=limit)
    except db.DBError as exc:
        return error(str(exc), 503, "DB_NOT_READY")

    return success("Event history retrieved.", {"count": len(items), "events": items})


@router.get("/v2v/history", summary="V2V message history (SQLite, beyond the in-memory 500)")
def v2v_history(
    start: Optional[str] = Query(None, alias="from", description="ISO 8601 UTC, inclusive."),
    end: Optional[str] = Query(None, alias="to", description="ISO 8601 UTC, inclusive."),
    vehicle: Optional[str] = Query(None),
    type: Optional[str] = Query(None, description="One of the bus message types."),
    limit: int = Query(200, ge=1, le=5000),
):

    vehicle_id = fleet.resolve(vehicle) if vehicle else None

    if vehicle and vehicle_id is None:
        return error("Unknown vehicle.", 400, "INVALID_VEHICLE")

    if type and type not in v2v.MESSAGE_TYPES:
        return error(f"Unknown message type. Allowed: {', '.join(v2v.MESSAGE_TYPES)}.", 400, "INVALID_MESSAGE_TYPE")

    try:
        items = db.query_v2v(start=start, end=end, msg_type=type, vehicle=vehicle_id, limit=limit)
    except db.DBError as exc:
        return error(str(exc), 503, "DB_NOT_READY")

    return success("V2V message history retrieved.", {"count": len(items), "messages": items})


@router.get("/replay", summary="Pose/state timeline for session replay (Phase 7)")
def replay(
    start: Optional[str] = Query(None, alias="from", description="ISO 8601 UTC, inclusive."),
    end: Optional[str] = Query(None, alias="to", description="ISO 8601 UTC, inclusive."),
    vehicle: Optional[str] = Query(None),
    limit: int = Query(5000, ge=1, le=20000),
):
    """Rows from services/db.py's `state_timeline` table, about one per
    vehicle per second: position, speed, status, mission, load, battery,
    fault, next action. Oldest first, so a client can scrub forward through
    the range directly."""

    vehicle_id = fleet.resolve(vehicle) if vehicle else None

    if vehicle and vehicle_id is None:
        return error("Unknown vehicle.", 400, "INVALID_VEHICLE")

    try:
        rows = db.query_timeline(start=start, end=end, vehicle=vehicle_id, limit=limit)
    except db.DBError as exc:
        return error(str(exc), 503, "DB_NOT_READY")

    return success("Replay timeline retrieved.", {"count": len(rows), "timeline": rows})
