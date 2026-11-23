# =========================================================
# FLEET STATE, VEHICLE COMMANDS AND SENSOR INPUTS
# =========================================================
#
# Every command, under that vehicle's command lock
# (api/commands.py): refresh the vehicle from ThingsBoard,
# validate with services/safety.py, change local state (the
# state transition re-checks the emergency latch atomically),
# log events, push the new state to ThingsBoard (best effort),
# and report the push result as telemetry_synced /
# telemetry_error in the response data.
#
# Sensor inputs take no command lock: a hazard reading must
# never wait behind a slow command.
# =========================================================

from fastapi import APIRouter, Depends

from services import autopilot
from services import events
from services import fleet
from services import safety
from services import state
from services import v2v

from .auth import require_role
from .commands import command_lock
from .common import (
    documented_body,
    error,
    invalid_vehicle,
    json_body,
    parse_bool,
    success,
    vehicle_from_body,
)
from .schemas import (
    BrakeInput,
    CompleteJourney,
    LoadingUpdate,
    ManualCommand,
    QrInput,
    RadarInput,
    VehicleRef,
)
from .sync import (
    link_status,
    local_set_brake,
    refresh_for_command,
    refresh_from_thingsboard,
    sync_and_report,
    sync_vehicle_to_thingsboard,
    telemetry_report,
)


router = APIRouter()

VEHICLE_BODY = documented_body(VehicleRef)


def blocked(vehicle, exc):
    """403 for a state transition refused for a safety reason."""

    events.log_safety_block(vehicle, str(exc), exc.code)

    return error(str(exc), 403, exc.code)


def autopilot_active(vehicle):
    """409 when the autopilot drives this vehicle (checked inside its command lock)."""

    if not autopilot.controller.is_auto(vehicle):
        return None

    return error(
        f"{vehicle} is driven by the autopilot. Disable it first "
        "(POST /api/v1/autopilot/disable), or use emergency stop.",
        409,
        "AUTOPILOT_ACTIVE"
    )


# =========================================================
# LIVE FLEET (POLLED BY THE DASHBOARD)
# =========================================================

@router.get("/api/fleet", tags=["Fleet"], summary="Live fleet state")
def get_fleet():
    """Every vehicle's live state, in fleet order. The dashboard polls this.

    Each vehicle's ThingsBoard device is read in parallel as a health check
    (`thingsboard_link`: reachable, last push accepted); with
    STATE_SOURCE=thingsboard_mirror its telemetry is also applied. `removed`
    lists vehicles removed during this call because their ThingsBoard device
    was confirmed deleted (3 polls in a row plus a device lookup)."""

    try:

        # One snapshot of the registry for the whole response,
        # so a vehicle added/deleted meanwhile cannot break it.
        vehicle_ids = fleet.vehicle_ids()

        vehicles = refresh_from_thingsboard(vehicle_ids)

        removed = [v for v in vehicle_ids if v not in vehicles]
        remaining = [v for v in vehicle_ids if v in vehicles]

        return success(
            "Fleet data retrieved.",
            {
                "count": len(remaining),
                "vehicles": [
                    {
                        **vehicles[vehicle_id],
                        "vehicle_id": vehicle_id,
                        "thingsboard_link": link_status(vehicle_id)
                    }
                    for vehicle_id in remaining
                ],
                "removed": removed
            }
        )

    except Exception as exc:
        return error(
            "Unable to retrieve fleet data.",
            503,
            "FLEET_DATA_ERROR",
            {"error": str(exc)}
        )


# =========================================================
# MISSION COMMANDS
# =========================================================

@router.post("/api/start", tags=["Commands"], summary="Start a vehicle (Stop 1)", dependencies=[Depends(require_role("operator"))], openapi_extra=VEHICLE_BODY)
def start_vehicle(data: dict = Depends(json_body)):
    """403: EMERGENCY_STOP_ACTIVE, RADAR_OBSTACLE, INVALID_START_LOCATION,
    ALREADY_RUNNING, QR_NOT_DETECTED (AprilTag marker at Stop 1 not seen)."""

    vehicle = vehicle_from_body(data)

    if not vehicle:
        return invalid_vehicle()

    with command_lock(vehicle):

        busy = autopilot_active(vehicle)

        if busy:
            return busy

        try:

            refresh_for_command(vehicle)

            validation = safety.validate_start_command(vehicle)

            if not validation["allowed"]:
                events.log_command(vehicle, "START", False, validation["reason"])
                events.log_safety_block(vehicle, validation["reason"], validation["code"])
                return error(validation["reason"], 403, validation["code"])

            new_state = state.start_vehicle(vehicle)

            events.log_command(vehicle, "START", True)
            events.log_vehicle_started(vehicle, new_state["location"])
            events.log_brake_released(vehicle)

            return success(
                "Vehicle started.",
                {"vehicle": new_state, **sync_and_report(vehicle)}
            )

        except state.TransitionBlocked as exc:
            return blocked(vehicle, exc)

        except Exception as exc:
            events.log_error("Failed to start vehicle.", vehicle, {"error": str(exc)})
            return error(str(exc), 400, "START_FAILED")


