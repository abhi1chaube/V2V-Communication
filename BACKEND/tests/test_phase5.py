"""Phase 5 tests: persistence (SQLite), the audit trail, the alert lifecycle,
the SSE live push feed, ThingsBoard push throttling, and history/replay.

The world engine's thread is off in tests; ENGINE.tick(dt) moves the vehicles
and runs every tick hook (V2V bus, autopilot, scenarios, alerts.evaluate(),
live.py's fleet/pose delta broadcaster and state_timeline writer).
"""

import json
import sqlite3
import threading
import time
from queue import Empty, Queue

import pytest

from services import alerts, db, fleet, live, state, v2v, world as world_module
from services.settings import settings
from api import sync

ENGINE = world_module.engine
BUS = v2v.bus
V1, V2 = "Vehicle_01", "Vehicle_02"
DT = 0.2


def run(seconds):
    for _ in range(int(round(seconds / DT))):
        ENGINE.tick(DT)


def post(api, path, **body):
    response = api.post(path, json=body)
    return response.status_code, response.json()


def wait_for_db(timeout=2.0):
    """The writer thread is async: wait until its queue is drained."""
    deadline = time.monotonic() + timeout
    while db.statistics()["queued"] > 0 and time.monotonic() < deadline:
        time.sleep(0.01)
    time.sleep(0.02)          # the last dequeued batch finishing its commit


def wait_until(condition, timeout=5.0, step=0.05):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(step)
    return condition()


# =========================================================
# PERSISTENCE (services/db.py)
# =========================================================

def test_activity_log_is_persisted(api):
    events_before = len(db.query_events(limit=1000))
    post(api, "/api/start", vehicle=V1)
    wait_for_db()
    rows = db.query_events(vehicle=V1, limit=10)
    assert len(db.query_events(limit=1000)) > events_before
    assert any(r["message"] and "started" in r["message"].lower() for r in rows)


def test_activity_log_filters_by_type_and_time_range(api):
    post(api, "/api/start", vehicle=V1)
    wait_for_db()
    safety_rows = db.query_events(event_type="movement", limit=50)
    assert all(r["event_type"] == "movement" for r in safety_rows)
    future = db.query_events(start="2099-01-01T00:00:00Z", limit=50)
    assert future == []


def test_v2v_messages_are_persisted(api):
    run(1.0)
    wait_for_db()
    rows = db.query_v2v(msg_type="CAM", limit=50)
    assert rows and all(r["msg_type"] == "CAM" for r in rows)
    assert rows[0]["payload"]["stop"] in ("stop_1", None, rows[0]["payload"].get("stop"))

    by_vehicle = db.query_v2v(vehicle=V1, limit=200)
    assert all(V1 in (r["sender"], r["recipient"]) or r["recipient"] == "*" for r in by_vehicle)


def test_state_timeline_is_written_about_once_a_second(api):
    run(3.0)
    wait_for_db()
    rows = db.query_timeline(vehicle=V1, limit=100)
    assert 2 <= len(rows) <= 4                     # ~1 Hz over 3 simulated seconds
    assert rows[0]["location"] == "stop_1"
    assert rows[-1]["timestamp_utc"] >= rows[0]["timestamp_utc"]


def test_latest_timeline_per_vehicle(api):
    run(1.2)
    wait_for_db()
    latest = db.latest_timeline_per_vehicle()
    assert set(latest) == {V1, V2}
    assert latest[V1]["vehicle"] == V1


def test_retention_cleanup_deletes_old_rows(api):
    db.record("activity_log", timestamp_utc="2000-01-01T00:00:00.000Z", vehicle=V1,
             event_type="test", severity="info", message="ancient", details_json="{}")
    wait_for_db()
    assert any(r["message"] == "ancient" for r in db.query_events(limit=1000))

    conn = sqlite3.connect(str(db.path()))
    try:
        db._cleanup(conn)
    finally:
        conn.close()

    assert not any(r["message"] == "ancient" for r in db.query_events(limit=1000))


