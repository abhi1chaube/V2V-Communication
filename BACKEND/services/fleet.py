# =========================================================
# FLEET REGISTRY
# =========================================================
#
# Single source of truth for every vehicle in the fleet.
#
# Vehicles are application data, not configuration. They are
# stored in BACKEND/data/vehicles.json:
#
#   {
#     "vehicles": [
#       {
#         "name": "Vehicle_01",
#         "device_id": "<ThingsBoard device id>",
#         "token": "<ThingsBoard device access token>",
#         "color": "yellow",           (optional)
#         "managed": true              (optional: device was
#                                       created by the dashboard)
#       }
#     ]
#   }
#
# BACKEND/.env holds only the ThingsBoard server credentials
# (THINGSBOARD_URL, THINGSBOARD_USERNAME, THINGSBOARD_PASSWORD).
#
# Vehicles are added, edited and deleted at runtime through
# /api/vehicles (see app.py), or by editing the file. The file
# is re-read whenever it changes on disk, so other processes
# such as vehicle_simulator.py see the same fleet.
#
# The vehicle name is its ID. List order is fleet order: each
# vehicle waits for the previous one to finish, and hands over
# to the next one, which wraps around to the first vehicle.
# =========================================================

import json
import os
import re
import shutil
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from threading import RLock
from typing import Optional

# Importing settings also loads BACKEND/.env into os.environ,
# which _legacy_env_vehicles() scans.
from .settings import settings


# =========================================================
# CONFIGURATION
# =========================================================

DATA_FILE = settings.fleet_data_path

NAME_PATTERN = re.compile(
    r"^[A-Za-z][A-Za-z0-9_-]{0,39}$"
)

NAME_RULE = (
    "1-40 characters: letters, digits, '_' or '-', "
    "starting with a letter"
)

DEVICE_ID_PATTERN = re.compile(
    r"^[A-Za-z0-9-]{1,64}$"
)

# The token is placed in the ThingsBoard URL path.
TOKEN_PATTERN = re.compile(
    r"^[^\s/?#]{1,128}$"
)

COLOR_PATTERN = re.compile(
    r"^(?:[A-Za-z]{3,20}|#[0-9A-Fa-f]{3,8})$"
)

_NUMBER_ALIAS_PATTERN = re.compile(
    r"^(?:vehicle_)?0*(\d+)$"
)

_TRAILING_NUMBER_PATTERN = re.compile(
    r"(\d+)$"
)

# Only used once, to import vehicles from an old .env
# (Vehicle_NN_ID / Vehicle_NN_TOKEN) when vehicles.json does
# not exist yet. Windows upper-cases environment variable
# names, so the pattern is case-insensitive.
_LEGACY_ENV_PATTERN = re.compile(
    r"^vehicle_(\d+)_(id|token|color)$",
    re.IGNORECASE
)

# Colours the first two vehicles had before vehicles.json.
_LEGACY_COLORS = ("yellow", "blue")


# =========================================================
# ERRORS
# =========================================================

class FleetError(ValueError):
    """A registry change was rejected. Carries an API code and status."""

    def __init__(self, message, code, status=400):

        super().__init__(message)

        self.code = code

        self.status = status


# =========================================================
# VEHICLE RECORD
# =========================================================

@dataclass(frozen=True)
class Vehicle:

    vehicle_id: str

    device_id: str

    token: str

    color: Optional[str] = None

    # True when the dashboard created the ThingsBoard device
    # (auto-provisioned). Only such devices may be renamed or
    # deleted in ThingsBoard by this application.
    managed: bool = False

    @property
    def name(self):

        return self.vehicle_id

    @property
    def number(self):

        match = _TRAILING_NUMBER_PATTERN.search(self.vehicle_id)

        return int(match.group(1)) if match else None

    @property
    def short_name(self):

        number = self.number

        return f"V{number:02d}" if number is not None else self.vehicle_id

    def to_record(self):
        """Representation stored in vehicles.json."""

        record = {
            "name": self.name,
            "device_id": self.device_id,
            "token": self.token
        }

        if self.color:
            record["color"] = self.color

        if self.managed:
            record["managed"] = True

        return record

    def to_public(self):
        """Representation returned by the API. The token is masked."""

        return {
            "name": self.name,
            "vehicle_id": self.vehicle_id,
            "short_name": self.short_name,
            "device_id": self.device_id,
            "color": self.color,
            "managed": self.managed,
            "has_token": bool(self.token),
            "token_hint": (
                f"••••{self.token[-4:]}"
                if len(self.token) > 4
                else ("••••" if self.token else "")
            )
        }