@router.post("/api/loading-point", tags=["Commands"], summary="Move a vehicle to the loading point (Stop 2)", dependencies=[Depends(require_role("operator"))], openapi_extra=VEHICLE_BODY)
def move_to_loading_point(data: dict = Depends(json_body)):
    """403: INVALID_STATUS, EMERGENCY_STOP_ACTIVE, RADAR_OBSTACLE, OBSTACLE_DETECTED."""

    vehicle = vehicle_from_body(data)

    if not vehicle:
        return invalid_vehicle()

    with command_lock(vehicle):

        busy = autopilot_active(vehicle)

        if busy:
            return busy

        try:

            refresh_for_command(vehicle)

            current = state.get_vehicle_state(vehicle)

            if current["status"] not in (state.STATUS_RUNNING, state.STATUS_READY):
                return error(
                    "Vehicle must be running before reaching loading point.",
                    403,
                    "INVALID_STATUS"
                )

            if current["emergency_stop"]:
                return error("Emergency stop is active.", 403, "EMERGENCY_STOP_ACTIVE")

            if current["radar_status"] == state.RADAR_OBSTACLE:
                return error("Radar detected an obstacle.", 403, "RADAR_OBSTACLE")

            if current["obstacle_detected"]:
                return error("Obstacle detected.", 403, "OBSTACLE_DETECTED")

            # The roadside unit gave the loading point to another
            # vehicle (SLOT_GRANT). Occupancy itself is checked in
            # state.reach_loading_point().
            holder = v2v.bus.slot_holder_conflict(vehicle)

            if holder is not None:
                return error(
                    f"The loading point is reserved for {holder}.",
                    403,
                    "LOADING_SLOT_BUSY"
                )

            old_location = current["location"]

            new_state = state.reach_loading_point(vehicle)

            v2v.bus.claim_slot(vehicle)

            events.log_location_change(vehicle, old_location, new_state["location"])
            events.log_brake_applied(vehicle, "Reached loading point")

            return success(
                "Vehicle reached loading point.",
                {"vehicle": new_state, **sync_and_report(vehicle)}
            )

        except state.TransitionBlocked as exc:
            return blocked(vehicle, exc)

        except Exception as exc:
            events.log_error("Failed to move vehicle to loading point.", vehicle, {"error": str(exc)})
            return error(str(exc), 400, "LOADING_POINT_FAILED")


@router.post("/api/loading/start", tags=["Commands"], summary="Start loading at Stop 2", dependencies=[Depends(require_role("operator"))], openapi_extra=VEHICLE_BODY)
def start_loading(data: dict = Depends(json_body)):
    """403: EMERGENCY_STOP_ACTIVE, NOT_AT_LOADING_POINT, RADAR_OBSTACLE,
    BRAKE_NOT_APPLIED, LOAD_ALREADY_COMPLETE, QR_NOT_DETECTED."""

    vehicle = vehicle_from_body(data)

    if not vehicle:
        return invalid_vehicle()

    with command_lock(vehicle):

        busy = autopilot_active(vehicle)

        if busy:
            return busy

        try:

            refresh_for_command(vehicle)

            validation = safety.validate_loading_command(vehicle)

            if not validation["allowed"]:
                events.log_command(vehicle, "START_LOADING", False, validation["reason"])
                events.log_safety_block(vehicle, validation["reason"], validation["code"])
                return error(validation["reason"], 403, validation["code"])

            new_state = state.start_loading(vehicle)

            events.log_loading_started(vehicle)
            events.log_command(vehicle, "START_LOADING", True)

            return success(
                "Loading started.",
                {"vehicle": new_state, **sync_and_report(vehicle)}
            )

        except state.TransitionBlocked as exc:
            return blocked(vehicle, exc)

        except Exception as exc:
            return error(str(exc), 400, "LOADING_START_FAILED")


