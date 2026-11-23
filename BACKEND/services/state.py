# =========================================================
# VEHICLE STATE SERVICE
# =========================================================
#
# Central internal vehicle-state management layer.
#
# Responsibilities:
#   1. Store current state of every registered vehicle (see fleet.py)
#   2. Validate vehicle operations
#   3. Update vehicle state
#   4. Handle loading percentage
#   5. Handle movement
#   6. Handle emergency stop
#   7. Handle QR / Radar status
#   8. Apply ThingsBoard telemetry to internal state
#
# =========================================================

import itertools
import time
from copy import deepcopy
from threading import RLock

from . import fleet


# =========================================================
# CONSTANTS
# =========================================================

STOP_1 = "stop_1"
STOP_2 = "stop_2"
STOP_3 = "stop_3"

STATUS_WAITING = "waiting"
STATUS_READY = "ready"
STATUS_RUNNING = "running"
STATUS_LOADING = "loading"
STATUS_COMPLETED = "completed"
STATUS_STOPPED = "stopped"
STATUS_EMERGENCY = "emergency_stop"
STATUS_CHARGING = "charging"

# The charging bay (world.json node CB, "stop": "charging").
STOP_CHARGING = "charging"

BRAKE_APPLIED = "applied"
BRAKE_NOT_APPLIED = "not_applied"

QR_DETECTED = "detected"
QR_NOT_DETECTED = "not_detected"

RADAR_CLEAR = "clear"
RADAR_OBSTACLE = "obstacle"

MOTOR_STRAIGHT = "straight"
MOTOR_STOPPED = "stopped"


# =========================================================
# INTERNAL STATE
# =========================================================

_lock = RLock()


_default_vehicle = {

    "name": "",

    "color": "",

    "status": STATUS_WAITING,

    "location": STOP_1,

    "current_stop": STOP_1,

    "latitude": 0.0,

    "longitude": 0.0,

    "v2v_status": "disconnected",

    "handover_status": "idle",

    "load_percentage": 0,

    "load": 0,

    "loading_status": "idle",

    "loading_progress": 0,

    "capacity": 1000,

    "braking_status": BRAKE_APPLIED,

    "motor_direction": MOTOR_STOPPED,

    "qr_status": QR_NOT_DETECTED,

    "Apriltag_status": QR_NOT_DETECTED,

    "radar_status": RADAR_CLEAR,

    "obstacle_detected": False,

    "speed": 0,

    "emergency_stop": False,

    "battery_percentage": 100,

    "temperature": 30,

    "motor_rpm": 0,

    "network_signal": 100,

    "mode": "AUTONOMOUS",

    "mission_status": "IDLE",

    "last_command": "NONE",

    "health_score": 100,

    # Fleet dashboard fields. None means "not reported yet".
    "next_action": None,

    "last_seen": None,

    "communication_status": None,

    "vehicle_online": None,

    "battery_level": None,

    "signal_strength": None,

    "destination": None,

    "distance_to_destination": None,

    # Filled by the world engine (services/world.py).
    "eta_seconds": None,

    # V2V fail-safe (services/v2v.py): True while every vehicle
    # in radio range has gone silent. The vehicle holds and
    # movement commands are refused (V2V_COMM_LOST).
    "comm_hold": False,

    # Autopilot (services/autopilot.py): MANUAL (the dashboard or
    # an operator drives) or AUTO (the backend drives the mission).
    # A mirror of the autopilot's own setting, for display.
    "autopilot_mode": "MANUAL",

    # A simulated breakdown (scenario fault injection), e.g.
    # "BREAKDOWN". The vehicle holds and cannot be started or take
    # over a cycle until it is repaired. None = healthy.
    "fault": None

}


# Telemetry fields copied as-is from ThingsBoard into state.
PASSTHROUGH_FIELDS = (
    "mission_status",
    "next_action",
    "last_seen",
    "communication_status",
    "vehicle_online",
    "battery_level",
    "signal_strength",
    "destination",
    "distance_to_destination",
)


# While a vehicle is emergency-stopped, ThingsBoard telemetry
# may not change these fields. Only release_emergency_stop()
# or reset_vehicle() clears an emergency stop: a simulator or
# device that keeps reporting "emergency_stop: false" (or an
# old value read before the stop reached ThingsBoard) must not
# silently restart the vehicle.
EMERGENCY_LATCHED_FIELDS = (
    "emergency_stop",
    "status",
    "speed",
    "motor_direction",
    "braking_status",
    "mission_status",
)


# get_latest_state() fills some keys from another ThingsBoard
# key; such a value counts as reported if any of these was.
_REPORTED_ALIASES = {
    "Apriltag_status": ("Apriltag_status", "apriltag_status", "qr_status"),
    "location": ("location", "current_stop"),
    "current_stop": ("current_stop", "location"),
    "load_percentage": ("load_percentage", "loading_progress", "load"),
    "loading_progress": ("loading_progress", "load_percentage"),
}


# vehicle_id -> epoch ms. Telemetry that ThingsBoard recorded
# before this time is ignored (set by reset / emergency stop /
# release, so a poll that read old values cannot undo them).
_ignore_before = {}


# =========================================================
# VEHICLES
# =========================================================

# Records are created on first use, so a vehicle added at
# runtime (POST /api/vehicles) gets default state without a
# restart.
vehicles = {}