# =========================================================
# REGISTRY STATE
# =========================================================

# Mutated in place, so references held elsewhere stay valid.
VEHICLES = {}

_lock = RLock()

_loaded_signature = None

_load_error = None


def _key(value):
    """Comparison key: case-insensitive, spaces/hyphens == '_'."""

    return re.sub(
        r"[\s\-]+",
        "_",
        str(value).strip().lower()
    )


# =========================================================
# FILE I/O
# =========================================================

def _file_signature():

    try:

        stat = DATA_FILE.stat()

    except FileNotFoundError:

        return None

    return (stat.st_mtime_ns, stat.st_size)


def _read_file():

    with open(DATA_FILE, encoding="utf-8") as handle:
        data = json.load(handle)

    if isinstance(data, dict):
        entries = data.get("vehicles", [])
    else:
        entries = data

    if not isinstance(entries, list):
        raise ValueError("'vehicles' must be a list.")

    vehicles = {}

    seen = set()

    for entry in entries:

        if not isinstance(entry, dict):
            continue

        name = str(entry.get("name") or "").strip()

        # Hand-edited files may contain blanks or duplicates.
        # Skip them instead of failing the whole fleet.
        if not name or _key(name) in seen:

            print(
                f"[fleet] Skipping invalid or duplicate vehicle "
                f"entry in {DATA_FILE.name}: {name or '(no name)'}"
            )

            continue

        seen.add(_key(name))

        vehicles[name] = Vehicle(
            vehicle_id=name,
            device_id=str(entry.get("device_id") or "").strip(),
            token=str(entry.get("token") or "").strip(),
            color=(str(entry.get("color")).strip() or None)
            if entry.get("color")
            else None,
            managed=entry.get("managed") is True
        )

    return vehicles


def _write_file(vehicles):
    """Write atomically, so a crash never leaves a half-written file."""

    global _loaded_signature

    DATA_FILE.parent.mkdir(
        parents=True,
        exist_ok=True
    )

    payload = {
        "vehicles": [
            vehicle.to_record()
            for vehicle in vehicles.values()
        ]
    }

    descriptor, temp_path = tempfile.mkstemp(
        dir=DATA_FILE.parent,
        prefix=".vehicles-",
        suffix=".tmp"
    )

    try:

        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:

            json.dump(payload, handle, indent=2)

            handle.write("\n")

        # OneDrive or an editor can hold the file for a moment
        # on Windows, which makes os.replace fail. Retry briefly.
        for attempt in range(5):

            try:

                os.replace(temp_path, DATA_FILE)

                break

            except PermissionError:

                if attempt == 4:
                    raise

                time.sleep(0.1)

    except BaseException:

        try:
            os.unlink(temp_path)
        except OSError:
            pass

        raise

    VEHICLES.clear()

    VEHICLES.update(vehicles)

    _loaded_signature = _file_signature()


def _legacy_env_vehicles():
    """Vehicles defined the old way, as Vehicle_NN_* in .env."""

    raw = {}

    for key, value in os.environ.items():

        match = _LEGACY_ENV_PATTERN.match(key)

        if match:

            raw.setdefault(
                int(match.group(1)),
                {}
            )[match.group(2).lower()] = value.strip()

    vehicles = {}

    for position, number in enumerate(sorted(raw)):

        fields = raw[number]

        name = f"Vehicle_{number:02d}"

        default_color = (
            _LEGACY_COLORS[position]
            if position < len(_LEGACY_COLORS)
            else None
        )

        vehicles[name] = Vehicle(
            vehicle_id=name,
            device_id=fields.get("id", ""),
            token=fields.get("token", ""),
            color=fields.get("color") or default_color
        )

    return vehicles