def test_writes_never_raise_when_the_writer_is_stopped(api):
    db.stop()
    try:
        db.record("activity_log", timestamp_utc="x", vehicle=None, event_type="t",
                  severity="info", message="dropped", details_json="{}")
        stats = db.statistics()
        assert stats["dropped"] >= 1 or not stats["running"]
    finally:
        db.start()


# =========================================================
# SAFE RESTORE AFTER A RESTART
# =========================================================

def test_restore_from_row_is_always_held_manual_never_moving():
    row = {"location": "stop_2", "segment": "outbound_1", "distance_along_m": 40.0, "node": None,
          "load_percentage": 55, "battery": 42.5, "emergency_stop": 0, "fault": None}

    record = state.restore_from_row(V1, row)

    assert record["status"] == "stopped" and record["speed"] == 0
    assert record["braking_status"] == "applied" and record["motor_direction"] == "stopped"
    assert record["mission_status"] == "HELD" and record["autopilot_mode"] == "MANUAL"
    assert record["load_percentage"] == 55 and record["battery_percentage"] == 42.5
    assert record["location"] == "stop_2" and record["emergency_stop"] is False


def test_restore_from_row_keeps_the_emergency_latch_exactly():
    row = {"location": "stop_3", "segment": None, "distance_along_m": 0.0, "node": "S3",
          "load_percentage": 0, "battery": 10, "emergency_stop": 1, "fault": "BREAKDOWN"}

    record = state.restore_from_row(V1, row)

    assert record["emergency_stop"] is True
    assert record["status"] == "emergency_stop" and record["mission_status"] == "EMERGENCY"
    assert record["fault"] == "BREAKDOWN"


def test_restore_fleet_restores_position_held_and_manual(api):
    """Exercises app.restore_fleet(), the exact function the lifespan calls
    on a real restart: writes real state_timeline rows, blanks the in-memory
    state and poses (simulating the moment just after a fresh process starts,
    before restore runs), then restores from the database."""

    import app as backend_module

    ENGINE.place(V1, segment="outbound_1", s=55.0, speed_mps=3.0, path=["outbound_1"])
    state.update_vehicle_state(V1, status=state.STATUS_RUNNING, mission_status="MOVING_TO_LOADING",
                               speed=18, load_percentage=30, load=300, battery_percentage=61, battery_level=61)
    run(1.2)                                       # writes state_timeline rows via the tick hook
    wait_for_db()
    last_row = db.latest_timeline_per_vehicle()[V1]

    with state._lock:
        state.vehicles.clear()
    with ENGINE._lock:
        ENGINE.poses.clear()

    restored = backend_module.restore_fleet()

    assert V1 in restored
    record = state.get_vehicle_state(V1)
    assert record["speed"] == 0 and record["braking_status"] == "applied"
    assert record["mission_status"] == "HELD" and record["autopilot_mode"] == "MANUAL"
    assert record["load_percentage"] == 30
    assert record["battery_percentage"] == pytest.approx(61, abs=0.1)
    assert record["status"] == "stopped" and record["emergency_stop"] is False

    pose = ENGINE.pose(V1)
    assert pose["speed_kmh"] == 0 and pose["segment"] == "outbound_1"
    assert pose["distance_along_m"] == pytest.approx(last_row["distance_along_m"], abs=0.1)


# =========================================================
# ALERTS (services/alerts.py, /api/v1/alerts)
# =========================================================

def test_no_alerts_at_rest(api):
    run(1.0)
    assert alerts.list_alerts(state=alerts.STATE_OPEN) == []


def test_emergency_stop_opens_an_alert_and_release_resolves_it(api):
    post(api, "/api/emergency-stop")
    run(0.2)
    open_alerts = alerts.list_alerts(state=alerts.STATE_OPEN)
    assert any(a["kind"] == "EMERGENCY_ACTIVE" and a["vehicle"] == V1 for a in open_alerts)

    post(api, "/api/emergency-release", fleet=True)
    run(0.2)
    assert not any(a["kind"] == "EMERGENCY_ACTIVE" and a["state"] != alerts.STATE_RESOLVED
                   for a in alerts.list_alerts())
    resolved = [a for a in alerts.list_alerts(vehicle=V1) if a["kind"] == "EMERGENCY_ACTIVE"][0]
    assert resolved["state"] == alerts.STATE_RESOLVED and resolved["resolved_by"] == "system"


