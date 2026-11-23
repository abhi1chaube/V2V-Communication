# =========================================================
# THINGSBOARD SERVICE
# =========================================================
#
# Central ThingsBoard communication layer.
#
# Responsibilities:
#   1. ThingsBoard login
#   2. JWT token management
#   3. Device identification
#   4. Read device telemetry
#   5. Send device telemetry
#   6. Read latest vehicle state
#   7. Normalize load into percentage
#
# =========================================================

import requests
import urllib3

from . import fleet
from .settings import settings


# =========================================================
# CONFIGURATION (from services/settings.py)
# =========================================================

THINGSBOARD_URL = settings.thingsboard_base_url

THINGSBOARD_USERNAME = settings.thingsboard_username

THINGSBOARD_PASSWORD = (
    settings.thingsboard_password.get_secret_value()
    if settings.thingsboard_password is not None
    else None
)


# TLS certificate checking for every ThingsBoard request.
#
# On by default. The corporate network intercepts TLS with its
# own root certificate, which the public certifi bundle does
# not contain but the Windows certificate store does (IT
# installs it there). With THINGSBOARD_TRUST_STORE=system (the
# default) Python verifies against the Windows store through
# the truststore package, so verification works on and off the
# corporate network.
#
# Other options:
#   THINGSBOARD_CA_BUNDLE=C:\path\to\corporate-root.pem
#       verify against an exported PEM file instead
#   THINGSBOARD_TRUST_STORE=certifi
#       public CAs only (networks without interception)
#   THINGSBOARD_VERIFY_TLS=false
#       no verification (not recommended)
VERIFY_TLS = settings.thingsboard_tls_verify

if VERIFY_TLS is True and settings.thingsboard_trust_store == "system":

    try:

        import truststore

        # Process-wide: Python's ssl module now verifies with
        # the operating system's certificate store.
        truststore.inject_into_ssl()

    except ImportError:

        print(
            "[thingsboard] truststore is not installed; TLS is verified "
            "against the certifi bundle only. Install requirements.txt."
        )

if VERIFY_TLS is False:
    urllib3.disable_warnings(
        urllib3.exceptions.InsecureRequestWarning
    )


def redact(text, *secrets):
    """`text` with every secret (device token, password) masked."""

    text = str(text)

    for secret in secrets:

        if secret:
            text = text.replace(str(secret), "••••")

    return text


# =========================================================
# DEVICE IDs / TOKENS
# =========================================================
#
# Each vehicle's device ID and access token come from
# BACKEND/data/vehicles.json, through services/fleet.py.
# .env holds only the server credentials above.
# =========================================================


# =========================================================
# ERRORS
# =========================================================

class DeviceNotFound(Exception):
    """ThingsBoard answered 404: the device no longer exists."""

    def __init__(self, device_id):

        super().__init__(
            f"Device {device_id} not found in ThingsBoard."
        )

        self.device_id = device_id


class ThingsBoardError(Exception):
    """A ThingsBoard management call failed. Carries TB's own message."""

    def __init__(self, message, status=None):

        super().__init__(message)

        self.status = status


# =========================================================
# TOKEN STORAGE
# =========================================================

_jwt_token = None


# =========================================================
# VALIDATE CONFIGURATION
# =========================================================

def validate_config():

    required = {
        "THINGSBOARD_USERNAME":
            THINGSBOARD_USERNAME,

        "THINGSBOARD_PASSWORD":
            THINGSBOARD_PASSWORD
    }


    missing = [

        key

        for key, value
        in required.items()

        if not value
    ]

    # Vehicle records are not checked here: one incomplete
    # vehicle must not block login for the whole fleet.
    # get_device_id() / get_device_token() report it instead.


    if missing:

        raise RuntimeError(

            "Missing ThingsBoard environment "
            "variables: "
            + ", ".join(missing)
        )


# =========================================================
# LOGIN
# =========================================================

