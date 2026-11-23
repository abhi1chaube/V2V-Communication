# =========================================================
# EVENTS SERVICE
# =========================================================
# Central event logger for the V2V Fleet Control System.
#
# Responsibilities:
# - Store recent system events
# - Record vehicle actions
# - Record sensor events
# - Record safety events
# - Record loading progress
# - Provide events to Flask/frontend
#
# This is an in-memory event log for the current development
# version. Later it can be moved to a database without
# changing the frontend API significantly.
# =========================================================

import itertools
import json
import threading
from copy import deepcopy

from . import db
from . import fleet
from . import j2735
from .timeutil import utc_iso


# =========================================================
# CONFIGURATION
# =========================================================

MAX_EVENTS = 200

# =========================================================
# VEHICLE DISPLAY NAME
# =========================================================

def vehicle_name(vehicle):

    vehicle_id = fleet.resolve(vehicle)

    if vehicle_id is None:
        return str(vehicle).upper()

    return fleet.short_name(vehicle_id)


# =========================================================
# INTERNAL EVENT STORAGE
# =========================================================

_events = []

_events_lock = threading.RLock()

# Called with the new event dict after it is stored (services/live.py
# subscribes here to push it onto the SSE "events" channel). Never
# raises into the caller: a broken subscriber is caught and printed.
subscribers = []

# Unique, increasing event IDs. (len(_events) + 1 repeated once
# the log reached MAX_EVENTS; the dashboard keys rows by id.)
_event_ids = itertools.count(1)


# =========================================================
# GET CURRENT TIMESTAMP
# =========================================================

def current_timestamp():
    """UTC, e.g. 2026-11-16T06:08:41Z. The dashboard shows it in local time."""

    return utc_iso()


# =========================================================
# ADD EVENT
# =========================================================

def add_event(
    message,
    vehicle=None,
    event_type="system",
    severity="info",
    details=None
):

    if message is None:

        message = ""

    message = str(message).strip()

    if not message:

        return None


    # Store registered vehicles under their canonical ID so
    # get_vehicle_events() can filter on it.
    vehicle = fleet.resolve(vehicle) or vehicle


    event = {

        "id": None,

        "timestamp": current_timestamp(),

        "vehicle": vehicle,

        "event_type": event_type,

        "severity": severity,

        "message": message,

        "details": details or {}
    }


    with _events_lock:

        event["id"] = next(_event_ids)

        _events.insert(
            0,
            event
        )


        # Keep only recent events.

        if len(_events) > MAX_EVENTS:

            del _events[
                MAX_EVENTS:
            ]

        stored = deepcopy(event)

    # Outside the lock: persistence and notification never block
    # the caller for long, and must not be able to deadlock on it.
    db.record(
        "activity_log",
        timestamp_utc=stored["timestamp"], vehicle=stored["vehicle"],
        event_type=stored["event_type"], severity=stored["severity"],
        message=stored["message"], details_json=json.dumps(stored["details"], default=str),
    )

    for subscriber in list(subscribers):
        try:
            subscriber(stored)
        except Exception as exc:
            print(f"[events] subscriber failed: {exc}")

    return stored


# =========================================================
# GET EVENTS
# =========================================================

def get_events(
    limit=50
):

    try:

        limit = int(
            limit
        )

    except (
        TypeError,
        ValueError
    ):

        limit = 50


    limit = max(
        1,
        min(limit, MAX_EVENTS)
    )


    with _events_lock:

        return deepcopy(
            _events[:limit]
        )


# =========================================================
# GET EVENTS FOR VEHICLE
# =========================================================

def get_vehicle_events(
    vehicle,
    limit=50
):

    vehicle = fleet.resolve(
        vehicle
    ) or str(vehicle).strip()


    try:

        limit = int(
            limit
        )

    except (
        TypeError,
        ValueError
    ):

        limit = 50


    limit = max(
        1,
        min(limit, MAX_EVENTS)
    )


    with _events_lock:

        filtered = [

            event

            for event in _events

            if event.get(
                "vehicle"
            ) == vehicle
        ]


        return deepcopy(
            filtered[:limit]
        )


# =========================================================
# CLEAR EVENTS
# =========================================================

def clear_events():

    with _events_lock:

        _events.clear()

        return True


# =========================================================
# SYSTEM EVENT
# =========================================================

def log_system_event(
    message,
    severity="info",
    details=None
):

    return add_event(

        message=message,

        vehicle=None,

        event_type="system",

        severity=severity,

        details=details
    )


# =========================================================
# VEHICLE EVENT
# =========================================================

def log_vehicle_event(
    vehicle,
    message,
    event_type="vehicle",
    severity="info",
    details=None
):

    return add_event(

        message=message,

        vehicle=vehicle,

        event_type=event_type,

        severity=severity,

        details=details
    )


# =========================================================
# START EVENT
# =========================================================

