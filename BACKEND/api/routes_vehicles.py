# =========================================================
# VEHICLE REGISTRY (VEHICLE MANAGER) AND THINGSBOARD
# =========================================================
#
# Vehicles live in BACKEND/data/vehicles.json (see
# services/fleet.py). Tokens are never returned by the API;
# responses carry token_hint (last 4 characters) instead.
# =========================================================

from typing import Optional

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse

from services import events
from services import fleet
from services import j2735
from services import provisioning
from services import state
from services import thingsboard

from .common import (
    FLEET_ERRORS,
    documented_body,
    error,
    fleet_error,
    json_body,
    parse_bool,
    require_local,
    success,
)
from . import commands
from .auth import require_role
from .commands import command_lock
from .schemas import VehicleCreate, VehicleUpdate
from services import alerts
from services import autopilot
from services import v2v
from services import world

from .sync import (
    _REMOVED,
    backend_owns_state,
    clear_missing,
    device_missing_in_thingsboard,
    forget_link,
    link_status,
    refresh_vehicle_from_thingsboard,
    rename_link,
    sync_all_to_thingsboard,
)


router = APIRouter()


# =========================================================
# FLEET BUSY GUARD
# =========================================================
#
# Fleet order is handover order. Adding, renaming or
# re-linking a vehicle while any vehicle is in a mission would
# change who hands over to whom mid-cycle, so it is refused
# server-side (the dashboard's own check was client-side only).
# Colour-only edits are allowed. Deleting is allowed for a
# vehicle that is itself not in a mission.
# =========================================================

ORDER_CHANGING_FIELDS = ("name", "device_id", "token")


def fleet_busy_error(busy):

    return error(
        "Vehicle registry changes are blocked while vehicles are in a mission: "
        f"{', '.join(busy)}. Finish the cycle, or reset it, then try again.",
        409,
        "FLEET_BUSY",
        {"vehicles_in_mission": busy}
    )


# =========================================================
# REGISTRY
# =========================================================

@router.get("/api/vehicles", tags=["Vehicles"], summary="List registered vehicles")
def list_vehicles(
    search: str = Query("", description="Case-insensitive filter on name or device ID.")
):
    """Vehicles from data/vehicles.json, in fleet order. Tokens are never
    returned; `token_hint` shows the last 4 characters. `problems` lists
    configuration issues (e.g. a missing token)."""

    search = str(search).strip().lower()

    vehicles = fleet.list_vehicles()

    if search:
        vehicles = [
            vehicle
            for vehicle in vehicles
            if search in vehicle["name"].lower()
            or search in vehicle["device_id"].lower()
        ]

    return success(
        "Registered vehicles retrieved.",
        {
            "count": len(vehicles),
            "vehicles": vehicles,
            "problems": fleet.validate()
        }
    )


@router.post(
    "/api/vehicles",
    tags=["Vehicles"],
    summary="Add a vehicle",
    status_code=201,
    dependencies=[Depends(require_local), Depends(require_role("admin"))],
    openapi_extra=documented_body(VehicleCreate)
)
def add_vehicle(data: dict = Depends(json_body)):
    """**auto** (default when no device_id/token is sent): send only `name`
    (and optional `color`). The backend creates the ThingsBoard device, reads
    its ID and access token, and saves the vehicle. If a later step fails, the
    new device is deleted again.

    **link**: send `name`, `device_id` and `token` of a device that already
    exists in ThingsBoard. Nothing is created there.

    Only accepted from the computer running the backend."""

    mode = str(
        data.get("mode")
        or (
            "link"
            if data.get("device_id") or data.get("token")
            else "auto"
        )
    ).strip().lower()

    if mode not in ("auto", "link"):
        return error("mode must be 'auto' or 'link'.", 400, "INVALID_MODE")

    busy = state.vehicles_in_mission()

    if busy:
        return fleet_busy_error(busy)

    try:

        if mode == "auto":

            vehicle = provisioning.provision_vehicle(
                data.get("name"),
                data.get("color")
            )

        else:

            if device_missing_in_thingsboard(data.get("device_id")):
                return error(
                    "No ThingsBoard device has this device ID. "
                    "Check the ID in ThingsBoard (Devices > device > Copy device ID).",
                    400,
                    "DEVICE_NOT_FOUND"
                )

            vehicle = fleet.add_vehicle(
                name=data.get("name"),
                device_id=data.get("device_id"),
                token=data.get("token"),
                color=data.get("color")
            )

    except FLEET_ERRORS as exc:
        return fleet_error(exc)

    if mode == "auto":
        events.log_vehicle_provisioned(vehicle.vehicle_id)
        message = f"{vehicle.name} added. ThingsBoard device created automatically."
    else:
        events.log_vehicle_added(vehicle.vehicle_id)
        message = f"{vehicle.name} added to the fleet."

    return success(
        message,
        {
            "vehicle": vehicle.to_public(),
            "mode": mode
        },
        status=201
    )


