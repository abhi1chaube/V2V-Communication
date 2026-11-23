# =========================================================
# AUTOPILOT AND SCENARIO ENDPOINTS (/api/v1/autopilot, /api/v1/scenarios)
# =========================================================
#
# Importing this module also wires the autopilot (services/) to
# the API layer: its ThingsBoard push (api/sync.py) and the
# per-vehicle command locks (api/commands.py), so the autopilot
# and an operator's command never act on a vehicle at once.
# =========================================================

from typing import Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

from services import autopilot
from services import fleet
from services import scenarios

from . import commands
from .auth import require_role
from .commands import command_lock
from .common import documented_body, error, json_body, parse_bool, require_local, success
from .sync import autopilot_sync


controller = autopilot.controller
runner = scenarios.runner

controller.on_transition = autopilot_sync
controller.try_lock = commands.try_lock


router = APIRouter(tags=["Autopilot"])


class AutopilotScope(BaseModel):
    vehicle: Optional[str] = Field(None, description="One vehicle (name or alias). Omit, or send fleet: true, for all.",
                                   examples=["Vehicle_01"])
    fleet: Optional[bool] = Field(None, examples=[True])
    dispatch: Optional[str] = Field(None, description="enable only: handover or concurrent.", examples=["handover"])


class TimeScale(BaseModel):
    scale: float = Field(description="Simulation speed, 0.25 to 8.", examples=[2])


class ScenarioRun(BaseModel):
    seed: Optional[int] = Field(None, description="Override the scenario's seed.", examples=[42])
    time_scale: Optional[float] = Field(None, description="Also set the simulation speed (0.25 to 8).", examples=[4])


def _targets(data):
    """(vehicle IDs, error response or None) from {vehicle} / {fleet}."""

    if data.get("vehicle") and not parse_bool(data.get("fleet"), False):
        vehicle = fleet.resolve(data.get("vehicle"))
        if vehicle is None:
            return None, error(f"Provide vehicle as one of: {fleet.describe()}.", 400, "INVALID_VEHICLE")
        return [vehicle], None

    return fleet.vehicle_ids(), None


# =========================================================
# AUTOPILOT
# =========================================================

@router.post(
    "/api/v1/autopilot/enable",
    summary="Put vehicles in AUTO (the backend drives the mission)",
    openapi_extra=documented_body(AutopilotScope, required=False),
)
def enable(data: dict = Depends(json_body)):
    """`{"vehicle": "Vehicle_01"}` or `{"fleet": true}` (also an empty body).
    Optional `"dispatch": "handover" | "concurrent"`.

    The autopilot drives the loop with the same safety checks as the REST
    commands; mission commands for an AUTO vehicle are refused with 409
    AUTOPILOT_ACTIVE until it is disabled. A vehicle under emergency stop or
    with a fault is not enabled (`refused`; 403 when it was the only one).
    409 AUTOPILOT_FAILED after a watchdog trip (POST /recover first)."""

    targets, problem = _targets(data)

    if problem:
        return problem

    try:
        with command_lock(*targets):
            result = controller.enable(targets, dispatch=data.get("dispatch"))
    except autopilot.AutopilotError as exc:
        return error(str(exc), exc.status, exc.code)

    if len(targets) == 1 and result["refused"]:
        code = result["refused"][targets[0]]
        return error(f"{targets[0]} cannot be put in AUTO: {code}.", 403, code, result)

    return success(
        f"Autopilot engaged for {', '.join(result['enabled']) or 'no vehicle'} "
        f"({controller.dispatch} dispatch).",
        {**result, "dispatch": controller.dispatch},
    )


@router.post(
    "/api/v1/autopilot/disable",
    summary="Back to MANUAL (the vehicle holds where it is)",
    openapi_extra=documented_body(AutopilotScope, required=False),
)
def disable(data: dict = Depends(json_body)):
    """`{"vehicle": ...}` or `{"fleet": true}` / empty body. A vehicle in the
    middle of its mission stops where it is (speed 0, brakes applied,
    mission HELD); the operator can then continue with the normal commands."""

    targets, problem = _targets(data)

    if problem:
        return problem

    with command_lock(*targets):
        disabled = controller.disable(targets)

    return success(
        f"Autopilot disengaged for {', '.join(disabled)}." if disabled else "No vehicle was in AUTO.",
        {"disabled": disabled},
    )


@router.get("/api/v1/autopilot/status", summary="Mode, plan and watchdog per vehicle")
def status():
    """Per vehicle: mode (MANUAL/AUTO), the autopilot's phase, status,
    next action, destination, distance, ETA, battery, fault, cycles. Plus
    dispatch policy, who holds the current cycle (handover dispatch), time
    scale, road obstacles and radio blackouts (scenarios), and the watchdog
    (ARMED, DISARMED or FAILED with the reason)."""

    return success("Autopilot status retrieved.", controller.status())