@router.post("/api/loading/update", tags=["Commands"], summary="Update the load percentage", dependencies=[Depends(require_role("operator"))], openapi_extra=documented_body(LoadingUpdate))
def update_loading(data: dict = Depends(json_body)):
    """400: MISSING_PERCENTAGE, INVALID_PERCENTAGE, LOAD_DECREASE_REJECTED
    (a lower value than the current load; send "force": true to correct a
    reading). 403: the same blocks as loading start."""

    vehicle = vehicle_from_body(data)

    if not vehicle:
        return invalid_vehicle()

    percentage = data.get("percentage")

    if percentage is None:
        percentage = data.get("load_percentage")

    if percentage is None:
        return error("Loading percentage is required.", 400, "MISSING_PERCENTAGE")

    try:
        percentage = float(percentage)
    except (TypeError, ValueError):
        return error("Percentage must be numeric.", 400, "INVALID_PERCENTAGE")

    if not 0 <= percentage <= 100:
        return error("Percentage must be between 0 and 100.", 400, "INVALID_PERCENTAGE")

    force = parse_bool(data.get("force"), False)

    with command_lock(vehicle):

        busy = autopilot_active(vehicle)

        if busy:
            return busy

        try:

            validation = safety.validate_loading_command(vehicle)

            if not validation["allowed"]:
                events.log_safety_block(vehicle, validation["reason"], validation["code"])
                return error(validation["reason"], 403, validation["code"])

            current_load = state.get_vehicle_state(vehicle)["load_percentage"]

            if percentage < current_load and not force:
                return error(
                    f"Load cannot go down from {current_load:g}% to {percentage:g}%. "
                    'Send "force": true to correct a wrong reading.',
                    400,
                    "LOAD_DECREASE_REJECTED"
                )

            new_state = state.update_loading(vehicle, percentage)

            events.log_loading_progress(vehicle, new_state["load_percentage"])

            if new_state["load_percentage"] >= 100:
                events.log_loading_complete(vehicle)

            return success(
                "Loading updated.",
                {"vehicle": new_state, **sync_and_report(vehicle)}
            )

        except state.TransitionBlocked as exc:
            return blocked(vehicle, exc)

        except Exception as exc:
            return error(str(exc), 400, "LOADING_UPDATE_FAILED")


@router.post("/api/warehouse", tags=["Commands"], summary="Move a loaded vehicle to the warehouse (Stop 3)", dependencies=[Depends(require_role("operator"))], openapi_extra=VEHICLE_BODY)
def move_to_warehouse(data: dict = Depends(json_body)):
    """403: EMERGENCY_STOP_ACTIVE, INVALID_LOCATION, LOAD_NOT_COMPLETE,
    RADAR_OBSTACLE, OBSTACLE_DETECTED.

    Call POST /api/complete afterwards to finish the journey and hand over
    to the next vehicle."""

    vehicle = vehicle_from_body(data)

    if not vehicle:
        return invalid_vehicle()

    with command_lock(vehicle):

        busy = autopilot_active(vehicle)

        if busy:
            return busy

        try:

            refresh_for_command(vehicle)

            validation = safety.validate_warehouse_command(vehicle)

            if not validation["allowed"]:
                events.log_safety_block(vehicle, validation["reason"], validation["code"])
                return error(validation["reason"], 403, validation["code"])

            old_location = state.get_vehicle_state(vehicle)["location"]

            new_state = state.reach_warehouse(vehicle)

            events.log_location_change(vehicle, old_location, new_state["location"])
            events.log_warehouse_reached(vehicle)

            return success(
                "Vehicle reached warehouse.",
                {"vehicle": new_state, **sync_and_report(vehicle)}
            )

        except state.TransitionBlocked as exc:
            return blocked(vehicle, exc)

        except Exception as exc:
            events.log_error("Failed to move vehicle to warehouse.", vehicle, {"error": str(exc)})
            return error(str(exc), 400, "WAREHOUSE_FAILED")


