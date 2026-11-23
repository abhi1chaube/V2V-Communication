# =========================================================
# EMERGENCY STOP, RELEASE, RESET AND SAFETY SUMMARY
# =========================================================

from fastapi import APIRouter, Depends

from services import autopilot
from services import events
from services import fleet
from services import hardware
from services import safety
from services import scenarios
from services import state

from .auth import require_role
from .common import documented_body, error, json_body, parse_bool, success
from .schemas import FleetScope
from .sync import (
    call_hardware,
    sync_all_to_thingsboard,
    sync_failures,
    sync_vehicle_to_thingsboard,
    telemetry_report,
)

# These three routes never take the per-vehicle command locks
# (api/commands.py): an emergency stop must not wait behind a
# slow command.


router = APIRouter()


@router.post(
    "/api/emergency-stop",
    tags=["Safety"],
    summary="Emergency stop (whole fleet)",
    openapi_extra=documented_body(FleetScope, required=False)
)
def emergency_stop():
    """**Stops every vehicle.** The hardware emergency stop is triggered once,
    then each vehicle is set to emergency stop (speed 0, brakes applied), even
    if one of them fails. No vehicle is needed in the body.

    The stop is latched: ThingsBoard telemetry cannot clear it, only Release
    or Reset can. Safe to call again. 500 EMERGENCY_STOP_PARTIAL: some vehicles
    failed, all others are stopped. 503 HARDWARE_EMERGENCY_STOP_FAILED:
    vehicles stopped on the dashboard, hardware not; call again."""

    vehicle_ids = fleet.vehicle_ids()

    hw = call_hardware(hardware.activate_emergency_stop)

    stopped = {}
    failed = {}

    for vehicle_id in vehicle_ids:

        try:
            stopped[vehicle_id] = state.emergency_stop(vehicle_id)
            events.log_emergency_stop(vehicle_id, "Fleet emergency stop requested.")
            events.log_brake_applied(vehicle_id, "Emergency stop")
        except Exception as exc:
            failed[vehicle_id] = str(exc)

    # The latch wins over the autopilot: every AUTO vehicle goes to
    # MANUAL and a running scenario ends. It never resumes by itself.
    try:
        autopilot.controller.on_emergency_stop()
        scenarios.runner.abort("aborted (emergency stop)")
        stopped = {v: state.get_vehicle_state(v) for v in stopped}
    except Exception as exc:
        events.log_error("Autopilot disengage after emergency stop failed.", details={"error": str(exc)})

    # A poll that read ThingsBoard before this moment must not
    # overwrite the stop with old values.
    state.ignore_telemetry_before(list(stopped))

    sync_results = sync_all_to_thingsboard()

    data = {
        "vehicles": stopped,
        "failed": failed,
        "hardware": hw,
        "thingsboard": sync_results,
        "thingsboard_failed": sync_failures(sync_results),
        **telemetry_report(sync_results)
    }

    if failed:
        events.log_error("Emergency stop failed for some vehicles.", details=failed)
        return error(
            "Emergency stop failed for: "
            + ", ".join(failed)
            + ". All other vehicles are stopped.",
            500,
            "EMERGENCY_STOP_PARTIAL",
            data
        )

    if not hw.get("success"):
        events.log_error("Hardware emergency stop failed.", details=hw)
        return error(
            "Vehicles are stopped on the dashboard, but the hardware "
            "emergency stop failed. Press EMERGENCY STOP again.",
            503,
            "HARDWARE_EMERGENCY_STOP_FAILED",
            data
        )

    return success(
        (
            "Fleet emergency stop activated."
            if vehicle_ids
            else "Emergency stop activated (no vehicles registered)."
        ),
        data
    )


