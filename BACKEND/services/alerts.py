# =========================================================
# ALERT LIFECYCLE (UPGRADE PHASE 5)
# =========================================================
#
# Conditions the old dashboard used to compute client-side
# (Emergency Active, Vehicle Offline, Communication Lost,
# Battery Below 20%, Mission Failed) are now real backend
# records with a lifecycle: open -> acknowledged -> resolved.
#
# AlertManager.evaluate() runs as a world-engine tick hook: it
# looks at every vehicle's state plus the autopilot watchdog and
# opens/auto-resolves alerts as conditions appear and clear. An
# operator may also acknowledge or resolve one by hand (the
# underlying condition does not have to be gone to acknowledge
# it; it does to auto-resolve).
#
# Kept in memory (like services/events.py) for fast reads, and
# persisted to services/db.py's `alerts` table (one row per
# alert id, kept current) so open/acknowledged alerts survive a
# restart; resolved ones are history only.
# =========================================================

import itertools
import threading
from copy import deepcopy

from . import db
from . import events
from . import fleet
from .settings import settings
from .timeutil import utc_iso


STATE_OPEN = "open"
STATE_ACKNOWLEDGED = "acknowledged"
STATE_RESOLVED = "resolved"

# kind -> (severity, label). Matches the alert types from the
# original dashboard (FRONTEND/script.js), now computed here.
KINDS = {
    "EMERGENCY_ACTIVE":       ("critical", "Emergency stop active"),
    "VEHICLE_OFFLINE":        ("critical", "No heartbeat from the vehicle"),
    "COMMUNICATION_LOST":     ("warning", "V2V contact with every vehicle in range is lost"),
    "BATTERY_LOW":            ("warning", "Battery below the low-battery threshold"),
    "VEHICLE_FAULT":          ("critical", "Vehicle fault (simulated breakdown)"),
    "WATCHDOG_FAILED":        ("critical", "Autopilot watchdog tripped: fleet emergency stop latched"),
    "MISSION_FAILED":         ("warning", "The autopilot could not continue the mission (NEEDS_OPERATOR)"),
}


class AlertError(ValueError):

    def __init__(self, message, code, status=400):
        super().__init__(message)
        self.code = code
        self.status = status


_lock = threading.RLock()
_alerts = {}                    # id -> dict (current state)
_id_counter = itertools.count(1)
_open_key = {}                  # (vehicle, kind) -> id, only while not resolved


def reset_for_tests():

    with _lock:
        _alerts.clear()
        _open_key.clear()
        global _id_counter
        _id_counter = itertools.count(1)


def restore():
    """After a restart: bring back every alert that was not yet resolved."""

    with _lock:
        _alerts.clear()
        _open_key.clear()

        try:
            rows = db.open_alerts()
            next_id = db.max_alert_id() + 1
        except db.DBError:
            return

        for row in rows:
            _alerts[row["id"]] = _from_row(row)
            if row["state"] != STATE_RESOLVED:
                _open_key[(row["vehicle"], row["kind"])] = row["id"]

        global _id_counter
        _id_counter = itertools.count(max(next_id, 1))


def _from_row(row):

    return {
        "id": row["id"], "created_at": row["created_at"], "updated_at": row["updated_at"],
        "vehicle": row["vehicle"], "severity": row["severity"], "kind": row["kind"],
        "message": row["message"], "state": row["state"],
        "acknowledged_by": row["acknowledged_by"], "acknowledged_at": row["acknowledged_at"],
        "resolved_by": row["resolved_by"], "resolved_at": row["resolved_at"],
    }


def _persist(alert):

    db.record(
        "alerts", mode="upsert",
        id=alert["id"], created_at=alert["created_at"], updated_at=alert["updated_at"],
        vehicle=alert["vehicle"], severity=alert["severity"], kind=alert["kind"],
        message=alert["message"], state=alert["state"],
        acknowledged_by=alert["acknowledged_by"], acknowledged_at=alert["acknowledged_at"],
        resolved_by=alert["resolved_by"], resolved_at=alert["resolved_at"],
    )


# Set by app.py to also push alert changes onto the SSE "alerts" channel.
on_change = None


def _notify(alert):

    if on_change is not None:
        try:
            on_change(deepcopy(alert))
        except Exception as exc:
            print(f"[alerts] notify failed: {exc}")


# =========================================================
# RAISE / AUTO-RESOLVE (condition-driven)
# =========================================================

def raise_alert(kind, vehicle, message=None, severity=None):
    """Open an alert for (vehicle, kind), or do nothing if one is already open/acknowledged."""

    with _lock:

        key = (vehicle, kind)

        if key in _open_key:
            return _alerts[_open_key[key]]

        default_severity, label = KINDS.get(kind, ("info", kind))
        now = utc_iso(milliseconds=True)

        alert = {
            "id": next(_id_counter), "created_at": now, "updated_at": now,
            "vehicle": vehicle, "severity": severity or default_severity, "kind": kind,
            "message": message or label, "state": STATE_OPEN,
            "acknowledged_by": None, "acknowledged_at": None,
            "resolved_by": None, "resolved_at": None,
        }

        _alerts[alert["id"]] = alert
        _open_key[key] = alert["id"]

    _persist(alert)
    events.add_event(f"[ALERT] {alert['message']}", vehicle, event_type="alert", severity=alert["severity"],
                     details={"alert_id": alert["id"], "kind": kind})
    _notify(alert)
    return alert


