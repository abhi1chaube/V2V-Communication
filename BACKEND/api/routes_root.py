# =========================================================
# ROOT, HEALTH AND DEVICE ENDPOINTS
# =========================================================

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from services import alerts
from services import autopilot
from services import db
from services import fleet
from services import hardware
from services import live
from services import thingsboard
from services import world
from services.hardware import HardwareController
from services.servo import set_servo_angle

from .common import API_VERSION, documented_body, error, json_body, success
from .schemas import DeviceCommand, ServoCommand


router = APIRouter()

hardware_controller = HardwareController()


@router.get("/", tags=["System"], summary="Backend banner")
def home():

    return success(
        "V2V Fleet Control Backend online.",
        {
            "service": "V2V Fleet Control System",
            "version": API_VERSION,
            "swagger": "/apidocs/"
        }
    )


@router.get("/api/health", tags=["Fleet"], summary="Service health")
def health():
    """Checks the ThingsBoard login and reports the fleet size, the
    vehicles.json path and any fleet configuration problems.
    Always 200, also when ThingsBoard is offline."""

    try:
        tb = thingsboard.test_connection()
    except Exception as exc:
        tb = {
            "connected": False,
            "message": str(exc)
        }

    return JSONResponse({
        "success": True,
        "backend": "online",
        "services": {
            "events": "online",
            "safety": "online",
            "state": "online",
            "thingsboard": "online" if tb.get("connected") else "offline",
            "hardware": "online"
        },
        "thingsboard": tb,
        "fleet": {
            "count": fleet.fleet_size(),
            "data_file": str(fleet.DATA_FILE),
            "problems": fleet.validate()
        },
        "world": world.engine.status(),
        # Phase 4: "FAILED" here after a watchdog trip.
        "autopilot": autopilot.controller.health(),
        # Phase 5: the SQLite writer, the SSE push feed, and open alerts.
        "db": db.statistics(),
        "stream": live.hub.status(),
        "alerts": {"open": len(alerts.list_alerts(state=alerts.STATE_OPEN)),
                   "acknowledged": len(alerts.list_alerts(state=alerts.STATE_ACKNOWLEDGED))},
        "swagger": "/apidocs/"
    })


@router.get("/api/hardware", tags=["System"], summary="Simulated hardware status")
def hardware_status():

    try:
        return success(
            "Hardware status retrieved.",
            hardware.get_hardware_status()
        )
    except Exception as exc:
        return error(str(exc), 500, "HARDWARE_ERROR")


@router.post(
    "/api/servo",
    tags=["System"],
    summary="Move the (simulated) servo",
    openapi_extra=documented_body(ServoCommand)
)
def control_servo(data: dict = Depends(json_body)):
    """400 INVALID_ANGLE unless `angle` is a whole number from 0 to 180.
    (An out-of-range value used to crash with a generic 500.)"""

    raw = data.get("angle", 90)

    try:
        angle = float(raw)
    except (TypeError, ValueError):
        angle = None

    if (
        isinstance(raw, bool)
        or angle is None
        or not angle.is_integer()
        or not 0 <= angle <= 180
    ):
        return error(
            "Angle must be a whole number from 0 to 180.",
            400,
            "INVALID_ANGLE"
        )

    result = set_servo_angle(int(angle))

    return JSONResponse(result)


@router.post(
    "/api/vehicle/command",
    tags=["System"],
    summary="Send a raw command to the (simulated) controller",
    openapi_extra=documented_body(DeviceCommand)
)
def vehicle_command(data: dict = Depends(json_body)):

    command = data.get("command")

    print(f"[COMMAND RECEIVED] {command}")

    hardware_controller.execute_command(command)

    return JSONResponse({
        "success": True,
        "command": command
    })