def login():

    global _jwt_token


    validate_config()


    url = (
        f"{THINGSBOARD_URL}"
        "/api/auth/login"
    )


    payload = {

        "username":
            THINGSBOARD_USERNAME,

        "password":
            THINGSBOARD_PASSWORD
    }


    response = requests.post(
    url,
    json=payload,
    timeout=10,
    verify=VERIFY_TLS
)

    response.raise_for_status()


    result = response.json()


    _jwt_token = result.get(
        "token"
    )


    if not _jwt_token:

        raise RuntimeError(
            "ThingsBoard login succeeded "
            "but JWT token was not returned."
        )


    return _jwt_token


# =========================================================
# GET JWT TOKEN
# =========================================================

def get_token():

    global _jwt_token


    if _jwt_token:

        return _jwt_token


    return login()


# =========================================================
# AUTHENTICATED HEADERS
# =========================================================

def authenticated_headers():

    token = get_token()


    return {

        "X-Authorization":
            f"Bearer {token}",

        "Content-Type":
            "application/json"
    }


# =========================================================
# GET DEVICE ID
# =========================================================
#
# This function fixes the error:
#
# module 'services.thingsboard'
# has no attribute 'get_device_id'
#
# =========================================================

def get_device_id(vehicle):

    device_id = fleet.device_id(vehicle)

    if not device_id:

        raise ValueError(
            f"{fleet.display_name(vehicle)} has no ThingsBoard "
            "device ID. Set it in the Vehicle Manager."
        )

    return device_id


# =========================================================
# GET DEVICE TOKEN
# =========================================================

def get_device_token(vehicle):

    device_token = fleet.token(vehicle)

    if not device_token:

        raise ValueError(
            f"{fleet.display_name(vehicle)} has no ThingsBoard "
            "access token. Set it in the Vehicle Manager."
        )

    return device_token


# =========================================================
# SEND DEVICE TELEMETRY
# =========================================================

def send_telemetry(
    device_token,
    telemetry
):

    if not device_token:

        raise ValueError(
            "Device token is required."
        )


    if not isinstance(
        telemetry,
        dict
    ):

        raise ValueError(
            "Telemetry must be a dictionary."
        )


    url = (
        f"{THINGSBOARD_URL}"
        f"/api/v1/{device_token}"
        "/telemetry"
    )

    # The device token is part of the URL, and requests puts
    # the URL into its error messages. Those messages reach API
    # responses and logs, so the token is masked here.
    try:

        response = requests.post(
            url,
            json=telemetry,
            timeout=10,
            verify=VERIFY_TLS
        )

        response.raise_for_status()

    except requests.RequestException as exc:

        status = getattr(
            getattr(exc, "response", None),
            "status_code",
            None
        )

        raise ThingsBoardError(
            redact(f"Telemetry push failed: {exc}", device_token),
            status
        ) from None


    return {

        "success": True,

        "status_code":
            response.status_code
    }


# =========================================================
# GET DEVICE TELEMETRY
# =========================================================

def get_device_telemetry(
    device_id,
    keys=None
):

    if not device_id:

        raise ValueError(
            "Device ID is required."
        )


    # -----------------------------------------------------
    # DEFAULT TELEMETRY KEYS
    # -----------------------------------------------------

    if keys is None:

        keys = [

            "vehicle_status",

            "current_stop",

            "load",

            "latitude",

            "longitude",

            "v2v_status",

            "handover_status",

            "load_percentage",

            "loading_status",

            "loading_progress",

            "braking_status",

            "motor_direction",

            "Apriltag_status",

            "radar_status",

            "obstacle_detected",

            "speed",

            "emergency_stop",
            
            "vehicle_id",
            "vehicle_name",

            "mission_status",

            "current_zone",

            "safety_status",

            "communication_status",

            "vehicle_online",

            "vehicle_type",

            "fleet_status",

            "vehicles_online",

            "next_action",

            "last_seen",

            "battery_level",

            "signal_strength",

            "destination",

            "distance_to_destination",
        ]


    key_string = ",".join(keys)


    url = (

        f"{THINGSBOARD_URL}"

        "/api/plugins/telemetry/DEVICE/"

        f"{device_id}"

        "/values/timeseries"

        f"?keys={key_string}"
    )


    response = requests.get(

        url,

        headers=authenticated_headers(),

        timeout=10,
        verify=VERIFY_TLS
    )


    # -----------------------------------------------------
    # JWT EXPIRED
    # -----------------------------------------------------

    if response.status_code == 401:

        login()

        response = requests.get(

            url,

            headers=authenticated_headers(),

            timeout=10,

            verify=VERIFY_TLS
        )


    # Only 404 means the device is gone. 403, 5xx and timeouts
    # stay ordinary errors and never lead to removal.
    if response.status_code == 404:

        raise DeviceNotFound(device_id)


    response.raise_for_status()


    return response.json()