# =========================================================
# PLACEMENTS (FOR THE WORLD ENGINE)
# =========================================================
#
# A transition that PUTS a vehicle at a stop (reset, handover,
# reaching the loading point or the warehouse, a location
# reported by ThingsBoard in mirror mode) bumps that vehicle's
# placement number. The world engine snaps the vehicle's pose
# to its stop when the number changes. Plain state values
# cannot tell this apart from a vehicle that was stopped on
# the road (emergency stop, then release): both say "at rest,
# location stop_1", but only the first was moved there.
# =========================================================

_placement_counter = itertools.count(1)

_placements = {}


def _bump_placement(vehicle_id):

    _placements[vehicle_id] = next(_placement_counter)


def placement_seq(vehicle_id):
    """Increases whenever a command placed the vehicle at a stop."""

    with _lock:
        return _placements.get(vehicle_id, 0)


def _new_record(vehicle_id):

    registered = fleet.get(
        vehicle_id
    )

    return {

        **deepcopy(_default_vehicle),

        "vehicle_id": vehicle_id,

        "name": registered.name,

        "color": registered.color or "",

        # A new vehicle stands at Stop 1, facing its AprilTag.
        **_tag_reading(True)
    }


# =========================================================
# SIMULATED APRILTAG / QR READING
# =========================================================
#
# Every stop has an AprilTag marker. The simulated camera sees
# it while the vehicle stands at a stop and loses it while the
# vehicle is moving between stops. An explicit reading sent to
# /api/sensors/qr (or reported by the vehicle through
# ThingsBoard) overrides this until the vehicle moves again.
# =========================================================

def _tag_reading(detected):

    status = QR_DETECTED if detected else QR_NOT_DETECTED

    return {
        "qr_status": status,
        "Apriltag_status": status
    }


# =========================================================
# TRANSITION GUARDS
# =========================================================

class TransitionBlocked(ValueError):
    """A state transition was refused for a safety reason.

    Raised inside the state lock, so it also catches a change
    (for example an emergency stop) that happened after the
    route validated the command. Routes answer 403 with `code`.
    """

    def __init__(self, message, code):

        super().__init__(message)

        self.code = code


def _require_not_emergency(record):

    if record["emergency_stop"]:

        raise TransitionBlocked(
            "Vehicle is under emergency stop.",
            "EMERGENCY_STOP_ACTIVE"
        )


def _require_path_clear(record):

    if record["radar_status"] == RADAR_OBSTACLE:

        raise TransitionBlocked(
            "Movement blocked: radar detected an obstacle.",
            "RADAR_OBSTACLE"
        )

    if record["obstacle_detected"]:

        raise TransitionBlocked(
            "Movement blocked: obstacle detected.",
            "OBSTACLE_DETECTED"
        )

    if record.get("comm_hold"):

        raise TransitionBlocked(
            "Movement blocked: V2V contact with every vehicle in range is lost.",
            "V2V_COMM_LOST"
        )


def battery_of(record):
    """Battery percentage of a state record (battery_level, else battery_percentage)."""

    value = record.get("battery_level")

    if value is None:
        value = record.get("battery_percentage")

    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _require_can_drive(record):
    """A broken-down vehicle or an empty battery cannot move (Phase 4)."""

    if record.get("fault"):

        raise TransitionBlocked(
            f"Vehicle has a fault ({record['fault']}) and must be repaired first.",
            "VEHICLE_FAULT"
        )

    battery = battery_of(record)

    if battery is not None and battery <= 0:

        raise TransitionBlocked(
            "Battery is empty.",
            "BATTERY_EMPTY"
        )


def departed_stop_2(record):
    """True once a loaded vehicle drives off the loading point (autopilot).

    Its location still reads stop_2 until it reaches Stop 3, but the
    loading point is free for the next vehicle.
    """

    return (
        record.get("location") == STOP_2
        and record.get("status") == STATUS_RUNNING
        and record.get("mission_status") == "MOVING_TO_WAREHOUSE"
    )


def stop_2_occupant(except_vehicle=None):
    """The vehicle currently at the loading point (Stop 2), if any."""

    with _lock:
        for vehicle_id, record in vehicles.items():
            if (
                vehicle_id != except_vehicle
                and record.get("location") == STOP_2
                and not departed_stop_2(record)
            ):
                return vehicle_id
    return None


# =========================================================
# MISSION ACTIVITY
# =========================================================
#
# A vehicle is "in a mission" while it moves, loads, or waits
# at the loading point to be loaded. Registry changes that
# would alter the handover order are refused while any vehicle
# is in a mission (see api/routes_vehicles.py).
# =========================================================

ACTIVE_MISSIONS = (
    "MOVING_TO_LOADING",
    "AT_LOADING_POINT",
    "LOADING",
    "MANUAL_CONTROL",
    # Driven by the autopilot (Phase 4).
    "UNLOADING",
    "RETURNING_TO_STOP_1",
    "MOVING_TO_CHARGING",
    "CHARGING",
)


def in_mission(record):

    try:
        speed = float(record.get("speed") or 0)
    except (TypeError, ValueError):
        speed = 0

    return (
        speed > 0
        or record.get("status") in (STATUS_RUNNING, STATUS_LOADING)
        or record.get("mission_status") in ACTIVE_MISSIONS
    )


def vehicles_in_mission():

    return [
        vehicle_id
        for vehicle_id, record in get_all_vehicle_states().items()
        if in_mission(record)
    ]


def hold_vehicle(vehicle, reason):
    """Stop a vehicle where it is (speed 0, brakes applied) and end its mission."""

    return update_vehicle_state(
        vehicle,
        status=STATUS_STOPPED,
        braking_status=BRAKE_APPLIED,
        motor_direction=MOTOR_STOPPED,
        speed=0,
        motor_rpm=0,
        loading_status="idle",
        mission_status="HELD",
        next_action=reason,
        last_command="HOLD"
    )