@router.put(
    "/api/vehicles/{vehicle_name}",
    tags=["Vehicles"],
    summary="Edit or rename a vehicle",
    dependencies=[Depends(require_local), Depends(require_role("admin"))],
    openapi_extra=documented_body(VehicleUpdate)
)
def update_vehicle(vehicle_name: str, data: dict = Depends(json_body)):
    """Send only the fields to change. A blank or missing token keeps the
    current token. Renaming a vehicle whose device was created by this
    dashboard (managed) also renames the ThingsBoard device; its device_id and
    token cannot be changed. `vehicle_name` must be the exact name (aliases
    such as "1" are not accepted). Only accepted from the computer running the
    backend."""

    changes = {
        key: data[key]
        for key in ("name", "device_id", "token", "color")
        if key in data
    }

    current = fleet.resolve(vehicle_name)

    if any(key in changes for key in ORDER_CHANGING_FIELDS):

        busy = state.vehicles_in_mission()

        if busy:
            return fleet_busy_error(busy)

    with command_lock(current):
        return _update_vehicle(vehicle_name, current, changes)


def _update_vehicle(vehicle_name, current, changes):

    if (
        "device_id" in changes
        and current is not None
        and str(changes["device_id"] or "").strip() != fleet.device_id(current)
        and device_missing_in_thingsboard(changes["device_id"])
    ):
        return error(
            "No ThingsBoard device has this device ID. "
            "Check the ID in ThingsBoard (Devices > device > Copy device ID).",
            400,
            "DEVICE_NOT_FOUND"
        )

    # A device created by the dashboard is renamed in
    # ThingsBoard too, first, so a rejected name changes nothing.
    undo_rename = None

    try:

        if "name" in changes:
            undo_rename = provisioning.rename_managed_device(
                vehicle_name,
                changes["name"]
            )

        old_id, vehicle = fleet.update_vehicle(vehicle_name, **changes)

    except FLEET_ERRORS as exc:

        if undo_rename is not None:
            try:
                undo_rename()
            except Exception as undo_error:
                print(f"[fleet] Could not undo ThingsBoard rename: {undo_error}")

        return fleet_error(exc)

    clear_missing(old_id, vehicle.vehicle_id)

    if old_id != vehicle.vehicle_id:
        # New name = new TemporaryID and a fresh msgCnt.
        j2735.forget(old_id, vehicle.vehicle_id)
        state.rename_vehicle(old_id, vehicle.vehicle_id)
        world.engine.rename(old_id, vehicle.vehicle_id)
        v2v.bus.forget(old_id)          # its agent restarts under the new name
        autopilot.controller.rename(old_id, vehicle.vehicle_id)
        rename_link(old_id, vehicle.vehicle_id)

    events.log_vehicle_updated(vehicle.vehicle_id, old_id)

    return success(
        f"{vehicle.name} updated.",
        {
            "vehicle": vehicle.to_public(),
            "previous_name": old_id
        }
    )