def _bootstrap():
    """Create vehicles.json on first run, importing any legacy .env vehicles."""

    vehicles = _legacy_env_vehicles()

    _write_file(vehicles)

    if vehicles:

        print(
            f"[fleet] Created {DATA_FILE} with "
            f"{len(vehicles)} vehicle(s) imported from .env: "
            f"{', '.join(vehicles)}. "
            "Remove the Vehicle_NN_ID / Vehicle_NN_TOKEN lines "
            "from .env; they are no longer read."
        )

    else:

        print(
            f"[fleet] Created empty {DATA_FILE}. "
            "Add vehicles from the dashboard's Vehicle Manager."
        )


def _refresh():
    """Load the registry, or reload it if the file changed on disk."""

    global _loaded_signature, _load_error

    with _lock:

        signature = _file_signature()

        if signature is None:

            _bootstrap()

            return

        if signature == _loaded_signature:
            return

        try:

            loaded = _read_file()

        except (OSError, ValueError) as exc:

            # Keep the last good registry, e.g. while the file
            # is being edited by hand. Retry when it changes.
            _load_error = f"{DATA_FILE.name} could not be read: {exc}"

            _loaded_signature = signature

            print(f"[fleet] {_load_error}")

            return

        _load_error = None

        VEHICLES.clear()

        VEHICLES.update(loaded)

        _loaded_signature = signature


# =========================================================
# LOOKUP
# =========================================================

def vehicle_ids():

    _refresh()

    with _lock:
        return list(VEHICLES)


def resolve(value):
    """Return the canonical vehicle ID for any accepted alias, or None.

    Accepted aliases (case-insensitive, spaces/hyphens == '_'):
        the vehicle name    "Vehicle_01", "vehicle 01"
        its trailing number "01", "1"  (when only one vehicle has it)
        its colour          "yellow"   (when only one vehicle has it)
    """

    if value is None:
        return None

    text = _key(value)

    if not text:
        return None

    _refresh()

    with _lock:

        for vehicle_id in VEHICLES:

            if text == _key(vehicle_id):
                return vehicle_id

        by_color = [
            vehicle_id
            for vehicle_id, vehicle in VEHICLES.items()
            if vehicle.color and text == _key(vehicle.color)
        ]

        if len(by_color) == 1:
            return by_color[0]

        match = _NUMBER_ALIAS_PATTERN.match(text)

        if match:

            number = int(match.group(1))

            by_number = [
                vehicle_id
                for vehicle_id, vehicle in VEHICLES.items()
                if vehicle.number == number
            ]

            if len(by_number) == 1:
                return by_number[0]

    return None


def get(value):

    vehicle_id = resolve(value)

    if vehicle_id is None:

        raise ValueError(
            f"Unknown vehicle: {value}. "
            f"Registered vehicles: {describe()}"
        )

    with _lock:
        return VEHICLES[vehicle_id]


def describe():

    return ", ".join(vehicle_ids()) or "none"


def fleet_size():

    return len(vehicle_ids())


def device_id(value):

    return get(value).device_id


def token(value):

    return get(value).token


def display_name(value):

    return get(value).name


def short_name(value):

    return get(value).short_name


def list_vehicles():
    """Public (token-masked) records, in fleet order."""

    _refresh()

    with _lock:

        return [
            vehicle.to_public()
            for vehicle in VEHICLES.values()
        ]


# =========================================================
# ORDERING
# =========================================================

def next_vehicle(value):
    """Vehicle that receives the handover after `value`.

    Wraps to the first vehicle, matching the 2-vehicle behaviour.
    """

    ids = vehicle_ids()

    vehicle_id = get(value).vehicle_id

    index = ids.index(vehicle_id)

    return ids[(index + 1) % len(ids)]