# =========================================================
# VEHICLE KEY
# =========================================================

def _key(vehicle):

    vehicle_id = fleet.resolve(
        vehicle
    )


    if vehicle_id is None:

        raise ValueError(
            f"Unknown vehicle: {vehicle}. "
            f"Registered vehicles: {fleet.describe()}"
        )


    with _lock:

        if vehicle_id not in vehicles:

            vehicles[vehicle_id] = _new_record(
                vehicle_id
            )

        else:

            # Follow colour edits made in the Vehicle Manager.
            vehicles[vehicle_id]["color"] = (
                fleet.get(vehicle_id).color or ""
            )


    return vehicle_id


# =========================================================
# REGISTRY CHANGES
# =========================================================

def rename_vehicle(
    old_id,
    new_id
):
    """Keep the live state of a vehicle renamed in the registry."""

    with _lock:

        record = vehicles.pop(
            old_id,
            None
        )

        if record is not None:

            record.update(
                vehicle_id=new_id,
                name=new_id
            )

            vehicles[new_id] = record

        if old_id in _ignore_before:

            _ignore_before[new_id] = _ignore_before.pop(old_id)

        if old_id in _placements:

            _placements[new_id] = _placements.pop(old_id)


def forget_vehicle(vehicle_id):
    """Drop the state of a vehicle deleted from the registry."""

    with _lock:

        vehicles.pop(
            vehicle_id,
            None
        )

        _ignore_before.pop(
            vehicle_id,
            None
        )

        _placements.pop(
            vehicle_id,
            None
        )


def ignore_telemetry_before(vehicle_ids, timestamp_ms=None):
    """Ignore ThingsBoard telemetry recorded before `timestamp_ms` (default: now)."""

    if timestamp_ms is None:
        timestamp_ms = int(time.time() * 1000)

    with _lock:

        for vehicle_id in vehicle_ids:
            _ignore_before[vehicle_id] = timestamp_ms


# =========================================================
# GET VEHICLE
# =========================================================

def get_vehicle_state(vehicle):

    vehicle = _key(
        vehicle
    )


    with _lock:

        return deepcopy(
            vehicles[vehicle]
        )


# =========================================================
# GET ALL VEHICLES
# =========================================================

def get_all_vehicle_states():

    return {

        vehicle_id:
            get_vehicle_state(
                vehicle_id
            )

        for vehicle_id in fleet.vehicle_ids()
    }


# =========================================================
# UPDATE STATE
# =========================================================

def update_vehicle_state(
    vehicle,
    **changes
):

    vehicle = _key(
        vehicle
    )


    with _lock:

        vehicles[vehicle].update(
            changes
        )

        return deepcopy(
            vehicles[vehicle]
        )


# =========================================================
# APPLY THINGSBOARD TELEMETRY
# =========================================================
#
# This function fixes the missing:
#
# apply_telemetry_state()
#
# It converts ThingsBoard telemetry names
# into our internal vehicle-state names.
#
# =========================================================

