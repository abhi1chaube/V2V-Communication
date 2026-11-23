# services/servo.py

from services.hardware import HardwareController

hardware = HardwareController()


def set_servo_angle(angle):

    if angle < 0 or angle > 180:
        raise ValueError("Angle must be between 0 and 180")

    hardware.move_servo(angle)

    return {
        "success": True,
        "angle": angle
    }