def test_fault_opens_an_alert_cleared_by_repair(api):
    state.set_fault(V1, "BREAKDOWN")
    run(0.2)
    assert any(a["kind"] == "VEHICLE_FAULT" and a["state"] == alerts.STATE_OPEN
               for a in alerts.list_alerts(vehicle=V1))
    state.set_fault(V1, None)
    run(0.2)
    assert not any(a["kind"] == "VEHICLE_FAULT" and a["state"] != alerts.STATE_RESOLVED
                   for a in alerts.list_alerts(vehicle=V1))


def test_low_battery_opens_an_alert(api):
    state.set_battery(V1, 10)
    run(0.2)
    assert any(a["kind"] == "BATTERY_LOW" for a in alerts.list_alerts(vehicle=V1, state=alerts.STATE_OPEN))
    state.set_battery(V1, 90)
    run(0.2)
    assert not any(a["kind"] == "BATTERY_LOW" and a["state"] != alerts.STATE_RESOLVED
                   for a in alerts.list_alerts(vehicle=V1))


def test_communication_lost_opens_an_alert(api):
    # comm_hold is recalculated by the V2V bus every tick (services/v2v.py),
    # so a real heartbeat loss is needed, not a manual override (which the
    # next tick would overwrite): same pattern as test_phase3/4's fail-safe
    # tests (silence for the full CAM timeout => comm_hold).
    run(2.0)
    BUS.update_config({"loss_rate": 1.0})
    run(2.5)
    assert any(a["kind"] == "COMMUNICATION_LOST" for a in alerts.list_alerts(vehicle=V1, state=alerts.STATE_OPEN))
    BUS.update_config({"loss_rate": 0.0})
    run(1.5)
    assert not any(a["kind"] == "COMMUNICATION_LOST" and a["state"] != alerts.STATE_RESOLVED
                   for a in alerts.list_alerts(vehicle=V1))


def test_watchdog_trip_raises_a_fleet_alert(api):
    from services import autopilot

    autopilot.controller.enable([V1])
    run(0.2)
    autopilot.controller.trip("TICK_STALLED", "test-induced trip")
    run(0.2)
    assert any(a["kind"] == "WATCHDOG_FAILED" and a["state"] == alerts.STATE_OPEN for a in alerts.list_alerts())
    autopilot.controller.recover()


def test_acknowledge_then_resolve_an_alert(api):
    post(api, "/api/emergency-stop")
    run(0.2)
    alert_id = [a for a in alerts.list_alerts(vehicle=V1) if a["kind"] == "EMERGENCY_ACTIVE"][0]["id"]

    status, body = post(api, f"/api/v1/alerts/{alert_id}/ack", actor="operator-1")
    assert status == 200 and body["data"]["state"] == "acknowledged"
    assert body["data"]["acknowledged_by"] == "operator-1"

    status, body = post(api, f"/api/v1/alerts/{alert_id}/resolve", actor="operator-1")
    assert status == 200 and body["data"]["state"] == "resolved"

    post(api, "/api/emergency-release", fleet=True)


def test_alert_errors(api):
    assert post(api, "/api/v1/alerts/999999/ack")[1]["code"] == "ALERT_NOT_FOUND"

    post(api, "/api/emergency-stop")
    run(0.2)
    alert_id = [a for a in alerts.list_alerts(vehicle=V1) if a["kind"] == "EMERGENCY_ACTIVE"][0]["id"]
    post(api, f"/api/v1/alerts/{alert_id}/resolve")
    status, body = post(api, f"/api/v1/alerts/{alert_id}/ack")
    assert status == 409 and body["code"] == "ALERT_RESOLVED"
    post(api, "/api/emergency-release", fleet=True)


