# =========================================================
# ALERT LIFECYCLE — /api/v1/alerts
# =========================================================

from typing import Optional

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field

from services import alerts

from .common import documented_body, error, json_body, success


router = APIRouter(prefix="/api/v1/alerts", tags=["Operations"])


class AlertAction(BaseModel):
    actor: Optional[str] = Field(
        None, description="Who is acting. Defaults to the caller's address (no user accounts until Phase 6).")


def _actor(request, data):
    actor = data.get("actor")
    if actor:
        return str(actor)
    return request.client.host if request.client else "unknown"


@router.get("", summary="List alerts")
def list_alerts(
    state: Optional[str] = Query(None, description="open, acknowledged or resolved."),
    vehicle: Optional[str] = Query(None),
    severity: Optional[str] = Query(None, description="info, warning or critical."),
):
    """Conditions the old dashboard computed client-side (emergency active,
    vehicle offline, V2V contact lost, low battery, a vehicle fault, the
    autopilot watchdog failing, or an autopilot that cannot continue) are now
    real records with a lifecycle: open -> acknowledged -> resolved. Open and
    low-severity alerts auto-resolve once their condition clears; resolving
    by hand works at any time."""

    if state and state not in (alerts.STATE_OPEN, alerts.STATE_ACKNOWLEDGED, alerts.STATE_RESOLVED):
        return error("state must be open, acknowledged or resolved.", 400, "INVALID_ALERT_STATE")

    from services import fleet
    vehicle_id = fleet.resolve(vehicle) if vehicle else None

    if vehicle and vehicle_id is None:
        return error("Unknown vehicle.", 400, "INVALID_VEHICLE")

    items = alerts.list_alerts(state=state, vehicle=vehicle_id, severity=severity)

    return success("Alerts retrieved.", {"count": len(items), "alerts": items})


@router.post("/{alert_id}/ack", summary="Acknowledge an alert", openapi_extra=documented_body(AlertAction, required=False))
def acknowledge(alert_id: int, request: Request, data: dict = Depends(json_body)):
    """404 ALERT_NOT_FOUND; 409 ALERT_RESOLVED (an already-resolved alert
    cannot be acknowledged)."""

    try:
        alert = alerts.acknowledge(alert_id, _actor(request, data))
    except alerts.AlertError as exc:
        return error(str(exc), exc.status, exc.code)

    return success("Alert acknowledged.", alert)


@router.post("/{alert_id}/resolve", summary="Resolve an alert", openapi_extra=documented_body(AlertAction, required=False))
def resolve(alert_id: int, request: Request, data: dict = Depends(json_body)):
    """404 ALERT_NOT_FOUND. Resolving does not require the underlying
    condition to be gone; auto-resolve (by the backend, once it clears) still
    runs normally for any alert raised again later."""

    try:
        alert = alerts.resolve(alert_id, _actor(request, data))
    except alerts.AlertError as exc:
        return error(str(exc), exc.status, exc.code)

    return success("Alert resolved.", alert)
