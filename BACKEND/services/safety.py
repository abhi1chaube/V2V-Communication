# =========================================================
# SAFETY SERVICE
# =========================================================

from . import state
from . import fleet
from .settings import settings


def tag_detected(current):
    """True when the AprilTag/QR marker counts as detected.

    The demo override QR_ALWAYS_DETECTED treats it as always
    detected (the old, hard-wired behaviour). It is read on
    every call, so tests and a settings change take effect at
    once.
    """

    return (
        settings.qr_always_detected
        or current["qr_status"] == state.QR_DETECTED
    )


# =========================================================
# GENERIC RESULT
# =========================================================

def allowed_result():

    return {
        "allowed": True,
        "reason": "Command allowed.",
        "code": None
    }


def blocked_result(
    reason,
    code
):

    return {
        "allowed": False,
        "reason": reason,
        "code": code
    }


# =========================================================
# START VALIDATION
# =========================================================

def validate_start_command(vehicle):

    current = state.get_vehicle_state(
        vehicle
    )


    if current["emergency_stop"]:

        return blocked_result(
            "Vehicle is under emergency stop.",
            "EMERGENCY_STOP_ACTIVE"
        )


    if (
        current["radar_status"] == state.RADAR_OBSTACLE
        or current["obstacle_detected"]
    ):

        return blocked_result(
            "Vehicle cannot start: radar detected an obstacle.",
            "RADAR_OBSTACLE"
        )


    if current["location"] != state.STOP_1:

        return blocked_result(
            "Vehicle can start only from Stop 1.",
            "INVALID_START_LOCATION"
        )


    if current.get("comm_hold"):

        return blocked_result(
            "Vehicle cannot start: V2V contact with every vehicle in range is lost.",
            "V2V_COMM_LOST"
        )


    if current["status"] == state.STATUS_RUNNING:

        return blocked_result(
            "Vehicle is already running.",
            "ALREADY_RUNNING"
        )


    if not tag_detected(current):

        return blocked_result(
            "Vehicle cannot start: the AprilTag/QR marker at Stop 1 is not detected.",
            "QR_NOT_DETECTED"
        )


    return allowed_result()


# =========================================================
# LOADING VALIDATION
# =========================================================

def validate_loading_command(vehicle):

    current = state.get_vehicle_state(
        vehicle
    )


    if current["emergency_stop"]:

        return blocked_result(
            "Loading blocked: emergency stop is active.",
            "EMERGENCY_STOP_ACTIVE"
        )


    if current["location"] != state.STOP_2:

        return blocked_result(
            "Loading can start only at Stop 2.",
            "NOT_AT_LOADING_POINT"
        )


    if (
        current["radar_status"] == state.RADAR_OBSTACLE
        or current["obstacle_detected"]
    ):

        return blocked_result(
            "Loading blocked: radar detected an obstacle.",
            "RADAR_OBSTACLE"
        )


    if current["braking_status"] != state.BRAKE_APPLIED:

        return blocked_result(
            "Loading blocked: vehicle brake must be applied.",
            "BRAKE_NOT_APPLIED"
        )


    if current["load_percentage"] >= 100:

        return blocked_result(
            "Vehicle is already fully loaded.",
            "LOAD_ALREADY_COMPLETE"
        )


    if not tag_detected(current):

        return blocked_result(
            "Loading blocked: the AprilTag/QR marker at Stop 2 is not detected, "
            "so the vehicle's position is not confirmed.",
            "QR_NOT_DETECTED"
        )


    return allowed_result()


# =========================================================
# WAREHOUSE VALIDATION
# =========================================================

def validate_warehouse_command(vehicle):

    current = state.get_vehicle_state(
        vehicle
    )


    if current["emergency_stop"]:

        return blocked_result(
            "Vehicle cannot move: emergency stop is active.",
            "EMERGENCY_STOP_ACTIVE"
        )


    if current["location"] != state.STOP_2:

        return blocked_result(
            "Vehicle must be at Stop 2 before moving to warehouse.",
            "INVALID_LOCATION"
        )


    if current["load_percentage"] < 100:

        return blocked_result(
            "Vehicle cannot move to warehouse before reaching 100% load.",
            "LOAD_NOT_COMPLETE"
        )


    if (
        current["radar_status"] == state.RADAR_OBSTACLE
        or current["obstacle_detected"]
    ):

        return blocked_result(
            "Vehicle cannot move: radar detected an obstacle.",
            "RADAR_OBSTACLE"
        )


    if current.get("comm_hold"):

        return blocked_result(
            "Vehicle cannot move: V2V contact with every vehicle in range is lost.",
            "V2V_COMM_LOST"
        )


    return allowed_result()