# =========================================================
# DEVICE EXISTS
# =========================================================

def device_exists(device_id):
    """True/False from a direct device lookup.

    False only on 404. Any other failure raises, so a caller
    never treats an outage as a deleted device.
    """

    if not device_id:

        raise ValueError(
            "Device ID is required."
        )


    url = (
        f"{THINGSBOARD_URL}"
        f"/api/device/{device_id}"
    )


    response = requests.get(
        url,
        headers=authenticated_headers(),
        timeout=10,
        verify=VERIFY_TLS
    )


    if response.status_code == 401:

        login()

        response = requests.get(
            url,
            headers=authenticated_headers(),
            timeout=10,
            verify=VERIFY_TLS
        )


    if response.status_code == 404:
        return False


    response.raise_for_status()


    return True


# =========================================================
# DEVICE MANAGEMENT (AUTO-PROVISIONING)
# =========================================================
#
# Used by services/provisioning.py when the dashboard adds a
# vehicle with only a name. Requires a TENANT_ADMIN account
# (or a role that can create/delete devices and read their
# credentials).
# =========================================================

def _api(method, path, **kwargs):
    """Authenticated ThingsBoard REST call, re-logging in once on 401."""

    url = f"{THINGSBOARD_URL}{path}"

    def send():

        return requests.request(
            method,
            url,
            headers=authenticated_headers(),
            timeout=15,
            verify=VERIFY_TLS,
            **kwargs
        )

    response = send()

    if response.status_code == 401:

        login()

        response = send()

    return response


def _error_message(response):

    try:
        message = response.json().get("message")
    except ValueError:
        message = None

    return message or f"ThingsBoard HTTP {response.status_code}"


def _raise_for(response, action):

    if not response.ok:

        raise ThingsBoardError(
            f"{action} failed: {_error_message(response)}",
            response.status_code
        )


def get_device_quota():
    """{"devices": n, "max_devices": m or None}, or None if unknown.

    ThingsBoard Cloud reports this at /api/usage. Servers
    without that endpoint (Community Edition) return None.
    """

    response = _api("GET", "/api/usage")

    if response.status_code in (403, 404):
        return None

    _raise_for(response, "Reading the device limit")

    usage = response.json()

    max_devices = usage.get("maxDevices")

    return {
        "devices": usage.get("devices"),

        # 0 means "unlimited" in ThingsBoard limits.
        "max_devices": max_devices or None
    }


def find_device_by_name(name):
    """The tenant device with exactly this name, or None."""

    response = _api(
        "GET",
        "/api/tenant/devices",
        params={"deviceName": name}
    )

    if response.status_code == 404:
        return None

    _raise_for(response, "Looking up the device name")

    return response.json()


def create_device(name, label=None):
    """Create a device on the default profile. Returns its ID."""

    body = {
        "name": name,
        "type": "default"
    }

    if label:
        body["label"] = label

    response = _api(
        "POST",
        "/api/device",
        json=body
    )

    _raise_for(response, "Creating the ThingsBoard device")

    return response.json()["id"]["id"]