def log_vehicle_started(
    vehicle,
    location
):

    return log_vehicle_event(

        vehicle,

        # The J2735-styled BSM itself is logged by log_bsm().
        f"{vehicle_name(vehicle)} started from {location}.",

        event_type="movement",

        details={
            "location": location
        }
    )

# =========================================================
# READY EVENT
# =========================================================

def log_vehicle_ready(
    vehicle,
    location
):

    return log_vehicle_event(

        vehicle,

        f"{vehicle.title()} vehicle is ready.",

        event_type="state",

        details={

            "location": location
        }
    )


# =========================================================
# LOCATION EVENT
# =========================================================

def log_location_change(
    vehicle,
    old_location,
    new_location
):

    return log_vehicle_event(

        vehicle,

        (
            f"{vehicle.title()} vehicle "
            f"moved from {old_location} "
            f"to {new_location}."
        ),

        event_type="movement",

        details={

            "old_location": old_location,

            "new_location": new_location
        }
    )


# =========================================================
# LOADING START EVENT
# =========================================================

def log_loading_started(
    vehicle
):

    return log_vehicle_event(

        vehicle,

       f"[STATUS] {vehicle_name(vehicle)} State Transition → LOADING",

        event_type="status"
    )

# =========================================================
# LOADING PROGRESS EVENT
# =========================================================

def log_loading_progress(
    vehicle,
    percentage
):

    return log_vehicle_event(

        vehicle,

      f"[STATUS] {vehicle_name(vehicle)} Cargo Load Progress = {percentage}%",
        event_type="status",

        details={
            "load_percent": percentage
        }
    )

# =========================================================
# LOADING COMPLETE EVENT
# =========================================================

def log_loading_complete(
    vehicle
):

    return log_vehicle_event(

        vehicle,

       f"[EVENT] {vehicle_name(vehicle)} Cargo Loading Complete",


        event_type="status",

        details={
            "load_percent": 100
        }
    )


# =========================================================
# WAREHOUSE EVENT
# =========================================================

def log_warehouse_reached(
    vehicle
):

    return log_vehicle_event(

        vehicle,

       f"[EVENT] {vehicle_name(vehicle)} arrived at Destination Zone",

        event_type="event"
    )


# =========================================================
# JOURNEY COMPLETE EVENT
# =========================================================
def log_journey_completed(
    vehicle
):

    return log_vehicle_event(

        vehicle,

    f"[MISSION] {vehicle_name(vehicle)} Mission Completed Successfully",
        event_type="event",

        severity="success"
    )


# =========================================================
# QR DETECTED EVENT
# =========================================================
def log_qr_detected(
    vehicle
):

    return log_vehicle_event(

        vehicle,

        f"[SENSOR] {vehicle_name(vehicle)} AprilTag detection confirmed",

        event_type="sensor",

        severity="success"
    )


# =========================================================
# QR NOT DETECTED EVENT
# =========================================================

def log_qr_not_detected(
    vehicle
):

    return log_vehicle_event(

        vehicle,

        (
            f"{vehicle.title()} vehicle "
            "QR code not detected."
        ),

        event_type="sensor",

        severity="warning"
    )


# =========================================================
# RADAR CLEAR EVENT
# =========================================================

def log_radar_clear(
    vehicle
):

    return log_vehicle_event(

        vehicle,

       f"[SAFETY] {vehicle_name(vehicle)} Route Clearance Confirmed",
        event_type="status",

        severity="success"
    )


# =========================================================
# RADAR OBSTACLE EVENT
# =========================================================

def log_radar_obstacle(
    vehicle
):

    return log_vehicle_event(

        vehicle,

       f"[SAFETY] {vehicle_name(vehicle)} Obstacle Detected",
        event_type="safety",

        severity="critical"
    )


# =========================================================
# BRAKE APPLIED EVENT
# =========================================================

def log_brake_applied(
    vehicle,
    reason="Safety stop"
):

    return log_vehicle_event(

        vehicle,

      f"[SAFETY] {vehicle_name(vehicle)} Brake Applied",
        event_type="safety",

        severity="warning",

        details={
            "reason": reason
        }
    )

# =========================================================
# BRAKE RELEASED EVENT
# =========================================================

def log_brake_released(
    vehicle
):

    return log_vehicle_event(

        vehicle,

        (
            f"{vehicle.title()} vehicle "
            "brake released."
        ),

        event_type="safety",

        severity="info"
    )


# =========================================================
# EMERGENCY STOP EVENT
# =========================================================

def log_emergency_stop(
    vehicle=None,
    reason="Emergency stop activated"
):

    if vehicle:

        message = (
   f"[SAFETY] {vehicle_name(vehicle)} Emergency Halt Activated"
)

    else:
       message = (
    "[SAFETY] Fleet emergency stop activated."
)

    return add_event(

        message=message,

        vehicle=vehicle,

        event_type="emergency",

        severity="critical",

        details={

            "reason": reason
        }
    )