def apply_telemetry_state(
    vehicle,
    telemetry=None,
    **kwargs
):

    # -----------------------------------------------------
    # SAFETY
    # -----------------------------------------------------

    if telemetry is None:

        telemetry = {}


    if not isinstance(
        telemetry,
        dict
    ):

        telemetry = {}


    # Allow direct keyword values also

    telemetry.update(
        kwargs
    )


    # -----------------------------------------------------
    # STALE TELEMETRY
    # -----------------------------------------------------
    # "_ts" is the newest ThingsBoard timestamp in this read
    # (see thingsboard.get_latest_state). Anything recorded
    # before the last reset / emergency stop / release is old.

    vehicle_id = _key(
        vehicle
    )

    reported_at = telemetry.get(
        "_ts"
    )

    with _lock:

        threshold = _ignore_before.get(
            vehicle_id
        )

    if (
        threshold is not None
        and reported_at is not None
        and reported_at < threshold
    ):

        return get_vehicle_state(
            vehicle_id
        )


    # -----------------------------------------------------
    # ONLY KEYS THE DEVICE ACTUALLY REPORTED
    # -----------------------------------------------------
    # thingsboard.get_latest_state() fills every missing key
    # with a placeholder (radar "clear", obstacle False,
    # AprilTag "not_detected", speed 0, ...). Applying those
    # would let a poll silently clear a real obstacle reading
    # or override the AprilTag simulation. "_reported" lists
    # the keys ThingsBoard really had values for.

    reported_keys = telemetry.get("_reported")

    if reported_keys is not None:

        reported_keys = set(reported_keys)

        telemetry = {
            key: value
            for key, value in telemetry.items()
            if key.startswith("_")
            or reported_keys.intersection(
                _REPORTED_ALIASES.get(key, (key,))
            )
        }


    changes = {}


    # -----------------------------------------------------
    # VEHICLE STATUS
    # -----------------------------------------------------

    if "vehicle_status" in telemetry:

        changes["status"] = (
            telemetry["vehicle_status"]
        )


    # -----------------------------------------------------
    # LOCATION
    # -----------------------------------------------------

    if "location" in telemetry:

        changes["location"] = (
            telemetry["location"]
        )

    if "current_stop" in telemetry:

        changes["location"] = (
            telemetry["current_stop"]
        )

        changes["current_stop"] = (
            telemetry["current_stop"]
        )


    # -----------------------------------------------------
    # LATITUDE / LONGITUDE
    # -----------------------------------------------------

    if "latitude" in telemetry:

        try:
            changes["latitude"] = float(
                telemetry["latitude"]
            )
        except (TypeError, ValueError):
            pass

    if "longitude" in telemetry:

        try:
            changes["longitude"] = float(
                telemetry["longitude"]
            )
        except (TypeError, ValueError):
            pass


    # -----------------------------------------------------
    # V2V / HANDOVER STATUS
    # -----------------------------------------------------

    if "v2v_status" in telemetry:

        changes["v2v_status"] = str(
            telemetry["v2v_status"]
        ).strip()

    if "handover_status" in telemetry:

        changes["handover_status"] = str(
            telemetry["handover_status"]
        ).strip()


    # -----------------------------------------------------
    # LOAD PERCENTAGE
    # -----------------------------------------------------

    if "load_percentage" in telemetry:

        try:

            percentage = float(
                telemetry["load_percentage"]
            )

            percentage = max(
                0,
                min(
                    100,
                    percentage
                )
            )

            changes["load_percentage"] = percentage

            changes["loading_progress"] = percentage

            changes["load"] = percentage * 10

        except (
            TypeError,
            ValueError
        ):

            pass

    if "load" in telemetry:

        try:

            changes["load"] = max(
                0,
                float(telemetry["load"])
            )

        except (TypeError, ValueError):

            pass

    if "loading_progress" in telemetry:

        try:

            progress = max(
                0,
                min(100, float(telemetry["loading_progress"]))
            )

            changes["loading_progress"] = progress
            changes["load_percentage"] = progress

            if "load" not in telemetry:

                changes["load"] = progress * 10

        except (TypeError, ValueError):

            pass


    # -----------------------------------------------------
    # LOADING STATUS / PROGRESS
    # -----------------------------------------------------

    if "loading_status" in telemetry:

        changes["loading_status"] = str(
            telemetry["loading_status"]
        ).strip()

    # -----------------------------------------------------
    # BRAKING STATUS
    # -----------------------------------------------------

    if "braking_status" in telemetry:

        changes["braking_status"] = (
            telemetry["braking_status"]
        )


    # -----------------------------------------------------
    # MOTOR DIRECTION
    # -----------------------------------------------------

    if "motor_direction" in telemetry:

        changes["motor_direction"] = (
            telemetry["motor_direction"]
        )


    # -----------------------------------------------------
    # APRILTAG / QR STATUS
    # -----------------------------------------------------

    apriltag_status = None

    for key in (
        "Apriltag_status",
        "apriltag_status",
        "qr_status"
    ):

        if key in telemetry:
            apriltag_status = telemetry[key]
            break

    if apriltag_status is not None:

        changes["qr_status"] = apriltag_status
        changes["Apriltag_status"] = apriltag_status


    # -----------------------------------------------------
    # RADAR STATUS
    # -----------------------------------------------------

    if "radar_status" in telemetry:

        changes["radar_status"] = (
            telemetry["radar_status"]
        )


    # -----------------------------------------------------
    # OBSTACLE DETECTED
    # -----------------------------------------------------

    if "obstacle_detected" in telemetry:

        value = telemetry[
            "obstacle_detected"
        ]

        if isinstance(
            value,
            str
        ):

            value = (
                value.lower().strip()
                in (
                    "true",
                    "1",
                    "yes",
                    "detected"
                )
            )

        else:

            value = bool(
                value
            )

        changes[
            "obstacle_detected"
        ] = value


    # -----------------------------------------------------
    # SPEED
    # -----------------------------------------------------

    if "speed" in telemetry:

        try:

            changes["speed"] = float(
                telemetry["speed"]
            )

        except (
            TypeError,
            ValueError
        ):

            pass


    # -----------------------------------------------------
    # EMERGENCY STOP
    # -----------------------------------------------------

    if "emergency_stop" in telemetry:

        value = telemetry[
            "emergency_stop"
        ]

        if isinstance(
            value,
            str
        ):

            value = (
                value.lower().strip()
                in (
                    "true",
                    "1",
                    "yes"
                )
            )

        else:

            value = bool(
                value
            )

        changes[
            "emergency_stop"
        ] = value


    # -----------------------------------------------------
    # FLEET DASHBOARD FIELDS
    # -----------------------------------------------------
    #
    # Copied only when ThingsBoard actually reported them,
    # so get_latest_state() placeholders never overwrite
    # local state.

    reported = telemetry.get(
        "_reported"
    )

    for key in PASSTHROUGH_FIELDS:

        if reported is not None and key not in reported:
            continue

        if telemetry.get(key) in (None, ""):
            continue

        changes[key] = telemetry[key]


    # -----------------------------------------------------
    # APPLY CHANGES
    # -----------------------------------------------------

    with _lock:

        # Emergency stop is latched locally (see
        # EMERGENCY_LATCHED_FIELDS).
        if vehicles[vehicle_id]["emergency_stop"] is True:

            for key in EMERGENCY_LATCHED_FIELDS:
                changes.pop(key, None)

        if changes:

            if "location" in changes:

                changes["current_stop"] = changes["location"]

                # Mirror mode: the device reported another stop.
                if changes["location"] != vehicles[vehicle_id]["location"]:
                    _bump_placement(vehicle_id)

            return update_vehicle_state(
                vehicle_id,
                **changes
            )


    # Nothing to update

    return get_vehicle_state(
        vehicle
    )


