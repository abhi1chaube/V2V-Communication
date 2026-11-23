# Fleet Refactor: Fixed Two-Vehicle Design -> Dynamic Registry

Written for: backend developers who will maintain or extend the V2V fleet code.

## 1. Architectural changes

1. **Central registry.** `services/fleet.py` is the single source of truth for vehicles. It reads `Vehicle_NN_ID`, `Vehicle_NN_TOKEN`, and optional `Vehicle_NN_NAME` / `Vehicle_NN_COLOR` from `.env`.
2. **Canonical vehicle IDs.** Every internal key is `Vehicle_01`, `Vehicle_02`, ... (no leading space, no lowercase). Callers may pass any alias: `Vehicle_01`, `vehicle 01`, `01`, `1`, or the dashboard colour `yellow`.
3. **Generic functions replace per-vehicle functions.**

   | Old | New |
   |---|---|
   | `get_Vehicle_01_telemetry()` / `get_Vehicle_02_telemetry()` | `thingsboard.get_vehicle_telemetry(vehicle_id)` |
   | `get_Vehicle_01_state()` / `get_Vehicle_02_state()` | `thingsboard.get_vehicle_state(vehicle_id)` (already existed) |
   | `Vehicle_01_completed()` | `vehicle_simulator.vehicle_completed(vehicle_id)` |
   | `wait_for_Vehicle_01()` | `vehicle_simulator.wait_for_predecessor(vehicle_id)` |
   | `set_Vehicle_02_ready()`, `load_Vehicle_02()`, `run_Vehicle_02_cycle()`, ... | `set_vehicle_ready(vid)`, `load_vehicle(vid)`, `run_vehicle_cycle(vid)`, ... |
   | `send_telemetry` copy per vehicle (in `send_yellow.py` and `send_blue.py`) | `vehicle_simulator.send_status(vehicle_id, ...)` |

4. **Ordering comes from the registry.** `fleet.previous_vehicle()` and `fleet.next_vehicle()` replace the hard-coded `"Vehicle_01" if ... else "Vehicle_02"` checks. Order is numeric. Handover wraps to the first vehicle, which keeps the 2-vehicle behaviour.
5. **One simulator for all vehicles.** `vehicle_simulator.py` runs any registered vehicle. `send_yellow.py` and `send_blue.py` are now thin wrappers, so existing commands still work.
6. **Dashboard compatibility layer.** `/api/vehicles` returns the vehicles under their IDs and also under their colour keys (`yellow`, `blue`), which is what `FRONTEND/script.js` reads.

## 2. Adding a vehicle

> **Superseded.** Vehicles are now stored in `BACKEND/data/vehicles.json`, not `.env`.
> Add, edit and delete them from the dashboard's **Vehicle Manager**, or through
> `GET/POST /api/vehicles` and `PUT/DELETE /api/vehicles/<name>`. `.env` holds only
> `THINGSBOARD_URL`, `THINGSBOARD_USERNAME` and `THINGSBOARD_PASSWORD`. On first start,
> if `vehicles.json` does not exist, any old `Vehicle_NN_*` keys in `.env` are imported
> into it once. The text below describes the old `.env` method.

Old method — add to `BACKEND/.env`:

```
Vehicle_03_ID=<ThingsBoard device id>
Vehicle_03_TOKEN=<ThingsBoard device access token>
# optional
Vehicle_03_NAME=Vehicle 03
Vehicle_03_COLOR=green
```

Then:

- `python vehicle_simulator.py Vehicle_03` runs its simulator.
- `/api/*` endpoints accept `Vehicle_03`, `vehicle 03`, `03`, or `3`.
- Safety, events, state, and ThingsBoard sync include it automatically.
- `fleet.validate()` reports any missing `_ID` or `_TOKEN` by name. The ThingsBoard login check raises an error that names them.

No Python changes are needed.

