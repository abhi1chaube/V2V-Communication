# =========================================================
# STATE <-> THINGSBOARD SYNCHRONISATION
# =========================================================
#
# Who owns vehicle state is set by STATE_SOURCE:
#
#   backend (default)   The backend's state is the truth.
#       Commands change it; the world engine moves the
#       vehicles; ThingsBoard RECEIVES each change (push).
#       Reading a device from ThingsBoard is only a health
#       check (is the device reachable, was the last push
#       accepted); its telemetry is never applied to state.
#       Commands do not read ThingsBoard at all.
#
#   thingsboard_mirror  The old behaviour: before a command
#       and on every fleet poll, ThingsBoard telemetry is read
#       back into state, for an external device or the legacy
#       simulator writing to ThingsBoard.
#
# Nothing here depends on the web framework.
# =========================================================

import time
from concurrent.futures import ThreadPoolExecutor
from threading import Event, Lock, Thread

from services import events
from services import fleet
from services import j2735
from services import state
from services import thingsboard
from services import v2v
from services import world
from services.settings import settings
from services.timeutil import utc_iso


def backend_owns_state():

    return settings.state_source == "backend"


# =========================================================
# THINGSBOARD LINK HEALTH (PER VEHICLE)
# =========================================================

_links = {}

_links_lock = Lock()


def _record_link(vehicle_id, **values):

    with _links_lock:
        _links.setdefault(vehicle_id, {}).update(values)


def link_status(vehicle_id):
    """ThingsBoard health of one vehicle's device, as shown in API responses.

    reachable        last read of the device succeeded (None = never read)
    checked_at       when it was last read
    read_error       why the last read failed
    last_push_ok     the last state push was accepted (None = nothing pushed yet)
    last_push_at     when the last push was attempted
    last_push_error  why it failed
    """

    with _links_lock:
        current = dict(_links.get(vehicle_id, {}))

    return {
        "reachable": current.get("reachable"),
        "checked_at": current.get("checked_at"),
        "read_error": current.get("read_error"),
        "last_push_ok": current.get("last_push_ok"),
        "last_push_at": current.get("last_push_at"),
        "last_push_error": current.get("last_push_error"),
    }


def forget_link(*vehicle_ids):

    with _links_lock:
        for vehicle_id in vehicle_ids:
            _links.pop(vehicle_id, None)


def rename_link(old_id, new_id):

    with _links_lock:
        if old_id in _links:
            _links[new_id] = _links.pop(old_id)


# ThingsBoard reads run in parallel, so a poll of a large
# fleet takes about as long as the slowest vehicle.
THINGSBOARD_WORKERS = 16

thingsboard_pool = ThreadPoolExecutor(
    max_workers=THINGSBOARD_WORKERS,
    thread_name_prefix="thingsboard"
)

# A vehicle whose ThingsBoard device answers 404 on this many
# polls in a row, and is confirmed missing by a direct device
# lookup, is removed from data/vehicles.json automatically.
MISSING_DEVICE_THRESHOLD = 3

_missing_counts = {}

_missing_lock = Lock()

# Returned by refresh_vehicle_from_thingsboard() for a vehicle
# that was just auto-removed.
_REMOVED = object()


# =========================================================
# LOCAL STATE HELPERS
# =========================================================

def local_set_brake(vehicle, applied):
    """Compatibility adapter because state.py has no set_brake()."""
    if applied:
        return state.update_vehicle_state(
            vehicle,
            braking_status="applied",
            motor_direction="stopped",
            speed=0
        )
    return state.update_vehicle_state(
        vehicle,
        braking_status="not_applied"
    )


# (The handover is negotiated over the V2V bus: see
# services/v2v.py and POST /api/complete. The next vehicle's
# state changes through state.prepare_for_handover().)


def call_hardware(action):
    """Run a hardware call; any exception becomes {"success": False}."""
    try:
        result = action()
    except Exception as exc:
        return {"success": False, "error": str(exc)}
    return result if isinstance(result, dict) else {"success": bool(result)}


