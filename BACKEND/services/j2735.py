# =========================================================
# J2735-STYLED MESSAGES
# =========================================================
#
# Builds SAE J2735-styled JSON for the Activity Log and the
# /api/j2735/bsm endpoint.
#
# What this is NOT: a conformant J2735 message. Nothing is
# ASN.1 UPER-encoded, signed or sent over DSRC / C-V2X. It is
# a readable JSON rendering that uses J2735 field names,
# units and enumerations.
#
# The vehicles have no GPS, heading, acceleration or steering
# sensors, so those fields carry J2735's own "unavailable"
# values (e.g. lat 900000001, long 1800000001, heading 28800).
#
# Fields that are not part of J2735 are prefixed "x_".
#
# BasicSafetyMessage (DSRC messageId 20) mapping:
#   msgCnt        per-vehicle counter 0-127, wraps
#   id            stable 4-octet TemporaryID from the vehicle
#                 name (real ids rotate for privacy)
#   secMark       UTC milliseconds within the minute
#   speed         km/h -> units of 0.02 m/s (8191 = unavailable)
#   transmission  motor_direction -> park / forwardGears /
#                 reverseGears / unavailable
#   brakes        braking_status applied -> all wheel brakes on
#   partII events emergency_stop -> eventHardBraking +
#                 eventHazardLights
# =========================================================

import hashlib
from datetime import datetime, timezone
from threading import Lock

from . import fleet


# =========================================================
# J2735 CONSTANTS
# =========================================================

BSM_MESSAGE_ID = 20

MSG_COUNT_MODULO = 128

FORMAT_NOTE = (
    "J2735-styled JSON: not UPER-encoded and not sent over V2X. "
    "Unknown values use the standard's 'unavailable' codes."
)

# "Unavailable" values defined by J2735 for each data element.
LAT_UNAVAILABLE = 900000001
LONG_UNAVAILABLE = 1800000001
ELEV_UNAVAILABLE = -4096
SPEED_UNAVAILABLE = 8191
SPEED_MAX = 8190
HEADING_UNAVAILABLE = 28800
ANGLE_UNAVAILABLE = 127
ACCEL_UNAVAILABLE = 2001
VERT_ACCEL_UNAVAILABLE = -127
SEMI_AXIS_UNAVAILABLE = 255
ORIENTATION_UNAVAILABLE = 65535

# Speed unit: 0.02 m/s.
SPEED_UNIT_M_S = 0.02

ALL_WHEELS = [
    "leftFront",
    "leftRear",
    "rightFront",
    "rightRear"
]

TRANSMISSION_BY_DIRECTION = {
    "stopped": "park",
    "forward": "forwardGears",
    "straight": "forwardGears",
    "backward": "reverseGears",
    "reverse": "reverseGears",
}


# =========================================================
# PER-VEHICLE STATE (msgCnt, last broadcast)
# =========================================================

_lock = Lock()

_msg_counts = {}

_last_signatures = {}


# =========================================================
# FIELD CONVERSIONS
# =========================================================

def temporary_id(vehicle_id):
    """4-octet TemporaryID as 8 hex digits, stable per vehicle name."""

    digest = hashlib.sha1(
        str(vehicle_id).encode("utf-8")
    ).hexdigest()

    return digest[:8].upper()


def sec_mark(now=None):

    now = now or datetime.now(timezone.utc)

    return now.second * 1000 + now.microsecond // 1000


def speed_units(speed_kmh):
    """km/h -> J2735 Speed (units of 0.02 m/s)."""

    try:
        speed_kmh = float(speed_kmh)
    except (TypeError, ValueError):
        return SPEED_UNAVAILABLE

    if speed_kmh < 0:
        return SPEED_UNAVAILABLE

    return min(
        SPEED_MAX,
        round(speed_kmh / 3.6 / SPEED_UNIT_M_S)
    )


def transmission(motor_direction):

    return TRANSMISSION_BY_DIRECTION.get(
        str(motor_direction or "").strip().lower(),
        "unavailable"
    )


def wheel_brakes(braking_status):

    value = str(braking_status or "").strip().lower()

    if value == "applied":
        return list(ALL_WHEELS)

    if value in ("not_applied", "released"):
        return []

    return ["unavailable"]


def event_flags(vehicle_state):

    if vehicle_state.get("emergency_stop") is True:

        return [
            "eventHazardLights",
            "eventHardBraking"
        ]

    return []


# =========================================================
# BASIC SAFETY MESSAGE
# =========================================================