# =========================================================
# START VEHICLE
# =========================================================

def start_vehicle(vehicle, speed_kmh=18):

    vehicle = _key(vehicle)

    with _lock:

        current = vehicles[vehicle]

        _require_not_emergency(current)

        _require_path_clear(current)

        _require_can_drive(current)

        if current["status"] == STATUS_RUNNING:

            raise TransitionBlocked(
                "Vehicle is already running.",
                "ALREADY_RUNNING"
            )

        return update_vehicle_state(

            vehicle,

            status=STATUS_RUNNING,

            braking_status=BRAKE_NOT_APPLIED,

            motor_direction=MOTOR_STRAIGHT,

            speed=speed_kmh,

            motor_rpm=int(speed_kmh * 100),

            mission_status="MOVING_TO_LOADING",

            last_command="START",

            # Left Stop 1: the marker is out of view until the
            # next stop.
            **_tag_reading(False)
        )


# =========================================================
# REACH LOADING POINT
# =========================================================

def reach_loading_point(vehicle):

    vehicle = _key(vehicle)

    with _lock:

        current = vehicles[vehicle]

        if current["location"] != STOP_1:

            raise ValueError(
                "Vehicle must start from Stop 1."
            )

        _require_not_emergency(current)

        _require_path_clear(current)

        # Only one vehicle at a time at the loading point.
        occupant = stop_2_occupant(except_vehicle=vehicle)

        if occupant is not None:

            raise TransitionBlocked(
                f"The loading point is occupied by {occupant}.",
                "LOADING_SLOT_BUSY"
            )

        _bump_placement(vehicle)

        return update_vehicle_state(

            vehicle,

            location=STOP_2,

            current_stop=STOP_2,

            status=STATUS_READY,

            loading_status="idle",

            loading_progress=current["load_percentage"],

            braking_status=BRAKE_APPLIED,

            motor_direction=MOTOR_STOPPED,

            speed=0,

            motor_rpm=0,

            mission_status="AT_LOADING_POINT",

            last_command="LOADING_POINT",

            # Arrived at Stop 2: its marker is in view.
            **_tag_reading(True)
        )


# =========================================================
# START LOADING
# =========================================================

def start_loading(vehicle):

    vehicle = _key(vehicle)

    with _lock:

        current = vehicles[vehicle]

        if current["location"] != STOP_2:

            raise ValueError(
                "Vehicle must be at loading point."
            )

        _require_not_emergency(current)

        return _start_loading_update(vehicle, current)


def _start_loading_update(vehicle, current):

    return update_vehicle_state(

        vehicle,

        status=STATUS_LOADING,

        loading_status="loading",

        loading_progress=current["load_percentage"],

        braking_status=BRAKE_APPLIED,

        motor_direction=MOTOR_STOPPED,

        speed=0,
        
        mission_status="LOADING",

        last_command="START_LOADING"
    )


# =========================================================
# UPDATE LOADING %
# =========================================================

def update_loading(
    vehicle,
    percentage
):

    vehicle = _key(vehicle)

    with _lock:

        current = vehicles[vehicle]

        if current["location"] != STOP_2:

            raise ValueError(
                "Vehicle must be at loading point."
            )

        _require_not_emergency(current)

        return _update_loading_values(vehicle, percentage)


def _update_loading_values(vehicle, percentage):

    percentage = float(
        percentage
    )


    percentage = max(
        0,
        min(
            100,
            percentage
        )
    )


    if percentage >= 100:

        return update_vehicle_state(

            vehicle,

            load_percentage=100,

            load=1000,

            loading_status="complete",

            loading_progress=100,

            status=STATUS_READY,

            braking_status=BRAKE_APPLIED,

            motor_direction=MOTOR_STOPPED,

            speed=0
        )


    return update_vehicle_state(

        vehicle,

        load_percentage=percentage,

        load=percentage * 10,

        loading_status="loading",

        loading_progress=percentage,

        status=STATUS_LOADING,

        braking_status=BRAKE_APPLIED,

        motor_direction=MOTOR_STOPPED,

        speed=0
    )


# =========================================================
# REACH WAREHOUSE
# =========================================================

def reach_warehouse(vehicle):

    vehicle = _key(vehicle)

    with _lock:

        current = vehicles[vehicle]

        if current["location"] != STOP_2:

            raise ValueError(
                "Vehicle must be at loading point."
            )

        if current["load_percentage"] < 100:

            raise ValueError(
                "Vehicle cannot go to warehouse "
                "before loading reaches 100%."
            )

        _require_not_emergency(current)

        # Radar AND the obstacle flag (the flag used to be
        # ignored here).
        _require_path_clear(current)

        _bump_placement(vehicle)

        return _reach_warehouse_update(vehicle, current)


def _reach_warehouse_update(vehicle, current):

    return update_vehicle_state(

        vehicle,

        location=STOP_3,

        current_stop=STOP_3,

        status=STATUS_COMPLETED,

        loading_status="complete",

        loading_progress=current["load_percentage"],

        braking_status=BRAKE_APPLIED,

        motor_direction=MOTOR_STOPPED,

        speed=0,
        
        mission_status="MOVING_TO_WAREHOUSE",

        last_command="WAREHOUSE",

        motor_rpm=0,

        # Arrived at Stop 3: its marker is in view.
        **_tag_reading(True)
    )


# =========================================================
# COMPLETE JOURNEY
# =========================================================