# =========================================================
# THINGSBOARD TELEMETRY CONVERSION
# =========================================================

def state_to_telemetry(vehicle_state):

    return {
        "vehicle_status": vehicle_state.get("status", "waiting"),
        "current_stop": vehicle_state.get(
            "current_stop",
            vehicle_state.get("location", "stop_1")
        ),
        "load": vehicle_state.get(
            "load",
            vehicle_state.get("load_percentage", 0) * 10
        ),
        "latitude": vehicle_state.get("latitude", 0.0),
        "longitude": vehicle_state.get("longitude", 0.0),
        "v2v_status": vehicle_state.get("v2v_status", "disconnected"),
        "handover_status": vehicle_state.get("handover_status", "idle"),
        "load_percentage": vehicle_state.get("load_percentage", 0),
        "loading_status": vehicle_state.get("loading_status", "idle"),
        "loading_progress": vehicle_state.get(
            "loading_progress",
            vehicle_state.get("load_percentage", 0)
        ),
        "braking_status": vehicle_state.get("braking_status", "applied"),
        "motor_direction": vehicle_state.get("motor_direction", "stopped"),
        "Apriltag_status": vehicle_state.get(
            "Apriltag_status",
            vehicle_state.get("qr_status", "not_detected")
        ),
        "radar_status": vehicle_state.get("radar_status", "clear"),
        "obstacle_detected": vehicle_state.get("obstacle_detected", False),
        "speed": vehicle_state.get("speed", 0),
        "emergency_stop": vehicle_state.get("emergency_stop", False),
        # Mirrored so the next ThingsBoard read returns the
        # mission set by the latest local command.
        "mission_status": vehicle_state.get("mission_status", "IDLE"),
    }


# =========================================================
# SYNC LOCAL STATE -> THINGSBOARD
# =========================================================

def broadcast_bsm(vehicle, current):
    """Log a J2735-styled BSM if the state changed. Never raises."""
    try:
        bsm = j2735.broadcast_if_changed(vehicle, current)
        if bsm is not None:
            events.log_bsm(vehicle, bsm)
            # ...and on the V2V radio, to the vehicles in range.
            v2v.bus.publish_bsm(vehicle, bsm)
    except Exception as exc:
        print(f"[j2735] BSM for {vehicle} skipped: {exc}")


def sync_vehicle_to_thingsboard(vehicle):
    """Push local state to ThingsBoard without breaking local control if TB is offline."""
    current = state.get_vehicle_state(vehicle)

    # Every state-changing command passes through here, so this
    # is where the J2735-styled BSM is logged (only when the
    # vehicle's safety state actually changed).
    broadcast_bsm(vehicle, current)

    return _push_telemetry(vehicle, current)


# The autopilot changes state on the world engine's thread, which
# must never wait for the cloud: its pushes go through this pool.
push_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="tb-push")


# =========================================================
# THINGSBOARD PUSH THROTTLING (UPGRADE PHASE 5)
# =========================================================
#
# The autopilot ticks every WORLD_TICK_MS (default 200 ms); pushing
# every change at that rate would hit ThingsBoard Cloud's per-device
# rate limit and the plan's message quota. So only the autopilot's
# own push path (this function) is throttled: at most one push per
# vehicle per TB_PUSH_MIN_INTERVAL_S (default 1 s), except a
# safety-relevant change (emergency stop, braking, radar/obstacle,
# V2V comm_hold), which always goes out at once. A push skipped by
# the window is COALESCED: the latest state is still sent, just
# after the window ends, by a small flusher thread, never dropped.
#
# REST commands (sync_vehicle_to_thingsboard, below) are untouched:
# they are discrete operator actions, not a 200 ms tick, so nothing
# here changes their push-every-time behaviour (and a large existing
# test suite depends on it).
# =========================================================

SAFETY_FIELDS = ("emergency_stop", "braking_status", "radar_status", "obstacle_detected", "comm_hold")