def get_device_access_token(device_id):
    """The device's access token (ACCESS_TOKEN credentials only)."""

    response = _api(
        "GET",
        f"/api/device/{device_id}/credentials"
    )

    _raise_for(response, "Reading the device access token")

    credentials = response.json()

    if credentials.get("credentialsType") != "ACCESS_TOKEN":

        raise ThingsBoardError(
            "The device does not use access-token credentials "
            f"({credentials.get('credentialsType')})."
        )

    return credentials["credentialsId"]


def rename_device(device_id, new_name):

    response = _api(
        "GET",
        f"/api/device/{device_id}"
    )

    _raise_for(response, "Reading the ThingsBoard device")

    device = response.json()

    device["name"] = new_name

    response = _api(
        "POST",
        "/api/device",
        json=device
    )

    _raise_for(response, "Renaming the ThingsBoard device")


def delete_device(device_id):
    """Delete a device. A device that is already gone counts as deleted."""

    response = _api(
        "DELETE",
        f"/api/device/{device_id}"
    )

    if response.status_code == 404:
        return

    _raise_for(response, "Deleting the ThingsBoard device")


# =========================================================
# GET LATEST TELEMETRY VALUE
# =========================================================

def latest_value(
    telemetry,
    key,
    default=None
):

    values = telemetry.get(key)


    if not values:

        return default


    latest = values[0]


    return latest.get(
        "value",
        default
    )


# =========================================================
# NORMALIZE LOAD PERCENTAGE
# =========================================================

def normalize_load_percentage(value):

    try:

        value = float(value)

    except (
        TypeError,
        ValueError
    ):

        return 0


    return max(
        0,
        min(
            100,
            value
        )
    )


# =========================================================
# GET LOAD PERCENTAGE
# =========================================================

def get_load_percentage(telemetry):

    percentage = latest_value(
        telemetry,
        "load_percentage",
        None
    )

    if percentage is None:
        return 0

    return normalize_load_percentage(
        percentage
    )


# =========================================================
# GET LATEST DEVICE STATE
# =========================================================