**Limit:** `FRONTEND/script.js` has two fixed dashboard cards. The backend supports N vehicles, but the dashboard only renders `yellow` and `blue`. Adding a card is a frontend change, which this refactor does not make.

---

## 3. File-by-file changes

### 3.1 `services/fleet.py` (NEW)

**Why:** Central registry, so no other file needs a vehicle list.

**Old:** None.

**New (key parts):**

```python
_ENV_PATTERN = re.compile(r"^vehicle_(\d+)_(id|token|name|color)$", re.IGNORECASE)

def resolve(value):
    """Return canonical vehicle ID for any alias, or None."""
    text = re.sub(r"[\s\-]+", "_", str(value).strip().lower())
    for vehicle_id, vehicle in VEHICLES.items():
        if text == vehicle_id.lower() or (vehicle.color and text == vehicle.color):
            return vehicle_id
    match = _ALIAS_NUMBER_PATTERN.match(text)   # "01", "1", "vehicle_03"
    ...
```

**Risks:**
- Environment variable names are upper-cased on Windows. The regex is case-insensitive for this reason.
- Vehicle numbers must be contiguous only in the sense that order is numeric. Gaps (for example `Vehicle_01` and `Vehicle_05`) are allowed.
- Colours are assigned by position: the first two vehicles get `yellow` and `blue`. Any further vehicle has no colour unless `_COLOR` is set.

---

### 3.2 `services/thingsboard.py`

**Why:** ID and token variables were per vehicle, and `validate_config()` had a typo (`VEHICLE_01_TOKEN`, which is not the same env var name).

**Old:**

```python
Vehicle_01_ID = os.getenv("Vehicle_01_ID")
Vehicle_02_ID = os.getenv("Vehicle_02_ID")
Vehicle_01_TOKEN = os.getenv("Vehicle_01_TOKEN")
Vehicle_02_TOKEN = os.getenv("Vehicle_02_TOKEN")

def get_device_id(vehicle):
    vehicle = str(vehicle).lower().strip()
    if vehicle in ("Vehicle_01", "vehicle_01", "vehicle 01", "01"):
        return Vehicle_01_ID
    ...
```

**New:**

```python
from . import fleet

def get_device_id(vehicle):
    return fleet.device_id(vehicle)

def get_device_token(vehicle):
    return fleet.token(vehicle)

def get_vehicle_telemetry(vehicle, keys=None):
    return get_device_telemetry(get_device_id(vehicle), keys)

def send_vehicle_telemetry(vehicle, telemetry):
    vehicle_id = fleet.resolve(vehicle)
    device_token = fleet.token(vehicle_id) if vehicle_id is not None else vehicle
    return send_telemetry(device_token, telemetry)
```

`validate_config()` now requires the two ThingsBoard credentials, plus `fleet.validate()`.

**Removed:** `get_Vehicle_01_state()`, `get_Vehicle_02_state()`. Nothing in the backend called them.

**Risks:**
- `send_vehicle_telemetry` still accepts a raw device token, for backward compatibility. A token that happens to equal an alias would be misread. This is very unlikely, since tokens are random strings.
- `validate_config()` runs at each ThingsBoard login. It now fails if any registered vehicle lacks an ID or token, which matches the old behaviour for the two fixed vehicles. Because it runs at login rather than at startup, a missing token shows up on the first ThingsBoard call, not when the server starts.

---

### 3.3 `services/state.py`

**Why:** Keys had a leading space (`" Vehicle_01"`) and lookups lower-cased the input, so they could never match. Every state call failed.

**Old:**

```python
vehicles = {
    " Vehicle_01": {**deepcopy(_default_vehicle), "name": "Vehicle 01", "color": "yellow"},
    " Vehicle_02": {**deepcopy(_default_vehicle), "name": "Vehicle 02", "color": "blue"},
}

def get_vehicle_state(vehicle):
    vehicle = str(vehicle).lower().strip()
    if vehicle not in vehicles:
        raise ValueError("Vehicle must be yellow or blue.")
    ...
```

