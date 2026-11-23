# =========================================================
# HARDWARE SERVICE
# =========================================================
#
# Hardware abstraction layer for the V2V vehicle system.
#
# Currently:
#   - Provides simulated/default hardware status.
#   - Does NOT require Raspberry Pi, Arduino or GPIO.
#
# Later:
#   - Real QR sensor
#   - Radar/ultrasonic sensor
#   - Brake actuator
#   - Motor controller
#   - Emergency-stop switch
#   - Load sensor
#
# The rest of the backend should communicate through this
# service instead of directly accessing hardware.
#
# =========================================================


# =========================================================
# HARDWARE MODE
# =========================================================

HARDWARE_MODE = "simulation"


# =========================================================
# SENSOR STATUS
# =========================================================

def get_sensor_status():

    return {
        "qr_sensor": {
            "available": False,
            "mode": HARDWARE_MODE,
            "status": "not_connected"
        },

        "radar_sensor": {
            "available": False,
            "mode": HARDWARE_MODE,
            "status": "not_connected"
        },

        "load_sensor": {
            "available": False,
            "mode": HARDWARE_MODE,
            "status": "not_connected"
        },

        "brake_actuator": {
            "available": False,
            "mode": HARDWARE_MODE,
            "status": "not_connected"
        },

        "motor_controller": {
            "available": False,
            "mode": HARDWARE_MODE,
            "status": "not_connected"
        },

        "emergency_stop": {
            "available": False,
            "mode": HARDWARE_MODE,
            "status": "not_connected"
        }
    }


# =========================================================
# HARDWARE STATUS
# =========================================================

def get_hardware_status():

    sensors = get_sensor_status()

    return {
        "hardware_mode": HARDWARE_MODE,

        "connected": False,

        "simulation": True,

        "message": (
            "Hardware interface is ready. "
            "Real sensors are not connected yet."
        ),

        "sensors": sensors
    }


# =========================================================
# QR SENSOR
# =========================================================

def read_qr_sensor():

    return {
        "detected": False,
        "status": "not_detected",
        "source": HARDWARE_MODE
    }


# =========================================================
# RADAR SENSOR
# =========================================================

def read_radar_sensor():

    return {
        "obstacle": False,
        "status": "clear",
        "source": HARDWARE_MODE
    }


# =========================================================
# LOAD SENSOR
# =========================================================

def read_load_sensor():

    return {
        "load_percentage": 0,
        "source": HARDWARE_MODE
    }


# =========================================================
# BRAKE ACTUATOR
# =========================================================

def apply_brake():

    return {
        "success": True,
        "brake": "applied",
        "source": HARDWARE_MODE
    }


def release_brake():

    return {
        "success": True,
        "brake": "released",
        "source": HARDWARE_MODE
    }


# =========================================================
# MOTOR CONTROLLER
# =========================================================

def start_motor():

    return {
        "success": True,
        "motor": "running",
        "source": HARDWARE_MODE
    }


def stop_motor():

    return {
        "success": True,
        "motor": "stopped",
        "source": HARDWARE_MODE
    }


# =========================================================
# EMERGENCY STOP
# =========================================================

def activate_emergency_stop():

    # Physical emergency-stop hardware will eventually
    # be handled here.

    return {
        "success": True,
        "emergency_stop": True,
        "source": HARDWARE_MODE
    }


def release_emergency_stop():

    return {
        "success": True,
        "emergency_stop": False,
        "source": HARDWARE_MODE
    }


# =========================================================
# HARDWARE SELF TEST
# =========================================================

def self_test():

    return {
        "success": True,

        "hardware_mode":
            HARDWARE_MODE,

        "simulation":
            True,

        "message":
            "Hardware abstraction layer is operational."
    }
# services/hardware.py

class HardwareController:

    def execute_command(self, command):

        print(f"[HARDWARE] Executing: {command}")

        return True

    def move_servo(self, angle):

        print(f"[HARDWARE] Servo moved to {angle}")

        return True