def complete_journey(vehicle):

    vehicle = _key(vehicle)

    with _lock:

        current = vehicles[vehicle]

        _require_not_emergency(current)

        if current["location"] != STOP_3:

            raise ValueError(
                "Vehicle must be at warehouse."
            )

        return update_vehicle_state(

            vehicle,

            status=STATUS_COMPLETED,

            braking_status=BRAKE_APPLIED,

            motor_direction=MOTOR_STOPPED,

            speed=0,

            mission_status="COMPLETED",

            last_command="COMPLETE"
        )


# =========================================================
# AUTOPILOT LEGS (PHASE 4)
# =========================================================
#
# The autopilot (services/autopilot.py) drives the whole loop:
#   Stop 1 -start_vehicle-> Stop 2 -reach_loading_point, loading->
#   -depart_loading_point-> Stop 3 -arrive_at_warehouse, unload_to,
#   complete_journey-> -start_return-> Stop 1 (arrive_at_origin) or
#   the charging bay (arrive_at_charging, charge_to, start_return).
# The world engine moves the vehicle between stops; these
# transitions run when it leaves or reaches one. Like the REST
# commands, every one re-checks the emergency latch and the path
# inside the state lock.
# =========================================================

def _drive_changes(speed_kmh, mission, command):

    return {
        "status": STATUS_RUNNING,
        "braking_status": BRAKE_NOT_APPLIED,
        "motor_direction": MOTOR_STRAIGHT,
        "speed": speed_kmh,
        "motor_rpm": int(speed_kmh * 100),
        "mission_status": mission,
        "last_command": command,
        # Leaving a stop: its marker leaves the camera.
        **_tag_reading(False),
    }


def _arrival_changes(stop, status, mission, command):

    return {
        "location": stop,
        "current_stop": stop,
        "status": status,
        "braking_status": BRAKE_APPLIED,
        "motor_direction": MOTOR_STOPPED,
        "speed": 0,
        "motor_rpm": 0,
        "mission_status": mission,
        "last_command": command,
        # Arrived at a stop: its marker is in view.
        **_tag_reading(True),
    }


def _require_movable(current):

    _require_not_emergency(current)
    _require_path_clear(current)
    _require_can_drive(current)

    if current["status"] == STATUS_RUNNING:
        raise TransitionBlocked("Vehicle is already running.", "ALREADY_RUNNING")


def depart_loading_point(vehicle, speed_kmh=18):
    """A fully loaded vehicle drives off Stop 2 toward the warehouse."""

    vehicle = _key(vehicle)

    with _lock:

        current = vehicles[vehicle]

        if current["location"] != STOP_2:
            raise ValueError("Vehicle must be at the loading point.")

        if current["load_percentage"] < 100:
            raise TransitionBlocked(
                "Vehicle cannot leave for the warehouse before reaching 100% load.",
                "LOAD_NOT_COMPLETE"
            )

        _require_movable(current)

        return update_vehicle_state(
            vehicle,
            loading_status="complete",
            **_drive_changes(speed_kmh, "MOVING_TO_WAREHOUSE", "AUTO_DEPART_STOP_2")
        )


def arrive_at_warehouse(vehicle):
    """The vehicle reached Stop 3 and starts unloading."""

    vehicle = _key(vehicle)

    with _lock:

        current = vehicles[vehicle]

        _require_not_emergency(current)

        _bump_placement(vehicle)

        return update_vehicle_state(
            vehicle,
            loading_status="unloading",
            **_arrival_changes(STOP_3, STATUS_COMPLETED, "UNLOADING", "AUTO_ARRIVE_STOP_3")
        )


def unload_to(vehicle, percentage):
    """Unloading at Stop 3: the load goes down to `percentage`."""

    vehicle = _key(vehicle)

    with _lock:

        current = vehicles[vehicle]

        if current["location"] != STOP_3:
            raise ValueError("Vehicle must be at the warehouse.")

        _require_not_emergency(current)

        percentage = max(0.0, min(float(current["load_percentage"]), float(percentage)))

        changes = {
            "load_percentage": percentage,
            "load": percentage * 10,
            "loading_progress": percentage,
            "loading_status": "unloading" if percentage > 0 else "idle",
        }

        if percentage <= 0:
            changes["mission_status"] = "UNLOADED"

        return update_vehicle_state(vehicle, **changes)


def start_return(vehicle, to_charging=False, speed_kmh=18):
    """Drive the return leg: from Stop 3 or the charging bay to Stop 1, or to charge."""

    vehicle = _key(vehicle)

    with _lock:

        current = vehicles[vehicle]

        if current["location"] not in (STOP_3, STOP_CHARGING):
            raise ValueError("The return leg starts at the warehouse or the charging bay.")

        _require_movable(current)

        mission = "MOVING_TO_CHARGING" if to_charging else "RETURNING_TO_STOP_1"

        return update_vehicle_state(
            vehicle,
            handover_status="idle",
            **_drive_changes(speed_kmh, mission, f"AUTO_{mission}")
        )


def depart_to_charging(vehicle, speed_kmh=18):
    """From Stop 1 with a low battery: drive to the charging bay instead of a new cycle."""

    vehicle = _key(vehicle)

    with _lock:

        current = vehicles[vehicle]

        if current["location"] != STOP_1:
            raise ValueError("Vehicle must be at Stop 1.")

        _require_movable(current)

        return update_vehicle_state(
            vehicle,
            **_drive_changes(speed_kmh, "MOVING_TO_CHARGING", "AUTO_MOVING_TO_CHARGING")
        )