def build_bsm(vehicle_id, vehicle_state, msg_cnt=None, now=None):
    """J2735-styled BasicSafetyMessage for one vehicle (pure)."""

    if msg_cnt is None:

        with _lock:
            msg_cnt = _msg_counts.get(vehicle_id, 0)

    flags = event_flags(vehicle_state)

    value = {

        "coreData": {

            "msgCnt": msg_cnt % MSG_COUNT_MODULO,

            "id": temporary_id(vehicle_id),

            "secMark": sec_mark(now),

            "lat": LAT_UNAVAILABLE,

            "long": LONG_UNAVAILABLE,

            "elev": ELEV_UNAVAILABLE,

            "accuracy": {
                "semiMajor": SEMI_AXIS_UNAVAILABLE,
                "semiMinor": SEMI_AXIS_UNAVAILABLE,
                "orientation": ORIENTATION_UNAVAILABLE
            },

            "transmission": transmission(
                vehicle_state.get("motor_direction")
            ),

            "speed": speed_units(
                vehicle_state.get("speed")
            ),

            "heading": HEADING_UNAVAILABLE,

            "angle": ANGLE_UNAVAILABLE,

            "accelSet": {
                "long": ACCEL_UNAVAILABLE,
                "lat": ACCEL_UNAVAILABLE,
                "vert": VERT_ACCEL_UNAVAILABLE,
                "yaw": 0
            },

            "brakes": {
                "wheelBrakes": wheel_brakes(
                    vehicle_state.get("braking_status")
                ),
                "traction": "unavailable",
                "abs": "unavailable",
                "scs": "unavailable",
                "brakeBoost": "unavailable",
                "auxBrakes": "unavailable"
            },

            # Vehicle dimensions are not configured.
            "size": {
                "width": 0,
                "length": 0
            }
        }
    }

    if flags:

        value["partII"] = [
            {
                "partII-Id": 0,
                "partII-Value": {
                    "events": flags
                }
            }
        ]

    return {

        "messageId": BSM_MESSAGE_ID,

        "messageName": "BasicSafetyMessage",

        "value": value,

        "x_vehicle": vehicle_id,

        "x_extensions": {
            "missionStatus": vehicle_state.get("mission_status"),
            "currentStop": vehicle_state.get(
                "current_stop",
                vehicle_state.get("location")
            ),
            "loadPercentage": vehicle_state.get("load_percentage"),
            "radar": vehicle_state.get("radar_status"),
            "trigger": vehicle_state.get("last_command")
        },

        "x_format": FORMAT_NOTE
    }


def _signature(bsm):
    """Fields whose change makes a new BSM worth logging.

    Load percentage is left out: it changes on every loading
    step and would flood the log.
    """

    core = bsm["value"]["coreData"]

    extensions = bsm["x_extensions"]

    return (
        core["speed"],
        core["transmission"],
        tuple(core["brakes"]["wheelBrakes"]),
        tuple(
            bsm["value"].get("partII", [{}])[0]
            .get("partII-Value", {})
            .get("events", [])
        ),
        extensions["currentStop"],
        extensions["missionStatus"],
        extensions["radar"]
    )


def broadcast_if_changed(vehicle_id, vehicle_state, now=None):
    """Return a new BSM if the vehicle's safety state changed, else None.

    Each returned BSM advances that vehicle's msgCnt.
    """

    preview = build_bsm(
        vehicle_id,
        vehicle_state,
        msg_cnt=0,
        now=now
    )

    signature = _signature(preview)

    with _lock:

        if _last_signatures.get(vehicle_id) == signature:
            return None

        _last_signatures[vehicle_id] = signature

        msg_cnt = _msg_counts.get(vehicle_id, 0)

        _msg_counts[vehicle_id] = (msg_cnt + 1) % MSG_COUNT_MODULO

    preview["value"]["coreData"]["msgCnt"] = msg_cnt

    return preview


def forget(*vehicle_ids):
    """Drop counters for vehicles deleted or renamed in the registry."""

    with _lock:

        for vehicle_id in vehicle_ids:

            _msg_counts.pop(vehicle_id, None)

            _last_signatures.pop(vehicle_id, None)


# =========================================================
# ONE-LINE SUMMARY
# =========================================================

def _short(vehicle_id):

    try:
        return fleet.short_name(vehicle_id)
    except ValueError:
        return str(vehicle_id)


def summary(bsm):

    core = bsm["value"]["coreData"]

    extensions = bsm["x_extensions"]

    speed = core["speed"]

    speed_text = (
        "n/a"
        if speed == SPEED_UNAVAILABLE
        else f"{speed * SPEED_UNIT_M_S:.2f} m/s"
    )

    wheels = core["brakes"]["wheelBrakes"]

    brakes = (
        "ON" if wheels == ALL_WHEELS
        else "OFF" if not wheels
        else "n/a"
    )

    # eventHardBraking -> hardBraking
    flags = [
        flag[5].lower() + flag[6:]
        for flag in (
            bsm["value"].get("partII", [{}])[0]
            .get("partII-Value", {})
            .get("events", [])
        )
    ]

    parts = [
        f"[BSM] {_short(bsm['x_vehicle'])}",
        f"id={core['id']}",
        f"msgCnt={core['msgCnt']}",
        f"speed={speed_text}",
        f"trans={core['transmission']}",
        f"brakes={brakes}"
    ]

    if flags:
        parts.append(f"events=[{','.join(flags)}]")

    if extensions.get("currentStop"):
        parts.append(f"stop={extensions['currentStop']}")

    if extensions.get("trigger") and extensions["trigger"] != "NONE":
        parts.append(f"trigger={extensions['trigger']}")

    return " ".join(parts)


# =========================================================
# V2V HANDOVER (CUSTOM, NOT J2735)
# =========================================================

def handover_message(from_vehicle, to_vehicle, now=None):
    """Custom V2V handover message in the same JSON style.

    J2735 defines no message for granting route access to
    another vehicle, so this is labelled as custom.
    """

    return {

        "messageName": "V2V-HANDOVER",

        "value": {
            "secMark": sec_mark(now),
            "from": {
                "id": temporary_id(from_vehicle),
                "x_vehicle": from_vehicle
            },
            "to": {
                "id": temporary_id(to_vehicle),
                "x_vehicle": to_vehicle
            },
            "grant": "routeAccess"
        },

        "x_format": (
            "Custom V2V message, not part of SAE J2735 "
            "(J2735 has no route-handover message)."
        )
    }