(The same 4-line check was repeated in `update_vehicle_state` and `reset_vehicle`.)

**New:**

```python
from . import fleet

vehicles = {
    vehicle_id: {
        **deepcopy(_default_vehicle),
        "name": registered.name,
        "color": registered.color or ""
    }
    for vehicle_id, registered in fleet.VEHICLES.items()
}

def _key(vehicle):
    vehicle_id = fleet.resolve(vehicle)
    if vehicle_id not in vehicles:
        raise ValueError(f"Unknown vehicle: {vehicle}. Registered vehicles: {fleet.describe()}")
    return vehicle_id

def get_vehicle_state(vehicle):
    vehicle = _key(vehicle)
    ...

def reset_cycle():
    return {vehicle_id: reset_vehicle(vehicle_id) for vehicle_id in vehicles}
```

**Risks:**
- Error text changed from "Vehicle must be yellow or blue." to a message listing the registered vehicles.
- `vehicles` is still a module-level dict, so code that reads `state.vehicles` directly gets the new keys.

---

### 3.4 `services/safety.py`

**Old:**

```python
def get_safety_summary():
    return {
        " Vehicle_01": get_sensor_safety(" Vehicle_01"),
        " Vehicle_02": get_sensor_safety(" Vehicle_02"),
    }
```

**New:**

```python
from . import fleet

def get_safety_summary():
    return {
        vehicle_id: get_sensor_safety(vehicle_id)
        for vehicle_id in fleet.vehicle_ids()
    }
```

**Risks:** Same leading-space key problem as state. Fixed. The `/api/safety` response keys change from `" Vehicle_01"` to `"Vehicle_01"`. Frontend does not read this endpoint.

---

### 3.5 `services/events.py`

**Why:**
- `vehicle_name()` compared against `" Vehicle_01"` (leading space), so it returned `"VEHICLE_01"` instead of `"V01"`.
- `get_vehicle_events()` lower-cased the filter, but events were stored with the original case, so the filter never matched.

**Old:**

```python
def vehicle_name(vehicle):
    vehicle = str(vehicle).lower().strip()
    if vehicle == " Vehicle_01":
        return "V01"
    if vehicle == "Vehicle_02":
        return "V02"
    return vehicle.upper()
```

**New:**

```python
from . import fleet

def vehicle_name(vehicle):
    vehicle_id = fleet.resolve(vehicle)
    if vehicle_id is None:
        return str(vehicle).upper()
    return fleet.short_name(vehicle_id)

def add_event(...):
    ...
    vehicle = fleet.resolve(vehicle) or vehicle   # store canonical ID

def get_vehicle_events(vehicle, limit=50):
    vehicle = fleet.resolve(vehicle) or str(vehicle).strip()
    ...
```

**Risks:** Events stored before this change (in memory only, so none after restart) would not match. The event log is in memory, so there is no stored data to migrate.

---

### 3.6 `app.py`

**Why:** This file had most of the fixed-vehicle logic: literal tuples repeated in about 12 places, a `"Vehicle_01" or "Vehicle_01"` typo in two places, a `("Vehicle_01", "Vehicle_01")` loop in emergency stop, and handover logic hard-coded to two vehicles.

**Old (request parsing, used by every endpoint):**

```python
vehicle = str(data.get("vehicle", "")).lower().strip()
if vehicle not in ("Vehicle_01", "Vehicle_02"):
    return None, data
return vehicle, data
```

**New:**

```python
from services import fleet
...
vehicle = fleet.resolve(data.get("vehicle"))
if vehicle is None:
    return None, data
return vehicle, data
```

**Changes, all in `app.py`:**