@router.delete(
    "/api/vehicles/{vehicle_name}",
    tags=["Vehicles"],
    summary="Delete a vehicle",
    dependencies=[Depends(require_local), Depends(require_role("admin"))]
)
def delete_vehicle(
    vehicle_name: str,
    delete_device: Optional[str] = Query(
        None,
        description="true = also delete the ThingsBoard device and its telemetry history "
                    "(irreversible, managed vehicles only)."
    )
):
    """Removes the vehicle from the dashboard and data/vehicles.json. The
    ThingsBoard device is kept unless `delete_device=true`, which is only
    allowed for managed vehicles. The device is deleted first, so if that
    fails nothing is removed. Only accepted from the computer running the
    backend."""

    delete_device = parse_bool(delete_device, False)

    target = fleet.resolve(vehicle_name)

    if target is not None and state.in_mission(state.get_vehicle_state(target)):
        return fleet_busy_error([target])

    with command_lock(target):

        try:
            vehicle = provisioning.remove_vehicle(
                vehicle_name,
                delete_device=delete_device
            )
        except FLEET_ERRORS as exc:
            return fleet_error(exc)

        clear_missing(vehicle.vehicle_id)
        state.forget_vehicle(vehicle.vehicle_id)
        j2735.forget(vehicle.vehicle_id)
        world.engine.forget(vehicle.vehicle_id)
        v2v.bus.forget(vehicle.vehicle_id)
        autopilot.controller.forget(vehicle.vehicle_id)
        alerts.forget_vehicle(vehicle.vehicle_id)
        forget_link(vehicle.vehicle_id)

    commands.forget(vehicle.vehicle_id)

    events.log_vehicle_deleted(
        vehicle.vehicle_id,
        reason="ThingsBoard device deleted too" if delete_device else None
    )

    return success(
        (
            f"{vehicle.name} removed from the fleet and its "
            "ThingsBoard device was deleted."
            if delete_device
            else f"{vehicle.name} removed from the fleet."
        ),
        {
            "vehicle": vehicle.to_public(),
            "device_deleted": delete_device
        }
    )


@router.get(
    "/api/vehicles/{vehicle}",
    tags=["Vehicles"],
    summary="One vehicle (live state + configuration)"
)
def get_vehicle(vehicle: str):
    """The vehicle's live state, its registry entry, and its ThingsBoard link
    health. With STATE_SOURCE=backend (default) the state is the backend's own
    and a ThingsBoard outage does not fail this call; in thingsboard_mirror
    mode the ThingsBoard telemetry is applied first (503 if it cannot be read).
    `vehicle` accepts a name or alias (Vehicle_01, 01, 1, yellow)."""

    vehicle = fleet.resolve(vehicle)

    if vehicle is None:
        return error(
            f"Vehicle must be one of: {fleet.describe()}.",
            400,
            "INVALID_VEHICLE"
        )

    if backend_owns_state():

        # The backend's state is the answer; the device read is a
        # health check and may fail without failing this call.
        current = refresh_vehicle_from_thingsboard(vehicle)

        if current is _REMOVED:
            return error(f"Vehicle {vehicle} not found.", 404, "VEHICLE_NOT_FOUND")

        current.pop("_thingsboard_error", None)

        return success(
            "Latest vehicle telemetry retrieved.",
            {
                "vehicle": current,
                "config": fleet.get(vehicle).to_public(),
                "thingsboard_link": link_status(vehicle)
            }
        )

    try:
        device_id = thingsboard.get_device_id(vehicle)
        latest = thingsboard.get_latest_state(device_id)
        current = state.apply_telemetry_state(vehicle, latest)

        return success(
            "Latest vehicle telemetry retrieved.",
            {
                "vehicle": current,
                "config": fleet.get(vehicle).to_public(),
                "thingsboard_link": link_status(vehicle)
            }
        )

    except Exception as exc:
        return error(
            "Unable to retrieve vehicle telemetry.",
            503,
            "THINGSBOARD_READ_ERROR",
            {"error": str(exc)}
        )


# =========================================================
# THINGSBOARD
# =========================================================

@router.get("/api/thingsboard/quota", tags=["ThingsBoard"], summary="Device limit of the ThingsBoard plan")
def thingsboard_quota():
    """How many devices the ThingsBoard account uses and allows. `quota` is
    null when the server does not report a limit."""

    try:
        quota = provisioning.device_quota()
    except Exception as exc:
        return error(
            "Unable to read the ThingsBoard device limit.",
            502,
            "THINGSBOARD_ERROR",
            {"error": str(exc)}
        )

    return success("Device quota retrieved.", {"quota": quota})


@router.get("/api/thingsboard/test", tags=["ThingsBoard"], summary="Test the ThingsBoard login")
def thingsboard_test():

    try:
        result = thingsboard.test_connection()

        return JSONResponse({
            "success": result.get("connected", False),
            "thingsboard": result
        })

    except Exception as exc:
        return error(str(exc), 503, "THINGSBOARD_CONNECTION_ERROR")


@router.post(
    "/api/thingsboard/sync", tags=["ThingsBoard"], summary="Push every vehicle's state to ThingsBoard",
    dependencies=[Depends(require_role("operator"))],
)
def thingsboard_sync():

    try:
        return success(
            "Vehicle state synchronized with ThingsBoard.",
            sync_all_to_thingsboard()
        )
    except Exception as exc:
        return error(str(exc), 503, "THINGSBOARD_SYNC_ERROR")