_push_lock = Lock()
_last_push_at = {}             # vehicle -> time.monotonic() of the last REAL push
_last_safety_fields = {}       # vehicle -> {field: value} last actually pushed
_pending = set()                # vehicles with a coalesced update waiting
_push_stats = {"pushed": 0, "coalesced": 0, "failed": 0}
_flusher_thread = None
_flusher_stop = Event()


def _safety_snapshot(current):
    return {key: current.get(key) for key in SAFETY_FIELDS}


def autopilot_sync(vehicle, radio=True):
    """After an autopilot transition: BSM on the radio now, ThingsBoard push
    throttled (immediately if due or safety-relevant, else coalesced).

    The BSM goes out inline (on the engine thread, so a seeded run
    stays repeatable); `radio=False` skips it (watchdog thread: the
    engine may be stuck).
    """

    current = state.get_vehicle_state(vehicle)

    if radio:
        broadcast_bsm(vehicle, current)

    safety_now = _safety_snapshot(current)
    now = time.monotonic()

    with _push_lock:
        last = _last_push_at.get(vehicle)
        urgent = _last_safety_fields.get(vehicle) != safety_now
        due = urgent or last is None or (now - last) >= settings.tb_push_min_interval_s
        if due:
            _last_push_at[vehicle] = now
            _last_safety_fields[vehicle] = safety_now
            _pending.discard(vehicle)
        else:
            _pending.add(vehicle)
            _push_stats["coalesced"] += 1

    if due:
        _submit_push(vehicle, current)
    else:
        _ensure_flusher()


def _submit_push(vehicle, current):

    try:
        push_pool.submit(_push_telemetry_counted, vehicle, current)
    except RuntimeError:
        pass                  # shutting down


def _push_telemetry_counted(vehicle, current):

    result = _push_telemetry(vehicle, current)

    with _push_lock:
        _push_stats["pushed" if isinstance(result, dict) and result.get("success") else "failed"] += 1

    return result


def _ensure_flusher():

    global _flusher_thread

    with _push_lock:
        if _flusher_thread is not None and _flusher_thread.is_alive():
            return
        _flusher_stop.clear()
        _flusher_thread = Thread(target=_flusher_run, name="tb-push-flusher", daemon=True)
        _flusher_thread.start()


def _flusher_run():

    while not _flusher_stop.wait(0.25):

        due = []
        now = time.monotonic()

        with _push_lock:
            for vehicle in list(_pending):
                last = _last_push_at.get(vehicle)
                if last is None or now - last >= settings.tb_push_min_interval_s:
                    due.append(vehicle)
            for vehicle in due:
                _pending.discard(vehicle)
                _last_push_at[vehicle] = now

        for vehicle in due:
            try:
                current = state.get_vehicle_state(vehicle)
            except ValueError:
                continue
            with _push_lock:
                _last_safety_fields[vehicle] = _safety_snapshot(current)
            _submit_push(vehicle, current)

        with _push_lock:
            if not _pending:
                return          # nothing left: ends the thread; _ensure_flusher restarts it on demand


def push_statistics():

    with _push_lock:
        return dict(_push_stats)


def reset_push_throttle():
    """Test isolation: forget every vehicle's throttle state and stop the flusher."""

    global _flusher_thread

    _flusher_stop.set()

    if _flusher_thread is not None:
        _flusher_thread.join(timeout=1)

    _flusher_thread = None

    with _push_lock:
        _last_push_at.clear()
        _last_safety_fields.clear()
        _pending.clear()
        _push_stats.update(pushed=0, coalesced=0, failed=0)


def _push_telemetry(vehicle, current):

    telemetry = state_to_telemetry(current)

    try:
        result = thingsboard.send_vehicle_telemetry(vehicle, telemetry)
    except Exception as exc:
        result = {
            "success": False,
            # Masked again here in case an error type other than
            # ThingsBoardError carried the token.
            "error": thingsboard.redact(exc, _token_or_none(vehicle)),
            "vehicle": vehicle
        }

    ok = isinstance(result, dict) and bool(result.get("success"))

    _record_link(
        vehicle,
        last_push_ok=ok,
        last_push_at=utc_iso(),
        last_push_error=None if ok else (result.get("error") if isinstance(result, dict) else "push failed"),
    )

    return result