| Location | Old | New |
|---|---|---|
| `get_vehicle_from_request` | lower-cased tuple check, which rejected `"yellow"` and `"blue"` | `fleet.resolve(...)` |
| `perform_handover` | `next_vehicle = "Vehicle_02" if completed_vehicle == "Vehicle_01" else "Vehicle_01"` | `fleet.next_vehicle(completed_vehicle)` |
| `sync_all_to_thingsboard` | `for vehicle in ("Vehicle_01", "Vehicle_02")` | `for vehicle in fleet.vehicle_ids()` |
| `refresh_from_thingsboard` | same tuple loop, returned `Vehicle_01` / `Vehicle_02` keys only | iterates `fleet.vehicle_ids()`, and also adds colour keys (`yellow`, `blue`) |
| `get_vehicle` (`GET /api/vehicles/<v>`) | tuple check | `fleet.resolve` |
| `"Provide vehicle as 'Vehicle_01' or 'Vehicle_02'."` (7 places) | literal | `f"Provide vehicle as one of: {fleet.describe()}."` |
| `"Provide vehicle as 'Vehicle_01' or 'Vehicle_01'."` (2 places) | typo | same f-string |
| `get_vehicle_safety`, `get_vehicle_events` | tuple check | `fleet.resolve` |
| `complete_journey` | `next_vehicle = ("Vehicle_02" if vehicle == "Vehicle_01" else "Vehicle_01")` | `fleet.next_vehicle(vehicle)` |
| `emergency_stop` | `for stopped_vehicle in ("Vehicle_01", "Vehicle_01")` (typo: vehicle 02 never stopped) | `for stopped_vehicle in fleet.vehicle_ids()` |
| `communication` | literal lists for `vehicles` and `vehicle_statuses` | built from `fleet.vehicle_ids()` |

**Refresh example (the dashboard fix):**

```python
# Dashboard compatibility: the frontend reads data.yellow and data.blue.
for vehicle_id in fleet.vehicle_ids():
    color = fleet.get(vehicle_id).color
    if color:
        result[color] = result[vehicle_id]
```

**Risks:**
- **Behaviour change, intended:** the frontend sends `"yellow"` and `"blue"`. The old backend rejected those, so commands from the dashboard failed with `INVALID_VEHICLE`. They are now accepted.
- **Response change, intended:** `/api/vehicles` now includes `yellow` and `blue` keys, which the dashboard reads. The `Vehicle_01` and `Vehicle_02` keys are still present, so anything reading those keeps working. The data is duplicated in the response.
- `/api/emergency-stop` now stops every registered vehicle. Previously it stopped only `Vehicle_01`, because of the typo. The loop also calls `hardware.activate_emergency_stop()` once per vehicle, as before.

---

### 3.7 `vehicle_simulator.py` (NEW)

**Why:** `send_yellow.py` and `send_blue.py` duplicated the same telemetry, journey, and ThingsBoard code. Each had its own JWT handling, and Blue duplicated the Yellow completion check with the names changed.

**Old:** Two files of about 650 and 1230 lines, with per-vehicle functions (`run_Vehicle_01_cycle`, `run_Vehicle_02_cycle`, `Vehicle_01_completed`, `wait_for_Vehicle_01`, ...).

**New:** One generic runner.

```python
def run_vehicle_cycle(vehicle_id):
    vehicle_id = fleet.get(vehicle_id).vehicle_id
    predecessor = fleet.previous_vehicle(vehicle_id)

    initialize_vehicle(vehicle_id)          # "waiting" if it has a predecessor
    time.sleep(2)
    if predecessor is not None:
        wait_for_predecessor(vehicle_id)    # polls vehicle_completed(predecessor)
        time.sleep(2)
        set_vehicle_ready(vehicle_id)
        time.sleep(2)
    move_to_loading_point(vehicle_id)
    time.sleep(2)
    load_vehicle(vehicle_id)
    time.sleep(2)
    move_to_warehouse(vehicle_id)
```

Sending goes through `thingsboard.send_telemetry`, so the simulator no longer has its own JWT or HTTP code.