@router.get("/api/v1/autopilot/trace", summary="Recent autopilot decisions (simulated time)")
def trace(
    limit: int = Query(100, ge=1, le=autopilot.TRACE_SIZE),
    vehicle: Optional[str] = Query(None),
):
    """What the autopilot did and when (simulated seconds since the last
    reset or scenario start): starts, arrivals, loading, handovers, hazard
    holds, emergency brakes, scenario events. The same scenario with the same
    seed gives the same trace."""

    vehicle_id = fleet.resolve(vehicle) if vehicle else None

    if vehicle and vehicle_id is None:
        return error("Unknown vehicle.", 400, "INVALID_VEHICLE")

    items = [e for e in controller.trace if vehicle_id is None or e["vehicle"] == vehicle_id]

    return success("Autopilot trace retrieved.", {"count": len(items[-limit:]), "trace": items[-limit:]})


@router.post(
    "/api/v1/autopilot/time-scale",
    summary="Simulation speed (demo: 0.25x to 8x)",
    dependencies=[Depends(require_local)],
    openapi_extra=documented_body(TimeScale),
)
def time_scale(data: dict = Depends(json_body)):
    """Everything simulated (motion, radio, loading, battery) runs this many
    times faster than the wall clock. 400 INVALID_TIME_SCALE. Only accepted
    from the computer running the backend."""

    try:
        scale = controller.engine.set_time_scale(data.get("scale"))
    except (TypeError, ValueError):
        return error("scale must be a number from 0.25 to 8.", 400, "INVALID_TIME_SCALE")

    return success(f"Simulation speed set to {scale:g}x.", {"time_scale": scale})


@router.post(
    "/api/v1/autopilot/recover",
    summary="Clear a watchdog trip (the fleet stays stopped)",
    dependencies=[Depends(require_local)],
)
def recover():
    """After the watchdog latched a fleet emergency stop, and once the tick
    loop runs again: clears FAILED so the autopilot can be enabled again. The
    vehicles stay emergency-stopped until POST /api/emergency-release. 409
    AUTOPILOT_STILL_STALLED while the loop is still not running."""

    try:
        result = controller.recover()
    except autopilot.AutopilotError as exc:
        return error(str(exc), exc.status, exc.code)

    return success(
        "Autopilot watchdog reset. Release the emergency stop before enabling it again."
        if result["recovered"] else result["message"],
        result,
    )


# =========================================================
# SCENARIOS
# =========================================================

@router.get("/api/v1/scenarios", tags=["Scenarios"], summary="List scenarios, the running one, the last report")
def list_scenarios():
    """Scenarios in BACKEND/scenarios/*.json (invalid files are listed with
    the reason), the active run (time, next events, log) and the report of the
    last finished run (cycles, handovers, hazard holds, emergency brakes,
    smallest gap between vehicles)."""

    return success("Scenarios retrieved.", runner.overview())


@router.get("/api/v1/scenarios/{name}", tags=["Scenarios"], summary="One scenario: setup and timeline")
def get_scenario(name: str):

    try:
        return success("Scenario retrieved.", runner.get(name))
    except scenarios.ScenarioError as exc:
        return error(str(exc), exc.status, exc.code)


@router.post(
    "/api/v1/scenarios/{name}/run",
    tags=["Scenarios"],
    summary="Run a scenario (resets the fleet cycle)",
    dependencies=[Depends(require_local)],
    openapi_extra=documented_body(ScenarioRun, required=False),
)
def run_scenario(name: str, data: dict = Depends(json_body)):
    """**Resets the fleet cycle** (all vehicles back at Stop 1, MANUAL), the
    radio and the simulated clock, seeds the random streams, applies the
    scenario setup, enables the autopilot and plays the timeline. Only
    accepted from the computer running the backend.

    404 SCENARIO_NOT_FOUND; 409 SCENARIO_RUNNING, SCENARIO_NEEDS_VEHICLES
    (fleet too small), EMERGENCY_STOP_ACTIVE (release first),
    AUTOPILOT_FAILED."""

    vehicle_ids = fleet.vehicle_ids()

    try:
        with command_lock(*vehicle_ids):
            result = runner.start(name, seed=data.get("seed"), time_scale=data.get("time_scale"))
    except scenarios.ScenarioError as exc:
        return error(str(exc), exc.status, exc.code)
    except autopilot.AutopilotError as exc:
        return error(str(exc), exc.status, exc.code)
    except ValueError as exc:
        return error(str(exc), 400, "INVALID_TIME_SCALE")

    return success(f"Scenario {name} started.", result)


@router.post(
    "/api/v1/scenarios/stop",
    tags=["Scenarios"],
    summary="Stop the running scenario (vehicles hold where they are)",
    dependencies=[Depends(require_local)],
)
def stop_scenario():

    report = runner.stop("stopped")

    if report is None:
        return success("No scenario is running.", {"report": None})

    return success(f"Scenario {report['name']} stopped.", {"report": report})
