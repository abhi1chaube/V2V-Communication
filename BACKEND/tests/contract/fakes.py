"""In-process fakes for ThingsBoard, shared by the contract runner and the pytest suite.

install() replaces the ThingsBoard functions the backend calls with
fakes whose behaviour tests can change through the returned
FakeThingsBoard object. block_network() makes any real HTTP request
fail loudly, so a missing fake can never reach the cloud.
"""

import requests


class FakeThingsBoard:

    def __init__(self, tb):

        self.tb = tb

        # Original functions, for tests of the real code paths.
        self.original = {
            name: getattr(tb, name)
            for name in (
                "get_latest_state", "send_vehicle_telemetry", "send_telemetry",
                "test_connection", "login", "device_exists", "get_device_quota",
                "find_device_by_name", "create_device", "get_device_access_token",
                "rename_device", "delete_device",
            )
        }

        self.reset()

    def reset(self):

        # Behaviour switches (tests change these).
        self.push_error = None            # str: every telemetry push fails with it
        self.latest_state = None          # callable(device_id) -> dict, or None for {}
        self.missing_devices = set()      # device IDs that answer 404
        self.quota = {"devices": 2, "max_devices": 5}

        # Recorded calls.
        self.pushes = []

        self._devices = {}
        self._counter = 100

    # ---- fakes ---------------------------------------------------

    def get_latest_state(self, device_id):

        if device_id in self.missing_devices:
            raise self.tb.DeviceNotFound(device_id)

        return self.latest_state(device_id) if self.latest_state else {}

    def send_vehicle_telemetry(self, vehicle, telemetry):

        self.pushes.append((vehicle, dict(telemetry)))

        if self.push_error:
            raise self.tb.ThingsBoardError(self.push_error, 503)

        return {"success": True, "fake": True}

    def send_telemetry(self, token, telemetry):

        return {"success": True, "fake": True, "status_code": 200}

    def device_exists(self, device_id):

        return device_id not in self.missing_devices

    def create_device(self, name, label=None):

        self._counter += 1
        device_id = f"00000000-0000-4000-8000-{self._counter:012d}"
        self._devices[device_id] = name
        return device_id

    def delete_device(self, device_id):

        self._devices.pop(device_id, None)


def install():
    """Install the fakes into services.thingsboard; return the controller."""

    from services import thingsboard as tb

    fake = FakeThingsBoard(tb)

    tb.get_latest_state = fake.get_latest_state
    tb.send_vehicle_telemetry = fake.send_vehicle_telemetry
    tb.send_telemetry = fake.send_telemetry
    tb.test_connection = lambda: {"connected": True, "message": "ThingsBoard connection successful."}
    tb.login = lambda: "fake-jwt"
    tb.device_exists = fake.device_exists
    tb.get_device_quota = lambda: dict(fake.quota)
    tb.find_device_by_name = lambda name: None
    tb.create_device = fake.create_device
    tb.get_device_access_token = lambda device_id: f"FAKE_TOKEN_{device_id[-4:]}"
    tb.rename_device = lambda device_id, new_name: None
    tb.delete_device = fake.delete_device

    return fake


def block_network():
    """Make every real HTTP request raise, so a missed fake fails loudly."""

    def refuse(self, method, url, *args, **kwargs):
        raise RuntimeError(f"network disabled in tests: {method} <url withheld>")

    requests.sessions.Session.request = refuse