# =========================================================
# MANUAL CONTROL VALIDATION
# =========================================================

def validate_manual_command(vehicle, command):
    """Manual drive. STOP is always allowed except under emergency stop
    (stopping is always safe); movement also needs a clear path and no
    loading in progress. (Manual control used to check only the
    emergency stop.)"""

    current = state.get_vehicle_state(
        vehicle
    )

    if current["emergency_stop"]:

        return blocked_result(
            "Emergency stop active.",
            "EMERGENCY_STOP_ACTIVE"
        )

    if command == "STOP":

        return allowed_result()

    if current["radar_status"] == state.RADAR_OBSTACLE:

        return blocked_result(
            "Manual movement blocked: radar detected an obstacle.",
            "RADAR_OBSTACLE"
        )

    if current["obstacle_detected"]:

        return blocked_result(
            "Manual movement blocked: obstacle detected.",
            "OBSTACLE_DETECTED"
        )

    if state.is_loading(current):

        return blocked_result(
            "Manual movement blocked: loading is in progress.",
            "LOADING_IN_PROGRESS"
        )

    if current.get("comm_hold"):

        return blocked_result(
            "Manual movement blocked: V2V contact with every vehicle in range is lost.",
            "V2V_COMM_LOST"
        )

    return allowed_result()


# =========================================================
# GENERAL MOVEMENT VALIDATION
# =========================================================

def can_vehicle_move(
    vehicle,
    require_qr=False
):

    current = state.get_vehicle_state(
        vehicle
    )


    if current["emergency_stop"]:

        return blocked_result(
            "Movement blocked: emergency stop is active.",
            "EMERGENCY_STOP_ACTIVE"
        )


    if current["radar_status"] == state.RADAR_OBSTACLE:

        return blocked_result(
            "Movement blocked: radar detected an obstacle.",
            "RADAR_OBSTACLE"
        )


    if current["obstacle_detected"]:

        return blocked_result(
            "Movement blocked: obstacle detected.",
            "OBSTACLE_DETECTED"
        )


    if require_qr and not tag_detected(current):

        return blocked_result(
            "Movement blocked: the AprilTag/QR marker is not detected.",
            "QR_NOT_DETECTED"
        )


    return allowed_result()


# =========================================================
# EMERGENCY STOP VALIDATION
# =========================================================

def validate_emergency_stop(vehicle):

    try:

        state.get_vehicle_state(
            vehicle
        )

    except ValueError as error:

        return blocked_result(
            str(error),
            "INVALID_VEHICLE"
        )


    # Emergency stop should always be allowed
    # for a valid vehicle.

    return allowed_result()


# =========================================================
# SENSOR SAFETY STATUS
# =========================================================

def get_sensor_safety(vehicle):

    current = state.get_vehicle_state(
        vehicle
    )


    safe_to_move = (

        not current["emergency_stop"]

        and

        not current["obstacle_detected"]

        and

        current["radar_status"]
        == state.RADAR_CLEAR

        # The AprilTag/QR marker is needed to START (and to load),
        # not to keep moving, so it is not part of safe_to_move.
    )


    return {

        "vehicle":
            vehicle,

        "safe_to_move":
            safe_to_move,

        "qr_status":
            current["qr_status"],

        "radar_status":
            current["radar_status"],

        "obstacle_detected":
            current["obstacle_detected"],

        "braking_status":
            current["braking_status"],

        "emergency_stop":
            current["emergency_stop"]
    }


# =========================================================
# COMPLETE SAFETY SUMMARY
# =========================================================

def get_safety_summary():

    return {

        vehicle_id:
            get_sensor_safety(
                vehicle_id
            )

        for vehicle_id in fleet.vehicle_ids()
    }