@router.post("/api/complete", tags=["Commands"], summary="Complete the journey and hand over to the next vehicle", dependencies=[Depends(require_role("operator"))], openapi_extra=documented_body(CompleteJourney))
def complete_journey(data: dict = Depends(json_body)):
    """The canonical end of a cycle. Requires the vehicle at Stop 3. Marks it
    COMPLETED, then negotiates the V2V handover with the next vehicle in fleet
    order (wrapping to the first) over the message bus: HANDOVER_REQUEST,
    ACCEPT or REJECT (with a reason), HANDOVER_COMPLETE, each with a timeout and
    one retry. On success the next vehicle is ready at Stop 1. `data.handover`
    reports the outcome (completed, rejected, timeout) and the messages. Send
    "handover": false for the last vehicle of a run.

    403: EMERGENCY_STOP_ACTIVE (this vehicle), NEXT_VEHICLE_EMERGENCY (the next
    vehicle is under emergency stop; nothing is changed). A handover never
    clears the next vehicle's radar or obstacle reading."""

    vehicle = vehicle_from_body(data)

    if not vehicle:
        return invalid_vehicle()

    handover = parse_bool(data.get("handover"), True)

    next_vehicle = fleet.next_vehicle(vehicle) if handover else None

    with command_lock(vehicle, next_vehicle):

        busy = autopilot_active(vehicle)

        if busy:
            return busy

        try:

            if state.get_vehicle_state(vehicle)["emergency_stop"]:
                return error("Vehicle is under emergency stop.", 403, "EMERGENCY_STOP_ACTIVE")

            if (
                next_vehicle not in (None, vehicle)
                and state.get_vehicle_state(next_vehicle)["emergency_stop"]
            ):
                return error(
                    f"{next_vehicle} is under emergency stop. Release it first; "
                    "nothing was changed.",
                    403,
                    "NEXT_VEHICLE_EMERGENCY"
                )

            completed_state = state.complete_journey(vehicle)

            events.log_journey_completed(vehicle)

            synced = {vehicle: sync_vehicle_to_thingsboard(vehicle)}

            if not handover:
                return success(
                    f"{vehicle.title()} completed. No handover requested.",
                    {
                        "completed_vehicle": completed_state,
                        "next_vehicle": None,
                        "handover": {"status": "not_requested"},
                        **telemetry_report(synced)
                    }
                )

            # V2V HANDOVER, negotiated over the message bus:
            # HANDOVER_REQUEST -> ACCEPT / REJECT -> COMPLETE (+ ACK),
            # with a timeout and one retry per step. The next vehicle
            # changes its own state when HANDOVER_COMPLETE arrives.
            negotiation = v2v.bus.wait_for(
                v2v.bus.start_handover(vehicle, next_vehicle)
            )

            if negotiation.status == "completed":

                events.log_vehicle_handover(vehicle, next_vehicle)

                synced[next_vehicle] = sync_vehicle_to_thingsboard(next_vehicle)

                message = (
                    f"{vehicle.title()} completed. "
                    f"{next_vehicle.title()} is ready "
                    "for the next cycle."
                )
                next_state = negotiation.next_state

            else:

                message = (
                    f"{vehicle.title()} completed. Handover to "
                    f"{next_vehicle.title()} {negotiation.status}"
                    + (f": {negotiation.reason}" if negotiation.reason else "")
                    + "."
                )
                next_state = state.get_vehicle_state(next_vehicle)

            return success(
                message,
                {
                    "completed_vehicle": completed_state,
                    "next_vehicle": next_state,
                    "handover": negotiation.to_dict(),
                    **telemetry_report(synced)
                }
            )

        except state.TransitionBlocked as exc:
            return blocked(vehicle, exc)

        except Exception as exc:
            events.log_error("Journey completion failed.", vehicle, {"error": str(exc)})
            return error(str(exc), 400, "COMPLETE_FAILED")