def test_list_alerts_endpoint_filters(api):
    post(api, "/api/emergency-stop")
    run(0.2)
    data = api.get("/api/v1/alerts?state=open").json()["data"]
    assert data["count"] >= 1 and all(a["state"] == "open" for a in data["alerts"])
    data = api.get(f"/api/v1/alerts?vehicle={V1}").json()["data"]
    assert all(a["vehicle"] == V1 for a in data["alerts"])
    assert api.get("/api/v1/alerts?state=weird").json()["code"] == "INVALID_ALERT_STATE"
    post(api, "/api/emergency-release", fleet=True)


def test_deleting_a_vehicle_resolves_its_open_alerts(api):
    fleet.add_vehicle("Vehicle_03", "00000000-0000-4000-8000-000000000003", "FAKE_TOKEN_0003")
    state.set_fault("Vehicle_03", "BREAKDOWN")
    run(0.2)
    assert any(a["kind"] == "VEHICLE_FAULT" and a["state"] == "open" for a in alerts.list_alerts(vehicle="Vehicle_03"))
    assert api.delete("/api/vehicles/Vehicle_03").status_code == 200
    assert not any(a["state"] != "resolved" for a in alerts.list_alerts(vehicle="Vehicle_03"))


def test_alerts_restore_after_a_restart(api):
    """Exercises alerts.restore(), the exact function the lifespan calls."""

    state.set_fault(V1, "BREAKDOWN")
    run(0.2)
    wait_for_db()
    assert any(a["kind"] == "VEHICLE_FAULT" and a["state"] == "open" for a in alerts.list_alerts(vehicle=V1))

    alerts._alerts.clear()
    alerts._open_key.clear()

    alerts.restore()

    assert any(a["kind"] == "VEHICLE_FAULT" and a["vehicle"] == V1 and a["state"] == "open"
               for a in alerts.list_alerts())

    state.set_fault(V1, None)


# =========================================================
# AUDIT TRAIL (api/audit.py, /api/v1/audit)
# =========================================================

def test_mutating_commands_are_audited(api):
    post(api, "/api/start", vehicle=V1)
    wait_for_db()
    entries = db.query_audit(command="POST /api/start", limit=20)
    assert entries and entries[0]["target"] == V1 and entries[0]["success"] is True


def test_get_requests_are_not_audited(api):
    before = len(db.query_audit(limit=10000))
    api.get("/api/fleet")
    api.get(f"/api/vehicles/{V1}")
    wait_for_db()
    after = len(db.query_audit(limit=10000))
    assert after == before


def test_failed_commands_are_audited_with_their_code(api):
    post(api, "/api/emergency-stop")
    status, body = post(api, "/api/start", vehicle=V1)
    wait_for_db()
    assert status == 403
    entries = db.query_audit(command="POST /api/start", limit=20)
    assert entries[0]["result_code"] == body["code"] and entries[0]["success"] is False
    post(api, "/api/emergency-release", fleet=True)


def test_tokens_are_redacted_in_the_audit_trail(api):
    status, body = api.post("/api/vehicles", json={
        "name": "Vehicle_09", "device_id": "00000000-0000-4000-8000-000000000009",
        "token": "SECRET_TOKEN_VALUE"
    }).status_code, None
    wait_for_db()
    entries = db.query_audit(command="POST /api/vehicles", limit=20)
    assert entries
    dump = json.dumps(entries[0]["details"])
    assert "SECRET_TOKEN_VALUE" not in dump
    assert entries[0]["details"]["body"]["token"] == "***"


def test_audit_endpoint_is_local_only(api, remote):
    post(api, "/api/start", vehicle=V1)
    assert remote.get("/api/v1/audit").status_code == 403
    assert api.get("/api/v1/audit").status_code == 200


def test_events_clear_is_not_audited(api):
    before = len(db.query_audit(limit=10000))
    api.post("/api/events/clear")
    wait_for_db()
    after = len(db.query_audit(limit=10000))
    assert after == before