def arrive_at_origin(vehicle):
    """Back at Stop 1 after the return leg: empty, waiting for the next cycle."""

    vehicle = _key(vehicle)

    with _lock:

        current = vehicles[vehicle]

        _require_not_emergency(current)

        _bump_placement(vehicle)

        return update_vehicle_state(
            vehicle,
            load_percentage=0,
            load=0,
            loading_status="idle",
            loading_progress=0,
            handover_status="idle",
            **_arrival_changes(STOP_1, STATUS_WAITING, "IDLE", "AUTO_ARRIVE_STOP_1")
        )


def arrive_at_charging(vehicle):
    """At the charging bay: charge until the target level."""

    vehicle = _key(vehicle)

    with _lock:

        current = vehicles[vehicle]

        _require_not_emergency(current)

        _bump_placement(vehicle)

        return update_vehicle_state(
            vehicle,
            **_arrival_changes(STOP_CHARGING, STATUS_CHARGING, "CHARGING", "AUTO_ARRIVE_CHARGING")
        )


def resume_drive(vehicle, mission, speed_kmh=18):
    """Continue a leg after the vehicle was held on the road (autopilot re-engaged)."""

    vehicle = _key(vehicle)

    with _lock:

        current = vehicles[vehicle]

        _require_movable(current)

        return update_vehicle_state(
            vehicle,
            **_drive_changes(speed_kmh, mission, f"AUTO_RESUME_{mission}")
        )


def resume_cruise(vehicle, speed_kmh=18):
    """A running vehicle whose commanded speed was cut to 0 (a radar
    obstacle that has cleared) drives on."""

    vehicle = _key(vehicle)

    with _lock:

        current = vehicles[vehicle]

        if current["status"] != STATUS_RUNNING:
            raise ValueError("Vehicle is not running.")

        _require_not_emergency(current)
        _require_path_clear(current)
        _require_can_drive(current)

        return update_vehicle_state(
            vehicle,
            speed=speed_kmh,
            motor_rpm=int(speed_kmh * 100),
            braking_status=BRAKE_NOT_APPLIED,
            motor_direction=MOTOR_STRAIGHT,
        )


def set_battery(vehicle, percentage):
    """Battery level from the battery model (0..100)."""

    percentage = round(max(0.0, min(100.0, float(percentage))), 2)

    return update_vehicle_state(
        vehicle,
        battery_percentage=percentage,
        battery_level=percentage,
    )


def set_fault(vehicle, fault):
    """Simulated breakdown (fault = e.g. "BREAKDOWN") or repair (fault = None).

    A breakdown holds the vehicle where it is. A repair only clears
    the fault: the vehicle stays held until it is started or the
    autopilot continues.
    """

    vehicle = _key(vehicle)

    with _lock:

        if fault:

            changes = {
                "fault": str(fault),
                "health_score": 20,
                "braking_status": BRAKE_APPLIED,
                "motor_direction": MOTOR_STOPPED,
                "speed": 0,
                "motor_rpm": 0,
                "loading_status": "idle",
                "next_action": str(fault),
                "last_command": "FAULT",
            }

            if not vehicles[vehicle]["emergency_stop"]:
                # Never overwrite an emergency stop's status.
                changes.update(status=STATUS_STOPPED, mission_status="HELD")

            return update_vehicle_state(vehicle, **changes)

        return update_vehicle_state(vehicle, fault=None, health_score=100)


# =========================================================
# PREPARE THE NEXT VEHICLE (V2V HANDOVER)
# =========================================================

# Kept across a handover (the record is otherwise renewed).
PRESERVED_ON_HANDOVER = (
    "battery_percentage",
    "battery_level",
    "signal_strength",
    "autopilot_mode",
)


def prepare_for_handover(vehicle):
    """Make the next vehicle ready at Stop 1 for its cycle.

    Refuses a vehicle under emergency stop (a handover must
    never clear a latch), and keeps its radar and obstacle
    readings (a handover must never clear a hazard).
    """

    vehicle = _key(vehicle)

    with _lock:

        current = vehicles[vehicle]

        if current["emergency_stop"]:

            raise TransitionBlocked(
                f"{vehicle} is under emergency stop; handover not performed.",
                "NEXT_VEHICLE_EMERGENCY"
            )

        hazards = {
            "radar_status": current["radar_status"],
            "obstacle_detected": current["obstacle_detected"],
        }

        # A handover is not a fresh vehicle: keep its battery, its
        # radio reading and whether the autopilot drives it.
        hazards.update({
            key: current[key]
            for key in PRESERVED_ON_HANDOVER
            if key in current
        })

        vehicles[vehicle] = _new_record(vehicle)

        _bump_placement(vehicle)

        return update_vehicle_state(
            vehicle,
            status=STATUS_READY,
            location=STOP_1,
            current_stop=STOP_1,
            load_percentage=0,
            load=0,
            loading_status="idle",
            loading_progress=0,
            braking_status=BRAKE_APPLIED,
            motor_direction=MOTOR_STOPPED,
            speed=0,
            emergency_stop=False,
            handover_status="ready",
            v2v_status="connected",
            **hazards
        )


# =========================================================
# EMERGENCY STOP
# =========================================================

def emergency_stop(vehicle):

    return update_vehicle_state(

        vehicle,

        status=STATUS_EMERGENCY,

        braking_status=BRAKE_APPLIED,

        motor_direction=MOTOR_STOPPED,

        speed=0,

        emergency_stop=True,
        
        mission_status="EMERGENCY",

        last_command="EMERGENCY_STOP",

        motor_rpm=0
    )