**Behaviour differences from the old scripts:**
- Initial telemetry for a waiting vehicle now uses `qr_status="not_detected"` and `next_action="WAIT_FOR_HANDOVER"`, as Blue did. Previously Blue used `not_detected`, and Yellow never waited.
- The completion check uses `thingsboard.get_load_percentage`. The old code had the same clamp-to-zero behaviour, so the result is the same.
- Telemetry field names and values are otherwise the same. `vehicles_online` now equals the number of registered vehicles (it was hard-coded to `2`).

**Risks:**
- Simulator output and telemetry field names are the same as before, but this has not been run against ThingsBoard. It was only compiled and imported (see section 4).

---

### 3.8 `send_yellow.py` and `send_blue.py`

**Old:** About 670 and 1240 lines of copied logic.

**New:**

```python
from vehicle_simulator import run_vehicle_cycle

if __name__ == "__main__":
    run_vehicle_cycle("Vehicle_01")   # send_blue.py uses "Vehicle_02"
```

**Risks:** Both wrappers still name a specific vehicle. Running `python vehicle_simulator.py <vehicle>` is the generic route, and the wrappers can be deleted once nothing calls them.

---

## 4. Verification performed

- `py_compile` passes for every changed file.
- Registry tests, run with the existing `.env` and with a third vehicle added through environment variables only:
  - aliases (`yellow`, `01`, `vehicle 01`, `BLUE`, ...) resolve correctly
  - `previous_vehicle` and `next_vehicle` are correct, with wrap-around
  - a missing `Vehicle_03_TOKEN` is reported by name
  - `state`, `safety`, `events` and `reset_cycle` all include `Vehicle_03`
- Flask test-client checks, with ThingsBoard stubbed out (no network):
  - `/api/start` with an unknown vehicle returns 400 `INVALID_VEHICLE`
  - `/api/sensors/radar` accepts `"yellow"` and `"Vehicle_02"`
  - `/api/safety`, `/api/communication`, `/api/events/yellow`, `/api/safety/blue` return 200
  - `/api/vehicles` includes both `yellow` / `blue` and `Vehicle_01` / `Vehicle_02` keys
- **Not verified:** a full run against the live ThingsBoard instance or the real hardware. Those need your environment.

## 5. Pre-existing issues found and NOT changed

These are outside the refactor. Fix them separately.

1. **`app.py` line ~33 shadows the hardware module.** `hardware = HardwareController()` replaces the module, but `/api/emergency-stop` calls `hardware.activate_emergency_stop()`, which is a module-level function. The endpoint returns `'HardwareController' object has no attribute 'activate_emergency_stop'`.
2. **`/api/j2735/bsm` (`get_bsm`)** still contains the literal `" Vehicle_02"` and has no `return`, so the route returns nothing. It is dead code. Left as is.
3. **`thingsboard.get_device_telemetry`** sends the same request twice on every call (the second `requests.get` is unconditional). It doubles ThingsBoard traffic. Left as is to keep the integration unchanged.
4. **`app_backup.py`** still uses `"yellow"` / `"blue"` strings. Nothing imports it.
5. **`BACKEND/.env` holds ThingsBoard tokens in plain text.** Do not commit it or include it in shared zips such as `V2V COMM-before-major-changes.zip`.
6. **Frontend has two fixed cards.** Supporting more vehicles visually needs changes to `FRONTEND/index.html` and `FRONTEND/script.js`.

> **Update:** items 1–3 and 6 have since been fixed (hardware module no longer shadowed; `/api/j2735/bsm` is generic and returns a BSM, see section 6; the duplicate telemetry request is gone; the frontend renders any number of vehicles).

## 6. J2735-styled messages in the Activity Log

`services/j2735.py` builds **J2735-styled JSON**. It is *not* a conformant J2735 message: nothing is ASN.1 UPER-encoded, signed, or sent over DSRC / C-V2X. It uses J2735 names, units and enumerations so the log reads like V2X traffic.