# =========================================================
# EMERGENCY RELEASE EVENT
# =========================================================

def log_emergency_released(
    vehicle=None
):

    if vehicle:

        message = (
            f"{vehicle.title()} vehicle "
            "emergency stop released."
        )

    else:

        message = (
            "Emergency stop released."
        )


    return add_event(

        message=message,

        vehicle=vehicle,

        event_type="emergency",

        severity="info"
    )


# =========================================================
# RESET EVENT
# =========================================================

def log_reset(
    vehicle=None
):

    if vehicle:

        message = (
            f"{vehicle.title()} vehicle "
            "reset to initial state."
        )

    else:

        message = (
            "Fleet cycle reset."
        )


    return add_event(

        message=message,

        vehicle=vehicle,

        event_type="system",

        severity="info"
    )


# =========================================================
# FLEET REGISTRY EVENTS (VEHICLE MANAGER)
# =========================================================

def log_vehicle_added(vehicle):

    return add_event(

        message=f"{vehicle} added to the fleet.",

        vehicle=vehicle,

        event_type="fleet",

        severity="success"
    )


def log_vehicle_provisioned(vehicle):

    return add_event(

        message=(
            f"{vehicle} added to the fleet "
            "(ThingsBoard device created automatically)."
        ),

        vehicle=vehicle,

        event_type="fleet",

        severity="success"
    )


def log_vehicle_updated(
    vehicle,
    old_name=None
):

    if old_name and old_name != vehicle:

        message = f"{old_name} renamed to {vehicle}."

    else:

        message = f"{vehicle} configuration updated."


    return add_event(

        message=message,

        vehicle=vehicle,

        event_type="fleet",

        severity="info"
    )


def log_vehicle_deleted(
    vehicle,
    reason=None
):

    message = f"{vehicle} removed from the fleet"

    message += f" ({reason})." if reason else "."


    # The vehicle is no longer registered, so the event is
    # stored under its plain name.
    return add_event(

        message=message,

        vehicle=vehicle,

        event_type="fleet",

        severity="warning"
    )


# =========================================================
# V2V COMMUNICATION EVENT
# =========================================================

def log_v2v_event(
    message,
    details=None
):

    return add_event(

        message=message,

        vehicle=None,

        event_type="communication",

        severity="info",

        details=details
    )


# =========================================================
#  VEHICLE HANDOVER EVENT
# =========================================================
def log_vehicle_handover(
    from_vehicle,
    to_vehicle
):

    return log_v2v_event(

   f"[V2V] {vehicle_name(from_vehicle)} granted route access to {vehicle_name(to_vehicle)}",

        details={

            "from_vehicle": from_vehicle,

            "to_vehicle": to_vehicle,

            "v2v_message": j2735.handover_message(
                from_vehicle,
                to_vehicle
            )
        }
    )


# =========================================================
# J2735-STYLED BASIC SAFETY MESSAGE
# =========================================================

def log_bsm(
    vehicle,
    bsm
):

    return log_vehicle_event(

        vehicle,

        j2735.summary(bsm),

        event_type="j2735",

        details={

            "j2735": bsm
        }
    )


# =========================================================
# SAFETY BLOCK EVENT
# =========================================================

def log_safety_block(
    vehicle,
    reason,
    code=None
):

    return log_vehicle_event(

        vehicle,

        (
            f"{vehicle.title()} vehicle command "
            f"blocked: {reason}"
        ),

        event_type="safety",

        severity="warning",

        details={

            "code": code
        }
    )


# =========================================================
# COMMAND EVENT
# =========================================================

def log_command(
    vehicle,
    command,
    accepted=True,
    reason=None
):

    if accepted:

        message = (
    f"[CMD] {command} accepted for "
    f"{vehicle_name(vehicle)}"
)

        severity = "info"

    else:

        message = (
    f"[CMD] {command} rejected for "
    f"{vehicle_name(vehicle)}"
)

        severity = "warning"


    return log_vehicle_event(

        vehicle,

        message,

        event_type="command",

        severity=severity,

        details={

            "command": command,

            "accepted": accepted,

            "reason": reason
        }
    )


# =========================================================
# CONNECTION EVENT
# =========================================================

def log_connection_event(
    service,
    connected,
    details=None
):

    if connected:

        message = (
            f"{service} connection established."
        )

        severity = "success"

    else:

        message = (
            f"{service} connection lost."
        )

        severity = "critical"


    return log_system_event(

        message,

        severity=severity,

        details=details
    )


# =========================================================
# STARTUP EVENT
# =========================================================

def log_backend_started():

    return log_system_event(

        "Fleet Control backend started.",

        severity="success"
    )


# =========================================================
# ERROR EVENT
# =========================================================

def log_error(
    message,
    vehicle=None,
    details=None
):

    return add_event(

        message=message,

        vehicle=vehicle,

        event_type="error",

        severity="critical",

        details=details
    )