def _token_or_none(vehicle):

    try:
        return fleet.token(vehicle)
    except Exception:
        return None


def sync_failures(results):
    """Vehicles whose ThingsBoard sync did not succeed."""
    return sorted(
        vehicle_id
        for vehicle_id, result in results.items()
        if not (isinstance(result, dict) and result.get("success"))
    )


def telemetry_report(results_by_vehicle):
    """Fields added to every command response: did the ThingsBoard push work?

    `results_by_vehicle` is {vehicle_id: sync_vehicle_to_thingsboard() result}.
    Returns {"telemetry_synced": True}, or
    {"telemetry_synced": False, "telemetry_error": "<vehicle>: <reason>; ..."}.

    A failed push never fails the command (local control keeps
    working while the cloud is down); it is only reported.
    """
    failed = sync_failures(results_by_vehicle)

    if not failed:
        return {"telemetry_synced": True}

    reasons = []

    for vehicle_id in failed:

        result = results_by_vehicle[vehicle_id]

        reason = (
            result.get("error") if isinstance(result, dict) else None
        ) or "ThingsBoard push failed."

        reasons.append(f"{vehicle_id}: {reason}")

    return {
        "telemetry_synced": False,
        "telemetry_error": "; ".join(reasons)
    }


def sync_and_report(*vehicle_ids):
    """Push each vehicle's state to ThingsBoard; return telemetry_report()."""

    return telemetry_report({
        vehicle_id: sync_vehicle_to_thingsboard(vehicle_id)
        for vehicle_id in vehicle_ids
    })


def sync_all_to_thingsboard():

    results = {}

    for vehicle in fleet.vehicle_ids():

        try:
            results[vehicle] = sync_vehicle_to_thingsboard(vehicle)
        except Exception as exc:
            results[vehicle] = {
                "success": False,
                "error": str(exc)
            }

    return results


# =========================================================
# THINGSBOARD -> LOCAL STATE
# =========================================================

def refresh_vehicle_from_thingsboard(vehicle):
    """Read one vehicle's ThingsBoard device; return the vehicle's state.

    backend mode: a health check only. The read records whether the
    device is reachable and detects deleted devices; the telemetry is
    not applied. `_thingsboard_error` is set only when the read failed.

    thingsboard_mirror mode: applies meaningful telemetry to state (an
    empty or partly configured device never overwrites valid state
    with placeholder values such as "unknown").
    """
    device_id = None

    try:
        device_id = thingsboard.get_device_id(vehicle)
        latest = thingsboard.get_latest_state(device_id) or {}

        clear_missing(vehicle)

        _record_link(vehicle, reachable=True, checked_at=utc_iso(), read_error=None)

        if backend_owns_state():
            return state.get_vehicle_state(vehicle)

        useful = (
            latest.get("vehicle_status") not in (None, "", "unknown")
            or latest.get("location") not in (None, "", "unknown")
            or latest.get("current_stop") not in (None, "", "unknown")
            or latest.get("load_percentage") is not None
        )

        if useful:
            return state.apply_telemetry_state(vehicle, latest)

        current = state.get_vehicle_state(vehicle)
        current["_thingsboard_error"] = "No usable telemetry available."
        return current

    except thingsboard.DeviceNotFound:
        _record_link(vehicle, reachable=False, checked_at=utc_iso(),
                     read_error="Device not found in ThingsBoard.")
        if note_missing_device(vehicle, device_id):
            return _REMOVED
        current = state.get_vehicle_state(vehicle)
        current["_thingsboard_error"] = "Device not found in ThingsBoard."
        return current

    except Exception as exc:
        message = thingsboard.redact(exc, _token_or_none(vehicle))
        _record_link(vehicle, reachable=False, checked_at=utc_iso(), read_error=message)
        current = state.get_vehicle_state(vehicle)
        current["_thingsboard_error"] = message
        return current