- **When:** a `BasicSafetyMessage` (messageId 20) is logged when a vehicle's safety state changes. That covers speed, transmission, brakes, emergency flags, stop, mission and radar. Load-percentage steps alone do not log one. The hook is `broadcast_bsm()` in `app.py`, called from `sync_vehicle_to_thingsboard()`, so every state-changing command passes through it. A failure there never breaks the command.
- **Not covered:** state changes that arrive only from ThingsBoard polling (e.g. `vehicle_simulator.py` running on its own) do not produce BSMs. Only commands through this backend do.
- **Mapping:**

  | Field | Rule |
  |---|---|
  | `msgCnt` | Per-vehicle counter, 0–127, wraps |
  | `id` | Stable 4-octet TemporaryID from the vehicle name. Real IDs rotate. |
  | `secMark` | UTC milliseconds within the minute |
  | `speed` | km/h converted to 0.02 m/s units |
  | `transmission` | Derived from `motor_direction` |
  | `brakes.wheelBrakes` | Derived from `braking_status` |
  | partII `events` | `emergency_stop` sets `eventHazardLights` + `eventHardBraking` |

- **No sensor, so "unavailable" codes:** the vehicles have no GPS, heading, acceleration or steering sensor. Those fields carry J2735's "unavailable" codes: lat 900000001, long 1800000001, elev -4096, heading 28800, angle 127, accel 2001 / -127, accuracy 255/255/65535. Vehicle size is not configured (0).
- **Non-J2735 fields** are prefixed `x_` (`x_vehicle`, `x_extensions`, `x_format`).
- **V2V handover** has no J2735 equivalent. It is logged as a custom `V2V-HANDOVER` message, labelled as such.
- **Dashboard:** BSM and V2V rows show a chip and expand to the JSON on click. `GET /api/j2735/bsm?vehicle=<name>` returns the same JSON without advancing `msgCnt`.

## 7. Move from Flask to FastAPI

The backend now runs on FastAPI (ASGI, served by uvicorn) instead of Flask. **The HTTP API is unchanged:**
- same paths, methods and port (5000)
- same status codes and `{success, message, code, data}` envelope
- same CORS policy

The dashboard did not change.

**Why:** the planned upgrade (SSE live stream, backend autopilot loop, message bus, React + TypeScript frontend) needs long-lived streams, a startup/shutdown lifecycle and a typed API schema. FastAPI provides all three natively. The switch is not a speed improvement; the slow part is ThingsBoard, not the framework.

### Layout

| File | Lines | Contents |
|---|---|---|
| `app.py` | 260 | App setup: CORS, error envelope handlers, lifespan, API docs, router registration, `python app.py` entry point |
| `api/common.py` | 263 | `success()` / `error()` envelope, `ApiError`, `json_body` (lenient body parsing), `require_local`, `parse_bool`, `fleet_error`, `API_VERSION` |
| `api/sync.py` | 337 | Local state ↔ ThingsBoard sync, parallel refresh pool, missing-device auto-removal, BSM hook, handover. Moved unchanged from the Flask `app.py`. |
| `api/routes_root.py` | 114 | `/`, `/api/health`, `/api/hardware`, `/api/servo`, `/api/vehicle/command` |
| `api/routes_vehicles.py` | 372 | Vehicle registry CRUD, `/api/thingsboard/*` |
| `api/routes_fleet.py` | 471 | `/api/fleet`, mission commands, manual control, sensor inputs |
| `api/routes_safety.py` | 275 | Emergency stop/release, reset, safety summaries |
| `api/routes_events.py` | 111 | Activity log, communication, J2735 BSM |
| `api/schemas.py` | 98 | Pydantic models that document request bodies in `/apidocs` |
| `services/*` | — | Unchanged. They never depended on Flask. |

### Mapping from Flask