# =========================================================
# LIVE STREAM (services/live.py, /api/v1/stream)
# =========================================================

class SSEReader:
    """Drains one streaming httpx response on a single background thread
    (iter_lines() is single-use: calling it again on the same response
    raises), so a test can `read()` events from it more than once, with a
    bounded wait that fails loudly instead of hanging the whole run."""

    def __init__(self, response):
        self.queue = Queue()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, args=(response,), daemon=True)
        self._thread.start()

    def _run(self, response):
        event_name, data_line = None, None
        try:
            for line in response.iter_lines():
                if self._stop.is_set():
                    return
                if line.startswith("event:"):
                    event_name = line[len("event:"):].strip()
                elif line.startswith("data:"):
                    data_line = line[len("data:"):].strip()
                elif line == "" and event_name is not None:
                    self.queue.put((event_name, json.loads(data_line)))
                    event_name, data_line = None, None
        except Exception as exc:
            self.queue.put(("__error__", str(exc)))

    def read(self, want, timeout=8.0):
        collected = []
        deadline = time.monotonic() + timeout

        while len(collected) < want and time.monotonic() < deadline:
            try:
                item = self.queue.get(timeout=max(0.05, deadline - time.monotonic()))
            except Empty:
                break
            if item[0] == "__error__":
                raise AssertionError(f"SSE stream failed: {item[1]}")
            collected.append(item)

        return collected

    def close(self):
        self._stop.set()


def test_stream_rejects_an_unknown_channel(api):
    response = api.get("/api/v1/stream?channels=nope")
    assert response.status_code == 400 and response.json()["code"] == "INVALID_CHANNEL"


def test_stream_sends_a_snapshot_on_connect(api):
    with api.stream("GET", "/api/v1/stream?channels=fleet,alerts") as response:
        reader = SSEReader(response)
        events = reader.read(want=2, timeout=8)
        reader.close()

    kinds = {e[0] for e in events}
    assert kinds == {"snapshot"}
    channels = {e[1]["channel"] for e in events}
    assert channels == {"fleet", "alerts"}
    fleet_snapshot = next(e[1] for e in events if e[1]["channel"] == "fleet")
    assert V1 in fleet_snapshot["data"]["vehicles"]


def test_stream_pushes_an_events_delta_without_a_tick(api):
    with api.stream("GET", "/api/v1/stream?channels=events") as response:
        reader = SSEReader(response)
        reader.read(want=1, timeout=8)                # the snapshot

        from services import events as events_module
        events_module.add_event("[TEST] live stream probe", V1, event_type="test")

        delivered = reader.read(want=1, timeout=8)
        reader.close()

    assert delivered and delivered[0][0] == "events"
    assert delivered[0][1]["data"]["message"] == "[TEST] live stream probe"


def test_stream_pushes_a_v2v_message(api):
    with api.stream("GET", "/api/v1/stream?channels=v2v") as response:
        reader = SSEReader(response)
        reader.read(want=1, timeout=8)

        BUS.send(V1, v2v.BROADCAST, "DENM", {"denm_id": "live-test", "action": "new", "cause": "OBSTACLE", "about": V1})

        delivered = reader.read(want=1, timeout=8)
        reader.close()

    assert delivered and delivered[0][0] == "v2v"
    assert delivered[0][1]["data"]["msg_type"] == "DENM"


def test_stream_pushes_a_fleet_delta_on_tick(api):
    with api.stream("GET", "/api/v1/stream?channels=fleet,poses") as response:
        reader = SSEReader(response)
        reader.read(want=2, timeout=8)                # two snapshots

        post(api, "/api/start", vehicle=V1)
        ENGINE.tick(DT)

        delivered = reader.read(want=2, timeout=8)
        reader.close()

    assert {e[0] for e in delivered} == {"fleet", "poses"}
    fleet_delta = next(e[1] for e in delivered if e[0] == "fleet")
    changed_ids = {c["vehicle_id"] for c in fleet_delta["data"]["changed"]}
    assert V1 in changed_ids


