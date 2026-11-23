# =========================================================
# VEHICLE SIMULATOR (API CLIENT)
# =========================================================
#
# Drives one vehicle's cycle through the backend's REST API,
# with the same commands the dashboard uses, so every safety
# check, the emergency latch and the event log apply:
#
#   wait at Stop 1 (for the handover, unless first in fleet order)
#   -> START -> drive to Stop 2 (waits until the world engine
#      reports the vehicle has arrived) -> LOADING 0..100 %
#   -> WAREHOUSE -> COMPLETE (+ V2V handover to the next vehicle)
#
# The backend must be running (python app.py).
#
# Usage:
#   python vehicle_simulator.py                    list the vehicles
#   python vehicle_simulator.py Vehicle_01         one cycle
#   python vehicle_simulator.py 2 --cycles 3       three cycles of Vehicle_02
#   python vehicle_simulator.py 1 --no-handover    complete without handing over
#   python vehicle_simulator.py 1 --url http://127.0.0.1:5000
#
# Ctrl+C stops the simulator; the vehicle keeps its last state.
#
# (The previous version wrote telemetry straight to ThingsBoard
# with each device's token. That bypassed the backend's safety
# checks and made two programs owners of the same vehicle's
# state. ThingsBoard now receives every change from the backend.)
# =========================================================

import argparse
import sys
import time
import uuid

import requests


DEFAULT_URL = "http://127.0.0.1:5000"

POLL_SECONDS = 0.5

# Safety blocks worth waiting out: the condition clears by itself
# or by an operator (obstacle removed, emergency released, ...).
RETRYABLE = {
    "RADAR_OBSTACLE", "OBSTACLE_DETECTED", "EMERGENCY_STOP_ACTIVE",
    "QR_NOT_DETECTED", "COMMAND_IN_PROGRESS", "NEXT_VEHICLE_EMERGENCY",
}

LOAD_STEPS = (25, 50, 75, 100)


class Backend:

    def __init__(self, url, vehicle):

        self.url = url.rstrip("/")
        self.vehicle = vehicle
        self.session = requests.Session()

    def get(self, path):

        response = self.session.get(f"{self.url}{path}", timeout=15)
        return response.status_code, _json(response)

    def post(self, path, **body):

        # One Idempotency-Key per command attempt: a network retry
        # of the same attempt cannot run the command twice.
        headers = {"Idempotency-Key": f"sim-{uuid.uuid4()}"}
        response = self.session.post(f"{self.url}{path}", json=body, headers=headers, timeout=30)
        return response.status_code, _json(response)

    def state(self):

        status, body = self.get(f"/api/vehicles/{self.vehicle}")
        if status != 200:
            raise SystemExit(f"Cannot read {self.vehicle}: {body.get('message')}")
        return body["data"]["vehicle"]

    def pose(self):

        status, body = self.get(f"/api/v1/vehicles/{self.vehicle}/pose")
        return body.get("data") if status == 200 else None


def _json(response):

    try:
        return response.json()
    except ValueError:
        return {"success": False, "message": response.text[:200]}


def log(vehicle, message):

    print(f"[{time.strftime('%H:%M:%S')}] [{vehicle}] {message}", flush=True)


# =========================================================
# STEPS
# =========================================================

def command(backend, path, label, **body):
    """Send a command; wait out retryable safety blocks; stop on anything else."""

    waited = False

    while True:

        status, result = backend.post(path, vehicle=backend.vehicle, **body)

        if status < 300 and result.get("success", True):
            data = result.get("data") or {}
            synced = data.get("telemetry_synced")
            note = "" if synced is None else f" (ThingsBoard synced: {synced})"
            log(backend.vehicle, f"{label}: {result.get('message', 'ok')}{note}")
            return data

        code = result.get("code")

        if code in RETRYABLE:
            if not waited:
                log(backend.vehicle, f"{label} blocked ({code}): {result.get('message')}. Waiting...")
                waited = True
            time.sleep(2)
            continue

        raise SystemExit(f"{label} failed ({status} {code}): {result.get('message')}")