def auto_resolve(kind, vehicle, why="condition cleared"):
    """The condition that raised this alert is gone: resolve it automatically."""

    with _lock:

        key = (vehicle, kind)
        alert_id = _open_key.get(key)

        if alert_id is None:
            return None

        alert = _alerts[alert_id]
        alert.update(state=STATE_RESOLVED, resolved_by="system", resolved_at=utc_iso(milliseconds=True),
                     updated_at=utc_iso(milliseconds=True))
        del _open_key[key]

    _persist(alert)
    events.add_event(f"[ALERT] Resolved automatically: {alert['message']} ({why}).", vehicle,
                     event_type="alert", severity="info", details={"alert_id": alert_id, "kind": kind})
    _notify(alert)
    return alert


# =========================================================
# OPERATOR ACTIONS
# =========================================================

def get(alert_id):

    with _lock:
        alert = _alerts.get(alert_id)
        return deepcopy(alert) if alert else None


def acknowledge(alert_id, actor="operator"):

    with _lock:

        alert = _alerts.get(alert_id)

        if alert is None:
            raise AlertError(f"No alert with id {alert_id}.", "ALERT_NOT_FOUND", 404)

        if alert["state"] == STATE_RESOLVED:
            raise AlertError("This alert is already resolved.", "ALERT_RESOLVED", 409)

        if alert["state"] != STATE_ACKNOWLEDGED:
            alert.update(state=STATE_ACKNOWLEDGED, acknowledged_by=actor,
                         acknowledged_at=utc_iso(milliseconds=True), updated_at=utc_iso(milliseconds=True))

        result = deepcopy(alert)

    _persist(alert)
    _notify(alert)
    return result


def resolve(alert_id, actor="operator"):

    with _lock:

        alert = _alerts.get(alert_id)

        if alert is None:
            raise AlertError(f"No alert with id {alert_id}.", "ALERT_NOT_FOUND", 404)

        if alert["state"] != STATE_RESOLVED:
            alert.update(state=STATE_RESOLVED, resolved_by=actor,
                         resolved_at=utc_iso(milliseconds=True), updated_at=utc_iso(milliseconds=True))
            _open_key.pop((alert["vehicle"], alert["kind"]), None)

        result = deepcopy(alert)

    _persist(alert)
    _notify(alert)
    return result


def list_alerts(state=None, vehicle=None, severity=None):

    with _lock:
        items = list(_alerts.values())

    if state:
        items = [a for a in items if a["state"] == state]
    if vehicle:
        items = [a for a in items if a["vehicle"] == vehicle]
    if severity:
        items = [a for a in items if a["severity"] == severity]

    return [deepcopy(a) for a in sorted(items, key=lambda a: -a["id"])]


def forget_vehicle(vehicle_id):
    """A deleted vehicle's open alerts are resolved (not deleted: still history)."""

    with _lock:
        ids = [alert_id for (v, k), alert_id in list(_open_key.items()) if v == vehicle_id]

    for alert_id in ids:
        resolve(alert_id, actor="system (vehicle removed)")


# =========================================================
# CONDITION EVALUATION (world engine tick hook)
# =========================================================

def evaluate(state_module, v2v_module, autopilot_module):
    """Open/auto-resolve alerts from the current fleet state. Cheap: call every tick."""

    for vehicle_id in fleet.vehicle_ids():

        try:
            record = state_module.get_vehicle_state(vehicle_id)
        except ValueError:
            continue

        _check(record.get("emergency_stop"), "EMERGENCY_ACTIVE", vehicle_id,
              f"{events.vehicle_name(vehicle_id)} is under emergency stop.")

        _check(bool(record.get("fault")), "VEHICLE_FAULT", vehicle_id,
              f"{events.vehicle_name(vehicle_id)} has a fault: {record.get('fault')}.")

        _check(bool(record.get("comm_hold")), "COMMUNICATION_LOST", vehicle_id,
              f"{events.vehicle_name(vehicle_id)} has lost V2V contact with every vehicle in range.")

        battery = state_module.battery_of(record)
        _check(battery is not None and battery < settings.battery_low_pct, "BATTERY_LOW", vehicle_id,
              f"{events.vehicle_name(vehicle_id)} battery is below {settings.battery_low_pct:g}%.")

        offline = _is_offline(record)
        _check(offline, "VEHICLE_OFFLINE", vehicle_id,
              f"{events.vehicle_name(vehicle_id)} has not reported a heartbeat recently.")

        needs_operator = (
            autopilot_module.controller.is_auto(vehicle_id)
            and autopilot_module.controller.memory.get(vehicle_id)
            and autopilot_module.controller.memory[vehicle_id].phase == "NEEDS_OPERATOR"
        )
        _check(needs_operator, "MISSION_FAILED", vehicle_id,
              f"{events.vehicle_name(vehicle_id)} autopilot cannot continue; it needs an operator.")

    watchdog_failed = autopilot_module.controller.failed is not None
    _check(watchdog_failed, "WATCHDOG_FAILED", None,
          "Autopilot watchdog tripped: fleet emergency stop latched.")


def _is_offline(record):

    from .timeutil import utc_now

    last_seen = record.get("last_seen")

    if not last_seen:
        return False          # never reported: not "offline", just unknown

    try:
        from datetime import datetime
        moment = datetime.fromisoformat(str(last_seen).replace("Z", "+00:00"))
    except ValueError:
        return False

    age = (utc_now() - moment).total_seconds()
    return age > settings.vehicle_offline_s


def _check(condition, kind, vehicle, message):

    if condition:
        raise_alert(kind, vehicle, message)
    else:
        auto_resolve(kind, vehicle)