def get_latest_state(device_id):

    telemetry = get_device_telemetry(
        device_id
    )


    state = {

        "vehicle_status":
            latest_value(
                telemetry,
                "vehicle_status",
                "unknown"
            ),
                
            "vehicle_id":
            latest_value(
                telemetry,
                "vehicle_id",
                ""
            ),

        "vehicle_name":
            latest_value(
                telemetry,
                "vehicle_name",
                ""
            ),

        "mission_status":
            latest_value(
                telemetry,
                "mission_status",
                "UNKNOWN"
            ),

        "current_zone":
            latest_value(
                telemetry,
                "current_zone",
                "UNKNOWN"
            ),

        "location":
            latest_value(
                telemetry,
                "current_stop",
                latest_value(
                    telemetry,
                    "location",
                    "unknown"
                )
            ),

        "current_stop":
            latest_value(
                telemetry,
                "current_stop",
                latest_value(
                    telemetry,
                    "location",
                    "unknown"
                )
            ),

        "load":
            float(
                latest_value(
                    telemetry,
                    "load",
                    0
                )
            ),

        "latitude":
            latest_value(
                telemetry,
                "latitude",
                0.0
            ),

        "longitude":
            latest_value(
                telemetry,
                "longitude",
                0.0
            ),

        "v2v_status":
            latest_value(
                telemetry,
                "v2v_status",
                "disconnected"
            ),

        "handover_status":
            latest_value(
                telemetry,
                "handover_status",
                "idle"
            ),
        
        "communication_status":
            latest_value(
                telemetry,
                "communication_status",
                "DISCONNECTED"
            ),

        "load_percentage":
            get_load_percentage(
                telemetry
            ),

        "loading_status":
            latest_value(
                telemetry,
                "loading_status",
                "idle"
            ),

        "loading_progress":
            normalize_load_percentage(
                latest_value(
                    telemetry,
                    "loading_progress",
                    latest_value(
                        telemetry,
                        "load_percentage",
                        0
                    )
                )
            ),

        "braking_status":
            latest_value(
                telemetry,
                "braking_status",
                "unknown"
            ),

        "motor_direction":
            latest_value(
                telemetry,
                "motor_direction",
                "unknown"
            ),

        "Apriltag_status":
            latest_value(
                telemetry,
                "Apriltag_status",
                latest_value(
                    telemetry,
                    "qr_status",
                    "not_detected"
                )
            ),


        "radar_status":
            latest_value(
                telemetry,
                "radar_status",
                "clear"
            ),

        "obstacle_detected":
            latest_value(
                telemetry,
                "obstacle_detected",
                False
            ),

        "speed":
            latest_value(
                telemetry,
                "speed",
                0
            ),

        "emergency_stop":
            latest_value(
                telemetry,
                "emergency_stop",
                False
            ),
        "safety_status":
            latest_value(
                telemetry,
                "safety_status",
             "SAFE"
            ),

        "vehicle_online":
            latest_value(
                telemetry,
                "vehicle_online",
                True
            ),

        "vehicle_type":
            latest_value(
                telemetry,
                "vehicle_type",
                "AGV"
            ),

        "fleet_status":
            latest_value(
                telemetry,
                "fleet_status",
                "ACTIVE"
            ),

        "vehicles_online":
            latest_value(
                telemetry,
                "vehicles_online",
                0
            ),

        "next_action":
            latest_value(
                telemetry,
                "next_action",
                "NONE"
            ),

        "last_seen":
            latest_value(
                telemetry,
                "last_seen",
                ""
            ),

        "battery_level":
            latest_value(
                telemetry,
                "battery_level",
                None
            ),

        "signal_strength":
            latest_value(
                telemetry,
                "signal_strength",
                None
            ),

        "destination":
            latest_value(
                telemetry,
                "destination",
                None
            ),

        "distance_to_destination":
            latest_value(
                telemetry,
                "distance_to_destination",
                None
            ),

        # Keys ThingsBoard actually returned a value for.
        # Lets state.apply_telemetry_state() ignore the
        # placeholder defaults above.
        "_reported":
            sorted(
                key
                for key, values in telemetry.items()
                if values
            ),

        # Newest ThingsBoard timestamp (epoch ms) in this read,
        # or None. state.apply_telemetry_state() uses it to
        # ignore values older than a reset / emergency stop.
        "_ts":
            max(
                (
                    values[0].get("ts")
                    for values in telemetry.values()
                    if values and isinstance(values[0].get("ts"), (int, float))
                ),
                default=None
            ),
    }


    return state


# =========================================================
# GET VEHICLE TELEMETRY (ANY REGISTERED VEHICLE)
# =========================================================

def get_vehicle_telemetry(
    vehicle,
    keys=None
):

    return get_device_telemetry(
        get_device_id(vehicle),
        keys
    )


# =========================================================
# GET VEHICLE STATE
# =========================================================

def get_vehicle_state(vehicle):

    device_id = get_device_id(
        vehicle
    )


    return get_latest_state(
        device_id
    )


# =========================================================
# CONNECTION TEST
# =========================================================

def test_connection():

    try:

        login()


        return {

            "connected": True,

            "message":
                "ThingsBoard connection successful."
        }


    except Exception as error:

        return {

            "connected": False,

            "message":
                str(error)
        }
 # =========================================================
# SEND VEHICLE TELEMETRY (ANY REGISTERED VEHICLE)
# =========================================================

def send_vehicle_telemetry(vehicle, telemetry):
    """
    Send telemetry for any registered vehicle.

    Accepts a vehicle ID or alias (e.g. "Vehicle_03", "vehicle 03",
    "yellow"), or an actual ThingsBoard device token for
    backward compatibility.
    """

    vehicle_id = fleet.resolve(vehicle)

    if vehicle_id is not None:
        device_token = get_device_token(vehicle_id)
    else:
        device_token = vehicle

    return send_telemetry(
        device_token,
        telemetry
    )