def previous_vehicle(value):
    """Vehicle that must finish before `value` may start, or None."""

    ids = vehicle_ids()

    vehicle_id = get(value).vehicle_id

    index = ids.index(vehicle_id)

    if index == 0:
        return None

    return ids[index - 1]


# =========================================================
# CHANGES (POST / PUT / DELETE /api/vehicles)
# =========================================================

_UNSET = object()


def _clean_name(value):

    name = str(value or "").strip()

    if not NAME_PATTERN.match(name):

        raise FleetError(
            f"Vehicle name must be {NAME_RULE}.",
            "INVALID_VEHICLE_NAME"
        )

    return name


def _clean_device_id(value):

    text = str(value or "").strip()

    if not text:

        raise FleetError(
            "ThingsBoard device ID is required.",
            "MISSING_DEVICE_ID"
        )

    if not DEVICE_ID_PATTERN.match(text):

        raise FleetError(
            "Device ID may contain only letters, digits and '-' "
            "(a ThingsBoard device UUID).",
            "INVALID_DEVICE_ID"
        )

    return text


def _clean_token(value):

    text = str(value or "").strip()

    if not text:

        raise FleetError(
            "ThingsBoard access token is required.",
            "MISSING_TOKEN"
        )

    if not TOKEN_PATTERN.match(text):

        raise FleetError(
            "Access token must be 1-128 characters with no spaces, "
            "'/', '?' or '#'.",
            "INVALID_TOKEN"
        )

    return text


def _clean_color(value):

    text = str(value or "").strip()

    if not text:
        return None

    if not COLOR_PATTERN.match(text):

        raise FleetError(
            "Colour must be a colour name (e.g. green) "
            "or a hex value (e.g. #22c55e).",
            "INVALID_COLOR"
        )

    return text.lower()


def _check_unique(vehicles, candidate, ignore=None):

    for vehicle_id, vehicle in vehicles.items():

        if vehicle_id == ignore:
            continue

        if _key(vehicle_id) == _key(candidate.vehicle_id):

            raise FleetError(
                f"A vehicle named {vehicle_id} already exists.",
                "DUPLICATE_VEHICLE",
                409
            )

        if vehicle.device_id == candidate.device_id:

            raise FleetError(
                f"Device ID is already used by {vehicle_id}.",
                "DUPLICATE_DEVICE_ID",
                409
            )

        if vehicle.token == candidate.token:

            raise FleetError(
                f"Access token is already used by {vehicle_id}.",
                "DUPLICATE_TOKEN",
                409
            )


def _require(value):

    vehicle_id = resolve(value)

    # Changes address a vehicle by its exact name, not an alias,
    # so DELETE /api/vehicles/1 cannot remove the wrong vehicle.
    if vehicle_id is None or _key(vehicle_id) != _key(value):

        raise FleetError(
            f"Vehicle {value} not found.",
            "VEHICLE_NOT_FOUND",
            404
        )

    return vehicle_id


def require(value):
    """The vehicle with exactly this name (no aliases), or FleetError 404.

    Used before irreversible ThingsBoard changes, so e.g.
    "1" can never delete the device of Vehicle_01.
    """

    with _lock:

        _refresh()

        return VEHICLES[_require(value)]


def check_new_vehicle(name, color=None):
    """Validate a name/colour for a new vehicle without saving.

    Returns (clean_name, clean_color). Used before creating a
    ThingsBoard device, so a bad name never creates one.
    """

    clean_name = _clean_name(name)

    clean_color = _clean_color(color)

    with _lock:

        _refresh()

        for vehicle_id in VEHICLES:

            if _key(vehicle_id) == _key(clean_name):

                raise FleetError(
                    f"A vehicle named {vehicle_id} already exists.",
                    "DUPLICATE_VEHICLE",
                    409
                )

    return clean_name, clean_color


def check_rename(current, new_name):
    """Validate renaming `current` to `new_name` without saving.

    Returns the clean new name.
    """

    clean_name = _clean_name(new_name)

    with _lock:

        _refresh()

        old_id = _require(current)

        for vehicle_id in VEHICLES:

            if (
                vehicle_id != old_id
                and _key(vehicle_id) == _key(clean_name)
            ):

                raise FleetError(
                    f"A vehicle named {vehicle_id} already exists.",
                    "DUPLICATE_VEHICLE",
                    409
                )

    return clean_name