| Flask | FastAPI |
|---|---|
| `request.get_json(silent=True) or {}` | `data: dict = Depends(json_body)`: same lenient behaviour, so each route still returns its own codes such as `INVALID_VEHICLE` and `INVALID_PERCENTAGE` |
| `@local_only` decorator | `dependencies=[Depends(require_local)]`; checks `request.client.host` against the same addresses and `ALLOW_REMOTE_FLEET_ADMIN` |
| flask-cors | `CORSMiddleware`. Same default: `null`, `file://`, and localhost/127.0.0.1/[::1] on any port. `CORS_ORIGINS` works as before. |
| flasgger YAML docstrings, `/apidocs/`, `/apispec.json` | Generated OpenAPI 3.1. Swagger UI at `/apidocs`, ReDoc at `/redoc`, schema at `/openapi.json` (also served at `/apispec.json`) |
| `@app.errorhandler(404/500)` | Exception handlers with the same bodies. Validation errors return **400** `VALIDATION_ERROR` in the envelope, not FastAPI's default 422. |
| `events.log_backend_started()` at import | `lifespan` startup. The ThingsBoard thread pool is shut down on exit. |
| `app.run(..., use_reloader=True)` | `uvicorn.run("app:app", ...)`. Auto-reload is **off** by default; set `DEV_RELOAD=true` to enable it. |

**Endpoints stay `def`, not `async def`.** The services make blocking `requests` calls and use `threading` locks. FastAPI runs `def` endpoints in a threadpool. Declaring them `async def` would block the event loop for every ThingsBoard call. Only code that genuinely awaits, such as the planned SSE stream, should be `async def`.

### Intentional change

`GET /` now reports `version: "2.1.0"`, defined once in `api/common.py` as `API_VERSION`. Flask reported `2.0.0` there and `2.1.0` in its Swagger spec.

### Running

```
cd BACKEND
venv\Scripts\python -m pip install -r requirements.txt
venv\Scripts\python app.py
```

Use the `venv` interpreter. The global Python on this machine has Flask, but not FastAPI. `requirments.txt` (misspelled, unpinned) was replaced by a pinned `requirements.txt` and `requirements-dev.txt`.

### Verification performed

- **Contract tests** (`tests/`, run with `venv\Scripts\python -m pytest tests -q`):
  - 84 requests cover every route and its main error paths: invalid vehicle, missing or invalid percentage, safety blocks, emergency latch, remote admin refused, unknown route, and add/edit/delete in auto and link mode.
  - The cases were first recorded from the Flask app into `tests/contract/golden_flask.json`. Recording twice gave identical output.
  - The test replays the cases against FastAPI. All 84 match; the only difference is the version noted above.
  - Vehicles are fake, ThingsBoard is faked, and real HTTP is blocked during the run. The real `data/vehicles.json` is never touched.
- **Live run in a browser,** with ThingsBoard deliberately unreachable and a throwaway registry:
  - `/apidocs` lists all 34 routes, each with a tag.
  - Through the existing dashboard: START → loading → handover → final warehouse → emergency stop → release → reset.
  - Vehicle Manager add (link mode) and delete.
  - Console errors: only the expected 502 from `/api/thingsboard/quota` while ThingsBoard is down.
- **CORS:** preflight and GET allowed for `null`, `file://` and localhost origins; a foreign origin is refused (400).
- **ThingsBoard down:** `/api/health` returns 200 with `thingsboard: offline`. Commands work, and responses say which ThingsBoard pushes failed.
- **Concurrency:** ThingsBoard was pointed at an address that never answers, then 20 parallel `GET /api/fleet` requests were sent. Flask and FastAPI behave the same: the requests finish after 30 s, because 40 reads share a 16-worker pool with a 10 s timeout. `GET /api/vehicles` still answered in about 15 ms meanwhile, so the event loop is not blocked.
- **Not verified:**
  - against the live ThingsBoard account
  - opening `index.html` straight from disk in a real browser; the CORS `null` origin was checked with direct requests instead
- **Removed:** `app_flask_legacy.py`, the copy of the Flask `app.py` used to record the golden file, was deleted after verification. `tests/contract/golden_flask.json` preserves its recorded behaviour.