@router.post("/api/manual-control", tags=["Commands"], summary="Manual drive command", dependencies=[Depends(require_role("operator"))], openapi_extra=documented_body(ManualCommand))
def manual_control(data: dict = Depends(json_body)):
    """FORWARD, BACKWARD, LEFT, RIGHT or STOP. 403: EMERGENCY_STOP_ACTIVE; for
    movement also RADAR_OBSTACLE, OBSTACLE_DETECTED, LOADING_IN_PROGRESS.
    STOP is always allowed except under emergency stop."""

    vehicle = vehicle_from_body(data)

    if not vehicle:
        return invalid_vehicle()

    command = str(data.get("command", "")).upper().strip()

    directions = {
        "FORWARD": "forward",
        "BACKWARD": "backward",
        "LEFT": "left",
        "RIGHT": "right",
        "STOP": "stopped"
    }

    if command not in directions:
        return error("Invalid command.", 400, "INVALID_COMMAND")

    with command_lock(vehicle):

        if autopilot.controller.is_auto(vehicle):

            if command != "STOP":
                return autopilot_active(vehicle)

            # STOP on an AUTO vehicle is an operator takeover: the
            # autopilot disengages and the vehicle holds where it is.
            autopilot.controller.disable([vehicle], reason="MANUAL_STOP")
            events.log_command(vehicle, command, True)

            return success(
                "Autopilot disengaged; the vehicle holds where it is.",
                {"vehicle": state.get_vehicle_state(vehicle), **sync_and_report(vehicle)}
            )

        validation = safety.validate_manual_command(vehicle, command)

        if not validation["allowed"]:
            events.log_command(vehicle, command, False, validation["reason"])
            return error(validation["reason"], 403, validation["code"])

        speed = 0 if command == "STOP" else 15

        try:
            new_state = state.manual_drive(vehicle, command, directions[command], speed)
        except state.TransitionBlocked as exc:
            return blocked(vehicle, exc)

        events.log_command(vehicle, command, True)

        return success(
            f"{command} command executed.",
            {"vehicle": new_state, **sync_and_report(vehicle)}
        )


# =========================================================
# SENSOR INPUTS (SIMULATED)
# =========================================================

@router.post("/api/sensors/qr", tags=["Sensors"], summary="Set the QR/AprilTag reading", dependencies=[Depends(require_role("operator"))], openapi_extra=documented_body(QrInput))
def qr_sensor(data: dict = Depends(json_body)):
    """The reading is honoured (it used to be forced to "detected"). START
    needs the marker at Stop 1 and loading needs it at Stop 2, unless
    QR_ALWAYS_DETECTED=true. The simulation sets it again whenever the
    vehicle arrives at a stop."""

    vehicle = vehicle_from_body(data)

    if not vehicle:
        return error("Provide vehicle.", 400, "INVALID_VEHICLE")

    status = data.get("status")

    if status is None:
        detected = parse_bool(data.get("detected"), False)
        status = state.QR_DETECTED if detected else state.QR_NOT_DETECTED

    try:

        new_state = state.set_qr_status(vehicle, status)

        if new_state.get("qr_status") == state.QR_DETECTED:
            events.log_qr_detected(vehicle)
        else:
            events.log_qr_not_detected(vehicle)

        return success(
            "QR status updated.",
            {"vehicle": new_state, **sync_and_report(vehicle)}
        )

    except Exception as exc:
        return error(str(exc), 400, "QR_UPDATE_FAILED")


@router.post("/api/sensors/radar", tags=["Sensors"], summary="Set the radar reading", dependencies=[Depends(require_role("operator"))], openapi_extra=documented_body(RadarInput))
def radar_sensor(data: dict = Depends(json_body)):

    vehicle = vehicle_from_body(data)

    if not vehicle:
        return error("Provide vehicle.", 400, "INVALID_VEHICLE")

    status = data.get("status")

    if status is None:
        obstacle = parse_bool(data.get("obstacle"), False)
        status = state.RADAR_OBSTACLE if obstacle else state.RADAR_CLEAR

    try:

        new_state = state.set_radar_status(vehicle, status)

        if status == state.RADAR_OBSTACLE:
            events.log_radar_obstacle(vehicle)
            events.log_brake_applied(vehicle, "Radar obstacle")
        else:
            events.log_radar_clear(vehicle)

        return success(
            "Radar status updated.",
            {"vehicle": new_state, **sync_and_report(vehicle)}
        )

    except Exception as exc:
        return error(str(exc), 400, "RADAR_UPDATE_FAILED")


@router.post("/api/sensors/brake", tags=["Sensors"], summary="Apply or release the brake", dependencies=[Depends(require_role("operator"))], openapi_extra=documented_body(BrakeInput))
def brake_sensor(data: dict = Depends(json_body)):

    vehicle = vehicle_from_body(data)

    if not vehicle:
        return error("Provide vehicle.", 400, "INVALID_VEHICLE")

    applied = parse_bool(data.get("applied"), True)

    try:

        new_state = local_set_brake(vehicle, applied)

        if applied:
            events.log_brake_applied(vehicle, "Brake controller")
        else:
            events.log_brake_released(vehicle)

        return success(
            "Brake state updated.",
            {"vehicle": new_state, **sync_and_report(vehicle)}
        )

    except Exception as exc:
        return error(str(exc), 400, "BRAKE_UPDATE_FAILED")