def add_vehicle(name, device_id, token, color=None, managed=False):
    """Append a vehicle to vehicles.json and return it."""

    candidate = Vehicle(
        vehicle_id=_clean_name(name),
        device_id=_clean_device_id(device_id),
        token=_clean_token(token),
        color=_clean_color(color),
        managed=bool(managed)
    )

    with _lock:

        _refresh()

        _check_unique(VEHICLES, candidate)

        vehicles = dict(VEHICLES)

        vehicles[candidate.vehicle_id] = candidate

        _write_file(vehicles)

    return candidate


def update_vehicle(current, name=_UNSET, device_id=_UNSET, token=_UNSET, color=_UNSET):
    """Change a vehicle in place (keeping its fleet position).

    Omitted fields are kept. A blank token keeps the current
    token. A blank colour removes the colour.

    Returns (old_vehicle_id, updated_vehicle).
    """

    with _lock:

        _refresh()

        old_id = _require(current)

        existing = VEHICLES[old_id]

        updated = Vehicle(
            vehicle_id=(
                existing.vehicle_id
                if name is _UNSET
                else _clean_name(name)
            ),
            device_id=(
                existing.device_id
                if device_id is _UNSET
                else _clean_device_id(device_id)
            ),
            token=(
                existing.token
                if token is _UNSET or not str(token or "").strip()
                else _clean_token(token)
            ),
            color=(
                existing.color
                if color is _UNSET
                else _clean_color(color)
            ),
            managed=existing.managed
        )

        # The device of an auto-created vehicle belongs to it;
        # re-pointing it would orphan that device.
        if existing.managed and (
            updated.device_id != existing.device_id
            or updated.token != existing.token
        ):

            raise FleetError(
                f"{old_id} uses a device created by this dashboard; its "
                "device ID and token cannot be changed. Delete the vehicle "
                "and add it again to use another device.",
                "MANAGED_DEVICE_LOCKED",
                400
            )

        _check_unique(VEHICLES, updated, ignore=old_id)

        # Rebuild in the same order so a rename keeps its position.
        vehicles = {
            (updated.vehicle_id if vehicle_id == old_id else vehicle_id):
                (updated if vehicle_id == old_id else vehicle)
            for vehicle_id, vehicle in VEHICLES.items()
        }

        _write_file(vehicles)

    return old_id, updated


def delete_vehicle(value, backup=False, expected_device_id=None):
    """Remove a vehicle from vehicles.json and return it.

    backup=True first copies the file to vehicles.json.bak
    (used by automatic removal, so it can be undone).

    expected_device_id: only delete if the vehicle still has
    this device ID, so a vehicle re-pointed to another device
    meanwhile is not removed.
    """

    with _lock:

        _refresh()

        vehicle_id = _require(value)

        removed = VEHICLES[vehicle_id]

        if (
            expected_device_id is not None
            and removed.device_id != expected_device_id
        ):

            raise FleetError(
                f"{vehicle_id} now uses a different device; not removed.",
                "DEVICE_CHANGED",
                409
            )

        if backup and DATA_FILE.exists():

            shutil.copyfile(
                DATA_FILE,
                DATA_FILE.with_name(DATA_FILE.name + ".bak")
            )

        vehicles = {
            other_id: vehicle
            for other_id, vehicle in VEHICLES.items()
            if other_id != vehicle_id
        }

        _write_file(vehicles)

    return removed


# =========================================================
# VALIDATION
# =========================================================

def validate():
    """Return a list of registry problems (empty when valid)."""

    _refresh()

    with _lock:

        problems = []

        if _load_error:
            problems.append(_load_error)

        for vehicle in VEHICLES.values():

            if not vehicle.device_id:
                problems.append(f"{vehicle.vehicle_id}: missing device_id")

            if not vehicle.token:
                problems.append(f"{vehicle.vehicle_id}: missing token")

        return problems