def wait_at_stop_1(backend, first_in_order):

    announced = False

    while True:

        current = backend.state()

        at_stop_1 = current.get("location") == "stop_1"
        idle = current.get("status") in ("waiting", "ready")
        handed_over = current.get("handover_status") == "ready"

        if (
            not current.get("emergency_stop")
            and at_stop_1
            and idle
            and (first_in_order or handed_over)
        ):
            return

        if not announced:
            reason = (
                "emergency stop active" if current.get("emergency_stop")
                else "not at Stop 1 yet (waiting for a handover back to Stop 1, or a reset)"
                if not at_stop_1
                else "waiting for the handover from the previous vehicle"
            )
            log(backend.vehicle, f"Waiting at Stop 1: {reason}.")
            announced = True

        time.sleep(POLL_SECONDS * 2)


def wait_for_arrival(backend, stop, timeout):

    deadline = time.monotonic() + timeout
    last_report = 0.0

    while time.monotonic() < deadline:

        pose = backend.pose()

        if pose is None:
            return False          # backend without a world engine: do not wait

        if pose.get("arrived") and pose.get("stop") == stop:
            log(backend.vehicle, f"Arrived at {stop}.")
            return True

        distance = pose.get("distance_to_destination_m")

        if distance is not None and time.monotonic() - last_report > 5:
            log(backend.vehicle, f"Driving: {distance} m to go, {pose.get('speed_kmh')} km/h, "
                                 f"ETA {pose.get('eta_seconds')} s.")
            last_report = time.monotonic()

        time.sleep(POLL_SECONDS)

    log(backend.vehicle, f"Did not report arrival at {stop} within {timeout:.0f} s; continuing.")
    return False


def run_cycle(backend, first_in_order, handover, arrival_timeout):

    wait_at_stop_1(backend, first_in_order)

    command(backend, "/api/start", "START")

    wait_for_arrival(backend, "stop_2", arrival_timeout)

    command(backend, "/api/loading-point", "LOADING POINT")

    command(backend, "/api/loading/start", "LOADING START")

    for percentage in LOAD_STEPS:
        time.sleep(1)
        command(backend, "/api/loading/update", f"LOAD {percentage}%", percentage=percentage)

    command(backend, "/api/warehouse", "WAREHOUSE")

    command(backend, "/api/complete", "COMPLETE", handover=handover)


# =========================================================
# MAIN
# =========================================================

def main():

    parser = argparse.ArgumentParser(description="Drive a vehicle's cycle through the backend API.")
    parser.add_argument("vehicle", nargs="?", help="Vehicle name or alias (Vehicle_01, 01, 1, yellow).")
    parser.add_argument("--url", default=DEFAULT_URL, help=f"Backend URL (default {DEFAULT_URL}).")
    parser.add_argument("--cycles", type=int, default=1, help="Number of cycles (default 1).")
    parser.add_argument("--no-handover", dest="handover", action="store_false",
                        help="Complete the journey without handing over to the next vehicle.")
    parser.add_argument("--arrival-timeout", type=float, default=120,
                        help="Seconds to wait for the vehicle to reach Stop 2 (default 120).")
    args = parser.parse_args()

    try:
        listing = requests.get(f"{args.url.rstrip('/')}/api/vehicles", timeout=10).json()
    except requests.RequestException as exc:
        raise SystemExit(f"Backend not reachable at {args.url}: {type(exc).__name__}. Start it with: python app.py")

    order = [vehicle["vehicle_id"] for vehicle in listing["data"]["vehicles"]]

    if not args.vehicle:
        print("Registered vehicles, in fleet (handover) order:")
        for vehicle_id in order:
            print(f"  {vehicle_id}")
        print("\nUsage: python vehicle_simulator.py <vehicle> [--cycles N] [--no-handover]")
        return

    backend = Backend(args.url, args.vehicle)
    vehicle_id = backend.state()["vehicle_id"]
    backend.vehicle = vehicle_id

    first_in_order = bool(order) and order[0] == vehicle_id

    log(vehicle_id, f"Simulating {args.cycles} cycle(s) via {args.url}"
                    f"{' (first in fleet order)' if first_in_order else ''}.")

    try:
        for cycle in range(1, args.cycles + 1):
            log(vehicle_id, f"--- cycle {cycle} of {args.cycles} ---")
            run_cycle(backend, first_in_order, args.handover, args.arrival_timeout)
    except KeyboardInterrupt:
        log(vehicle_id, "Stopped by user.")
        sys.exit(130)

    log(vehicle_id, "Done.")


if __name__ == "__main__":
    main()