@router.post(
    "/api/emergency-release",
    tags=["Safety"],
    summary="Release the emergency stop",
    dependencies=[Depends(require_role("operator"))],
    openapi_extra=documented_body(FleetScope, required=False)
)
def emergency_release(data: dict = Depends(json_body)):
    """`{"fleet": true}` (or an empty body) releases every stopped vehicle;
    `{"vehicle": "Vehicle_01"}` releases only that one. Released vehicles stay
    stopped with brakes applied (status READY) until START.

    The hardware latch is released first, and only when no vehicle stays
    stopped. If that fails, nothing is released (503
    HARDWARE_EMERGENCY_RELEASE_FAILED)."""

    fleet_wide = (
        parse_bool(data.get("fleet"), False)
        or not data.get("vehicle")
    )

    stopped = [
        vehicle_id
        for vehicle_id in fleet.vehicle_ids()
        if state.get_vehicle_state(vehicle_id)["emergency_stop"]
    ]

    if fleet_wide:

        targets = stopped

    else:

        vehicle = fleet.resolve(data.get("vehicle"))

        if vehicle is None:
            return error(
                f"Provide vehicle as one of: {fleet.describe()}.",
                400,
                "INVALID_VEHICLE"
            )

        targets = [vehicle]

    remaining = [v for v in stopped if v not in targets]

    hw = None

    if not remaining:

        hw = call_hardware(hardware.release_emergency_stop)

        if not hw.get("success"):
            return error(
                "Hardware emergency stop could not be released. "
                "Nothing was released.",
                503,
                "HARDWARE_EMERGENCY_RELEASE_FAILED",
                {"hardware": hw}
            )

    released = {}

    for vehicle_id in targets:
        released[vehicle_id] = state.release_emergency_stop(vehicle_id)
        events.log_emergency_released(vehicle_id)

    # Old ThingsBoard values (emergency_stop: true) must not
    # re-apply the stop that was just released.
    state.ignore_telemetry_before(targets)

    sync_results = {
        vehicle_id: sync_vehicle_to_thingsboard(vehicle_id)
        for vehicle_id in targets
    }

    return success(
        (
            f"Emergency stop released for {', '.join(targets)}. "
            "Vehicles remain stopped with brakes applied."
            if targets
            else "No vehicle was emergency-stopped."
        ),
        {
            "vehicles": released,
            "still_stopped": remaining,
            "hardware": hw,
            "thingsboard": sync_results,
            "thingsboard_failed": sync_failures(sync_results),
            **telemetry_report(sync_results)
        }
    )


@router.post("/api/reset", tags=["Safety"], summary="Reset the fleet cycle", dependencies=[Depends(require_role("operator"))])
def reset_cycle():
    """**Resets every vehicle** to Stop 1, load 0, brakes applied, and clears
    any emergency stop. The hardware latch is released first; if that fails,
    nothing is reset. ThingsBoard values recorded before the reset are ignored
    afterwards. `thingsboard_failed` lists vehicles whose new state could not
    be sent to ThingsBoard."""

    hw = call_hardware(hardware.release_emergency_stop)

    if not hw.get("success"):
        return error(
            "Reset blocked: the hardware emergency stop could not be "
            "released. Nothing was reset.",
            503,
            "HARDWARE_EMERGENCY_RELEASE_FAILED",
            {"hardware": hw}
        )

    try:

        # Back to MANUAL, no scenario, no injected obstacles/blackouts.
        scenarios.runner.abort("aborted (fleet reset)")
        autopilot.controller.reset_all()

        result = state.reset_cycle()

        # A poll that read ThingsBoard before the reset must not
        # bring back the old positions and loads.
        state.ignore_telemetry_before(list(result))

        events.log_reset()

        sync_results = sync_all_to_thingsboard()

        failed = sync_failures(sync_results)

        return success(
            (
                "Fleet cycle reset."
                if not failed
                else (
                    "Fleet cycle reset, but ThingsBoard was not updated for "
                    + ", ".join(failed)
                    + ". If a vehicle or simulator keeps reporting its old "
                    "state, the dashboard will show it again."
                )
            ),
            {
                "vehicles": result,
                "hardware": hw,
                "thingsboard": sync_results,
                "thingsboard_failed": failed,
                **telemetry_report(sync_results)
            }
        )

    except Exception as exc:
        events.log_error("Fleet reset failed.", details={"error": str(exc)})
        return error(str(exc), 500, "RESET_FAILED")


@router.get("/api/safety", tags=["Safety"], summary="Safety summary for every vehicle")
def get_safety():

    try:
        return success("Safety status retrieved.", safety.get_safety_summary())
    except Exception as exc:
        return error(str(exc), 500, "SAFETY_ERROR")


@router.get("/api/safety/{vehicle}", tags=["Safety"], summary="Safety summary for one vehicle")
def get_vehicle_safety(vehicle: str):

    vehicle = fleet.resolve(vehicle)

    if vehicle is None:
        return error("Invalid vehicle.", 400, "INVALID_VEHICLE")

    try:
        return success(
            "Vehicle safety status retrieved.",
            safety.get_sensor_safety(vehicle)
        )
    except Exception as exc:
        return error(str(exc), 500, "SAFETY_ERROR")
