# =========================================================
# V2V MESSAGE BUS ENDPOINTS (/api/v1/v2v)
# =========================================================

from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

from services import fleet
from services import v2v

from .common import documented_body, error, json_body, require_local, success


router = APIRouter(prefix="/api/v1/v2v", tags=["V2V"])


class SendMessage(BaseModel):
    sender: str = Field(description="Vehicle name/alias, or a roadside unit ID (RSU1).", examples=["Vehicle_01"])
    recipient: str = Field("*", description="Vehicle, roadside unit, or * for a broadcast.", examples=["*"])
    msg_type: str = Field(description="One of the bus message types.", examples=["DENM"])
    payload: Dict[str, Any] = Field(default_factory=dict, examples=[{"cause": "OBSTACLE", "action": "new"}])
    priority: Optional[int] = Field(None, ge=0, le=7)
    ttl_ms: Optional[float] = Field(None, gt=0)
    msg_id: Optional[str] = Field(None, description="Reuse an ID to demonstrate duplicate rejection.")


class BusConfig(BaseModel):
    comm_range_m: Optional[float] = Field(None, examples=[150])
    loss_rate: Optional[float] = Field(None, ge=0, le=1, examples=[0.3])
    latency_min_ms: Optional[float] = Field(None, examples=[20])
    latency_max_ms: Optional[float] = Field(None, examples=[80])
    rsu_enabled: Optional[bool] = None
    cam_interval_ms: Optional[float] = None
    cam_timeout_ms: Optional[float] = None
    handover_timeout_ms: Optional[float] = None
    target_gap_m: Optional[float] = None


def _endpoint(name):
    """Canonical vehicle ID, a roadside unit ID, '*' or None."""

    if name == v2v.BROADCAST:
        return name
    if name in v2v.bus.rsus:
        return name
    return fleet.resolve(name)


@router.get("/messages", summary="Recent bus traffic")
def messages(
    limit: int = Query(50, ge=1, le=v2v.HISTORY_SIZE),
    type: Optional[str] = Query(None, description="Only this message type."),
    vehicle: Optional[str] = Query(None, description="Only messages sent by, to, or received by this vehicle."),
):
    """Newest first (the last 500 are kept). Each message lists its deliveries:
    delivered (with latency, and `via` a roadside unit when relayed), lost,
    out_of_range, stale or duplicate."""

    vehicle_id = fleet.resolve(vehicle) if vehicle else None

    if vehicle and vehicle_id is None:
        return error("Unknown vehicle.", 400, "INVALID_VEHICLE")

    if type and type not in v2v.MESSAGE_TYPES:
        return error(f"Unknown message type. Allowed: {', '.join(v2v.MESSAGE_TYPES)}.", 400, "INVALID_MESSAGE_TYPE")

    items = v2v.bus.messages(limit=limit, msg_type=type, vehicle=vehicle_id)

    return success("V2V messages retrieved.", {"count": len(items), "messages": items})


@router.get("/links", summary="Link quality per pair")
def links():
    """Every directed radio link seen so far (vehicle to vehicle, vehicle to
    roadside unit): state as judged by the receiver (connected, degraded,
    pending, lost, out_of_range), quality (0..1, moving average of
    deliveries), distance, attempts, received, lost."""

    items = v2v.bus.links()

    return success("V2V links retrieved.", {"count": len(items), "links": items})


@router.get("/inbox/{vehicle}", summary="One vehicle's received messages")
def inbox(vehicle: str, limit: int = Query(50, ge=1, le=v2v.INBOX_SIZE)):

    vehicle_id = fleet.resolve(vehicle)

    if vehicle_id is None:
        return error(f"Vehicle must be one of: {fleet.describe()}.", 400, "INVALID_VEHICLE")

    items = v2v.bus.inbox(vehicle_id, limit)

    return success("Inbox retrieved.", {"vehicle": vehicle_id, "count": len(items), "messages": items})


@router.get("/agents", summary="What each vehicle's onboard unit knows")
def agents():
    """Per vehicle: neighbours and link states, hazards heard (DENMs and
    silent neighbours), its own active DENMs, loading-slot status, shared-lane
    status, platoon role; per roadside unit: slot holder, queue, who it hears;
    and the latest handover negotiations."""

    return success("V2V agents retrieved.", v2v.bus.agents_view())


@router.get("/stats", summary="Bus counters")
def stats():
    """Messages sent, deliveries attempted, delivered, lost, out of range,
    duplicate, stale, relayed by a roadside unit, by message type; and the
    current configuration."""

    return success("V2V statistics retrieved.", v2v.bus.statistics())


@router.get("/config", summary="Radio settings")
def get_config():

    return success("V2V configuration retrieved.", dict(v2v.bus.config))


@router.post(
    "/config",
    summary="Change radio settings (demo: degrade the link)",
    dependencies=[Depends(require_local)],
    openapi_extra=documented_body(BusConfig),
)
def set_config(data: dict = Depends(json_body)):
    """Send only the settings to change, e.g. {"loss_rate": 0.5} or
    {"comm_range_m": 60}. Takes effect on the next message. Only accepted from
    the computer running the backend. 400 INVALID_V2V_CONFIG."""

    try:
        config = v2v.bus.update_config(data)
    except v2v.V2VError as exc:
        return error(str(exc), 400, "INVALID_V2V_CONFIG")

    return success("V2V configuration updated.", config)


@router.post(
    "/send",
    summary="Inject a message (demo tool)",
    dependencies=[Depends(require_local)],
    openapi_extra=documented_body(SendMessage),
)
def send(data: dict = Depends(json_body)):
    """Puts a message on the radio as if `sender` had sent it (range, latency
    and loss apply). It can forge a hazard warning, so it is only accepted from
    the computer running the backend (admin role after authentication is
    added). 400: INVALID_SENDER, INVALID_RECIPIENT, INVALID_MESSAGE_TYPE,
    INVALID_PAYLOAD."""

    sender = _endpoint(data.get("sender"))
    recipient = _endpoint(data.get("recipient", v2v.BROADCAST))

    if sender in (None, v2v.BROADCAST):
        return error("sender must be a vehicle or a roadside unit.", 400, "INVALID_SENDER")

    if recipient is None:
        return error("recipient must be a vehicle, a roadside unit or *.", 400, "INVALID_RECIPIENT")

    payload = data.get("payload", {})

    if not isinstance(payload, dict):
        return error("payload must be a JSON object.", 400, "INVALID_PAYLOAD")

    try:
        message = v2v.bus.send(
            sender, recipient, str(data.get("msg_type", "")), payload,
            priority=data.get("priority"), ttl_ms=data.get("ttl_ms"), msg_id=data.get("msg_id"),
        )
    except v2v.V2VError as exc:
        return error(str(exc), 400, "INVALID_MESSAGE_TYPE")

    return success("Message sent.", message.to_dict())