def test_client_count_and_resync(api, monkeypatch):
    monkeypatch.setattr(settings, "sse_resync_s", 0.3)

    assert live.hub.client_count() == 0

    with api.stream("GET", "/api/v1/stream?channels=alerts") as response:
        assert wait_until(lambda: live.hub.client_count() == 1, timeout=3)
        reader = SSEReader(response)
        events = reader.read(want=2, timeout=3)        # initial snapshot + one resync
        reader.close()

    assert len(events) >= 2 and all(e[0] == "snapshot" for e in events)
    assert wait_until(lambda: live.hub.client_count() == 0, timeout=3)


# =========================================================
# THINGSBOARD PUSH THROTTLING (api/sync.py)
# =========================================================

def test_first_autopilot_push_is_immediate(api, fake_tb):
    sync.autopilot_sync(V1, radio=False)
    assert wait_until(lambda: sync.push_statistics()["pushed"] >= 1, timeout=2)


def test_repeated_pushes_within_the_window_are_coalesced_then_flushed(api, fake_tb, monkeypatch):
    monkeypatch.setattr(settings, "tb_push_min_interval_s", 0.3)

    sync.autopilot_sync(V1, radio=False)
    assert wait_until(lambda: sync.push_statistics()["pushed"] == 1, timeout=2)

    sync.autopilot_sync(V1, radio=False)                      # same safety fields, inside the window
    assert sync.push_statistics()["coalesced"] == 1
    assert sync.push_statistics()["pushed"] == 1               # not pushed yet

    assert wait_until(lambda: sync.push_statistics()["pushed"] == 2, timeout=2)   # the flusher catches up


def test_a_safety_relevant_change_is_never_throttled(api, fake_tb, monkeypatch):
    monkeypatch.setattr(settings, "tb_push_min_interval_s", 10.0)      # a window we won't wait out

    sync.autopilot_sync(V1, radio=False)
    assert wait_until(lambda: sync.push_statistics()["pushed"] == 1, timeout=2)

    state.update_vehicle_state(V1, emergency_stop=True)
    sync.autopilot_sync(V1, radio=False)
    assert wait_until(lambda: sync.push_statistics()["pushed"] == 2, timeout=2)
    assert sync.push_statistics()["coalesced"] == 0


# =========================================================
# METRICS
# =========================================================

def test_metrics_endpoint_is_prometheus_text(api):
    run(1.0)
    response = api.get("/metrics")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    text = response.text
    assert "v2v_world_ticks_total" in text
    assert "v2v_messages_sent_total" in text
    assert "v2v_sse_clients" in text
    assert "# HELP" in text and "# TYPE" in text


# =========================================================
# HISTORY / REPLAY
# =========================================================

def test_events_history_endpoint(api):
    post(api, "/api/start", vehicle=V1)
    wait_for_db()
    data = api.get(f"/api/v1/events/history?vehicle={V1}&limit=20").json()["data"]
    assert data["count"] > 0 and all(e["vehicle"] == V1 for e in data["events"])
    assert api.get("/api/v1/events/history?vehicle=zzz").json()["code"] == "INVALID_VEHICLE"


def test_v2v_history_endpoint(api):
    run(1.0)
    wait_for_db()
    data = api.get("/api/v1/v2v/history?type=CAM&limit=20").json()["data"]
    assert data["count"] > 0 and all(m["msg_type"] == "CAM" for m in data["messages"])
    assert api.get("/api/v1/v2v/history?type=NOPE").json()["code"] == "INVALID_MESSAGE_TYPE"


def test_replay_endpoint(api):
    run(2.0)
    wait_for_db()
    data = api.get(f"/api/v1/replay?vehicle={V1}&limit=50").json()["data"]
    assert data["count"] > 0
    assert data["timeline"][0]["vehicle"] == V1
    assert "x" in data["timeline"][0] and "battery" in data["timeline"][0]