def refresh_for_command(vehicle):
    """Before a command: pull ThingsBoard state only in mirror mode.

    When the backend owns state there is nothing to pull, so a
    command never waits for (or depends on) the cloud.
    """

    if not backend_owns_state():
        refresh_from_thingsboard([vehicle])


def refresh_from_thingsboard(vehicle_ids=None):
    """Refresh the given vehicles (default: the whole fleet) in parallel.

    Returns {vehicle_id: state}. Commands pass only the vehicle
    they act on, so a command does not wait for 100 TB reads.
    Vehicles auto-removed during the refresh are left out.
    """
    if vehicle_ids is None:
        vehicle_ids = fleet.vehicle_ids()

    states = thingsboard_pool.map(
        refresh_vehicle_from_thingsboard,
        vehicle_ids
    )

    return {
        vehicle_id: vehicle_state
        for vehicle_id, vehicle_state in zip(vehicle_ids, states)
        if vehicle_state is not _REMOVED
    }


# =========================================================
# DEVICES DELETED IN THINGSBOARD
# =========================================================

def clear_missing(*vehicle_ids):

    with _missing_lock:
        for vehicle_id in vehicle_ids:
            _missing_counts.pop(vehicle_id, None)


def note_missing_device(vehicle, device_id):
    """Count a 404 for `vehicle`; remove it once deletion is confirmed.

    Returns True when the vehicle is no longer registered.
    Never removes on anything but confirmed 404s: an outage,
    expired login or 403 only ever shows the vehicle offline.
    """
    with _missing_lock:
        count = _missing_counts.get(vehicle, 0) + 1
        _missing_counts[vehicle] = count

    if count < MISSING_DEVICE_THRESHOLD:
        return False

    try:
        if thingsboard.device_exists(device_id):
            clear_missing(vehicle)
            return False
    except Exception:
        # Could not confirm. Keep the vehicle, retry next poll.
        return False

    # Never remove a vehicle in the middle of a mission: that
    # would change the handover order under the fleet's feet.
    # Hold it first (stopped, brakes applied, mission HELD); the
    # next poll that still finds the device missing removes it.
    try:
        if state.in_mission(state.get_vehicle_state(vehicle)):
            state.hold_vehicle(vehicle, "DEVICE_DELETED_IN_THINGSBOARD")
            events.log_error(
                f"{vehicle} was held: its ThingsBoard device was deleted during a "
                "mission. It will be removed from the fleet on the next poll.",
                vehicle
            )
            return False
    except ValueError:
        pass

    try:
        fleet.delete_vehicle(
            vehicle,
            backup=True,
            expected_device_id=device_id
        )
    except fleet.FleetError as exc:
        clear_missing(vehicle)
        # Already removed (another poll or a user got there first).
        return exc.code == "VEHICLE_NOT_FOUND"
    except OSError as exc:
        print(f"[fleet] Could not auto-remove {vehicle}: {exc}")
        return False

    clear_missing(vehicle)
    state.forget_vehicle(vehicle)
    j2735.forget(vehicle)
    world.engine.forget(vehicle)
    v2v.bus.forget(vehicle)
    forget_link(vehicle)
    events.log_vehicle_deleted(
        vehicle,
        reason="device deleted in ThingsBoard"
    )

    print(f"[fleet] {vehicle} removed: device deleted in ThingsBoard.")

    return True


def device_missing_in_thingsboard(device_id):
    """True only if ThingsBoard confirms the device does not exist.

    Used to reject a mistyped device ID on add/edit. If
    ThingsBoard cannot be reached, the check is skipped.
    """
    device_id = str(device_id or "").strip()

    if not fleet.DEVICE_ID_PATTERN.match(device_id):
        return False   # fleet validation reports the format error

    try:
        return not thingsboard.device_exists(device_id)
    except Exception:
        return False