# =========================================================
# RELEASE EMERGENCY
# =========================================================

def release_emergency_stop(vehicle):

    current = get_vehicle_state(
        vehicle
    )


    return update_vehicle_state(

        vehicle,

        status=STATUS_READY,

        braking_status=BRAKE_APPLIED,

        motor_direction=MOTOR_STOPPED,

        speed=0,

        emergency_stop=False
    )


# =========================================================
# MANUAL DRIVE
# =========================================================

def is_loading(record):

    return (
        record.get("status") == STATUS_LOADING
        or record.get("loading_status") == "loading"
    )


def manual_drive(vehicle, command, direction, speed):
    """Apply a manual drive command after re-checking safety atomically.

    STOP is refused only under emergency stop (the vehicle is
    already stopped then). Any movement also needs a clear path
    and no loading in progress.
    """

    vehicle = _key(vehicle)

    with _lock:

        current = vehicles[vehicle]

        _require_not_emergency(current)

        moving = speed > 0

        if moving:

            _require_path_clear(current)

            if is_loading(current):

                raise TransitionBlocked(
                    "Manual movement blocked: loading is in progress.",
                    "LOADING_IN_PROGRESS"
                )

        changes = {
            "mode": "MANUAL",
            "status": STATUS_RUNNING,
            "motor_direction": direction,
            "speed": speed,
            "last_command": command,
            "mission_status": "MANUAL_CONTROL",
        }

        if moving:
            # Driving off a stop: its marker leaves the camera.
            changes.update(_tag_reading(False))

        return update_vehicle_state(vehicle, **changes)


# =========================================================
# QR STATUS
# =========================================================

def set_qr_status(
    vehicle,
    status
):
    """Record an AprilTag / QR reading. The reading is honoured.

    (It used to be forced to "detected", so the QR interlock
    could never block anything.) The demo override
    QR_ALWAYS_DETECTED lives in services/safety.py, which then
    ignores the reading; the reading itself is still stored.
    """

    status = str(status).lower().strip()

    if status not in (QR_DETECTED, QR_NOT_DETECTED):

        raise ValueError(
            "QR status must be detected or not_detected."
        )

    return update_vehicle_state(
        vehicle,
        **_tag_reading(status == QR_DETECTED)
    )


# =========================================================
# RADAR STATUS
# =========================================================

def set_radar_status(
    vehicle,
    status
):

    status = str(
        status
    ).lower().strip()


    if status not in (
        RADAR_CLEAR,
        RADAR_OBSTACLE
    ):

        raise ValueError(
            "Radar status must be clear "
            "or obstacle."
        )


    if status == RADAR_OBSTACLE:

        return update_vehicle_state(

            vehicle,

            radar_status=status,

            obstacle_detected=True,

            braking_status=BRAKE_APPLIED,

            speed=0
        )


    return update_vehicle_state(

        vehicle,

        radar_status=RADAR_CLEAR,

        obstacle_detected=False
    )


# =========================================================
# SAFE RESTORE AFTER A RESTART (PHASE 5)
# =========================================================
#
# State is otherwise in memory and lost on restart. On startup
# (app.py's lifespan, before the world engine or the autopilot
# start) each vehicle's last known db.py state_timeline row is
# installed here, with the ground rule: an unknown state after
# restart holds, it never moves. So this ALWAYS restores HELD
# (speed 0, brakes applied, autopilot MANUAL), restores the
# emergency latch exactly, and never resumes a mission by itself.
# The world engine's pose (segment/s or node) is placed by the
# caller (app.py), which owns that import.
# =========================================================

def restore_from_row(vehicle_id, row):

    vehicle_id = _key(vehicle_id)

    emergency = bool(row.get("emergency_stop"))
    battery = row.get("battery")
    location = row.get("location") or STOP_1

    with _lock:

        record = _new_record(vehicle_id)

        record.update(
            status=(STATUS_EMERGENCY if emergency else STATUS_STOPPED),
            location=location,
            current_stop=location,
            load_percentage=row.get("load_percentage") or 0,
            load=(row.get("load_percentage") or 0) * 10,
            loading_progress=row.get("load_percentage") or 0,
            braking_status=BRAKE_APPLIED,
            motor_direction=MOTOR_STOPPED,
            speed=0,
            motor_rpm=0,
            emergency_stop=emergency,
            mission_status=("EMERGENCY" if emergency else "HELD"),
            last_command="RESTORED",
            next_action="RESTORED_HELD",
            fault=row.get("fault"),
            battery_percentage=(battery if battery is not None else 100),
            battery_level=(battery if battery is not None else 100),
            autopilot_mode="MANUAL",
        )

        vehicles[vehicle_id] = record

        _bump_placement(vehicle_id)

        return deepcopy(record)


# =========================================================
# RESET VEHICLE
# =========================================================

def reset_vehicle(vehicle):

    vehicle = _key(
        vehicle
    )


    with _lock:

        vehicles[vehicle] = _new_record(
            vehicle
        )

        _bump_placement(vehicle)

        vehicles[vehicle]["qr_status"] = QR_DETECTED
        vehicles[vehicle]["Apriltag_status"] = QR_DETECTED
        vehicles[vehicle]["current_stop"] = STOP_1


        return deepcopy(
            vehicles[vehicle]
        )


# =========================================================
# RESET COMPLETE CYCLE
# =========================================================

def reset_cycle():

    return {

        vehicle_id:
            reset_vehicle(
                vehicle_id
            )

        for vehicle_id in fleet.vehicle_ids()
    }