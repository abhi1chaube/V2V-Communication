"""Phase 1 regression tests: one or more per fixed defect.

Uses the `api` / `remote` / `fake_tb` fixtures from conftest.py (fake
vehicles Vehicle_01 and Vehicle_02, ThingsBoard faked, no network).
"""

import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import requests

from services import events, fleet, state, thingsboard
from services.settings import settings
from api import commands

V1, V2 = "Vehicle_01", "Vehicle_02"
BACKEND = Path(__file__).resolve().parent.parent


def post(api, path, **body):
    response = api.post(path, json=body)
    return response.status_code, response.json()


def code(result):
    return result[1].get("code")


def drive_to_stop2(api, vehicle=V1):
    assert post(api, "/api/start", vehicle=vehicle)[0] == 200
    assert post(api, "/api/loading-point", vehicle=vehicle)[0] == 200


def load_full(api, vehicle=V1):
    assert post(api, "/api/loading/start", vehicle=vehicle)[0] == 200
    assert post(api, "/api/loading/update", vehicle=vehicle, percentage=100)[0] == 200


def drive_to_warehouse(api, vehicle=V1):
    drive_to_stop2(api, vehicle)
    load_full(api, vehicle)
    assert post(api, "/api/warehouse", vehicle=vehicle)[0] == 200


# ---- a) AprilTag / QR interlock ---------------------------------------------

def test_qr_not_detected_blocks_start(api):
    assert post(api, "/api/sensors/qr", vehicle=V1, detected=False)[0] == 200
    result = post(api, "/api/start", vehicle=V1)
    assert result[0] == 403 and code(result) == "QR_NOT_DETECTED"

    assert post(api, "/api/sensors/qr", vehicle=V1, detected=True)[0] == 200
    assert post(api, "/api/start", vehicle=V1)[0] == 200


def test_qr_reading_is_honoured_not_forced(api):
    _, body = post(api, "/api/sensors/qr", vehicle=V1, status="not_detected")
    assert body["data"]["vehicle"]["qr_status"] == "not_detected"


def test_qr_always_detected_demo_flag(api):
    settings.qr_always_detected = True
    post(api, "/api/sensors/qr", vehicle=V1, detected=False)
    assert post(api, "/api/start", vehicle=V1)[0] == 200


def test_qr_reading_follows_the_vehicle(api):
    assert state.get_vehicle_state(V1)["qr_status"] == "detected"      # at Stop 1
    post(api, "/api/start", vehicle=V1)
    assert state.get_vehicle_state(V1)["qr_status"] == "not_detected"  # moving
    post(api, "/api/loading-point", vehicle=V1)
    assert state.get_vehicle_state(V1)["qr_status"] == "detected"      # at Stop 2


def test_qr_not_detected_blocks_loading(api):
    drive_to_stop2(api)
    post(api, "/api/sensors/qr", vehicle=V1, detected=False)
    result = post(api, "/api/loading/start", vehicle=V1)
    assert result[0] == 403 and code(result) == "QR_NOT_DETECTED"


def test_qr_rejects_unknown_reading(api):
    result = post(api, "/api/sensors/qr", vehicle=V1, status="maybe")
    assert result[0] == 400 and code(result) == "QR_UPDATE_FAILED"


# ---- b) manual control checks ------------------------------------------------

def test_manual_movement_blocked_by_radar_but_stop_allowed(api):
    post(api, "/api/sensors/radar", vehicle=V1, obstacle=True)
    result = post(api, "/api/manual-control", vehicle=V1, command="FORWARD")
    assert result[0] == 403 and code(result) == "RADAR_OBSTACLE"
    assert post(api, "/api/manual-control", vehicle=V1, command="STOP")[0] == 200


def test_manual_movement_blocked_by_obstacle_flag(api):
    state.update_vehicle_state(V1, obstacle_detected=True)
    result = post(api, "/api/manual-control", vehicle=V1, command="LEFT")
    assert result[0] == 403 and code(result) == "OBSTACLE_DETECTED"


def test_manual_movement_blocked_while_loading(api):
    drive_to_stop2(api)
    assert post(api, "/api/loading/start", vehicle=V1)[0] == 200
    result = post(api, "/api/manual-control", vehicle=V1, command="BACKWARD")
    assert result[0] == 403 and code(result) == "LOADING_IN_PROGRESS"


# ---- c) obstacle flag at the warehouse step ----------------------------------

def test_reach_warehouse_checks_obstacle_flag_in_state(api):
    drive_to_stop2(api)
    load_full(api)
    state.update_vehicle_state(V1, obstacle_detected=True)

    with pytest.raises(state.TransitionBlocked) as blocked:
        state.reach_warehouse(V1)
    assert blocked.value.code == "OBSTACLE_DETECTED"

    assert post(api, "/api/warehouse", vehicle=V1)[0] == 403


# ---- d) START status guard ---------------------------------------------------

def test_second_start_is_rejected(api):
    assert post(api, "/api/start", vehicle=V1)[0] == 200
    result = post(api, "/api/start", vehicle=V1)
    assert result[0] == 403 and code(result) == "ALREADY_RUNNING"


# ---- e) load cannot go down --------------------------------------------------

def test_load_decrease_rejected_unless_forced(api):
    drive_to_stop2(api)
    post(api, "/api/loading/start", vehicle=V1)
    assert post(api, "/api/loading/update", vehicle=V1, percentage=60)[0] == 200

    result = post(api, "/api/loading/update", vehicle=V1, percentage=40)
    assert result[0] == 400 and code(result) == "LOAD_DECREASE_REJECTED"
    assert state.get_vehicle_state(V1)["load_percentage"] == 60

    status, body = post(api, "/api/loading/update", vehicle=V1, percentage=40, force=True)
    assert status == 200 and body["data"]["vehicle"]["load_percentage"] == 40


# ---- f) telemetry push result is reported ------------------------------------

def test_command_reports_successful_push(api):
    _, body = post(api, "/api/start", vehicle=V1)
    assert body["data"]["telemetry_synced"] is True
    assert "telemetry_error" not in body["data"]


def test_command_reports_failed_push_and_still_succeeds(api, fake_tb):
    fake_tb.push_error = "ThingsBoard unreachable"
    status, body = post(api, "/api/start", vehicle=V1)
    assert status == 200 and body["success"] is True
    assert body["data"]["telemetry_synced"] is False
    assert "Vehicle_01" in body["data"]["telemetry_error"]
    assert "ThingsBoard unreachable" in body["data"]["telemetry_error"]


def test_fleet_commands_report_push_result(api, fake_tb):
    fake_tb.push_error = "down"
    _, body = post(api, "/api/emergency-stop", fleet=True)
    assert body["data"]["telemetry_synced"] is False
    _, body = post(api, "/api/reset")
    assert body["data"]["telemetry_synced"] is False


def test_device_token_is_masked_in_push_errors(fake_tb, monkeypatch):
    token = "SECRET_TOKEN_123"

    def fail(url, **kwargs):
        raise requests.ConnectionError(f"Max retries exceeded with url: /api/v1/{token}/telemetry")

    monkeypatch.setattr(thingsboard.requests, "post", fail)

    with pytest.raises(thingsboard.ThingsBoardError) as error:
        fake_tb.original["send_telemetry"](token, {"speed": 0})

    assert token not in str(error.value)
    assert error.value.__suppress_context__ is True


# ---- g) communication status is measured -------------------------------------

def test_communication_is_not_unconditionally_connected(api):
    # Before any V2V traffic nobody can claim to be connected. (Phase 1 used
    # the last_seen age; Phase 3 measures the radio: see test_phase3.py.)
    body = api.get("/api/communication").json()
    assert body["data"]["vehicle_statuses"] == {V1: "unknown", V2: "unknown"}
    assert body["data"]["connected"] is False


# ---- h) servo angle validation -----------------------------------------------

@pytest.mark.parametrize("angle", [500, -1, "abc", 90.5, True, None])
def test_servo_rejects_bad_angles(api, angle):
    result = post(api, "/api/servo", angle=angle)
    assert result[0] == 400 and code(result) == "INVALID_ANGLE"


def test_servo_accepts_valid_angle(api):
    assert post(api, "/api/servo", angle=180)[0] == 200


# ---- i) /api/complete is the handover path -----------------------------------

def test_complete_logs_handover_and_marks_completed(api):
    drive_to_warehouse(api)
    status, body = post(api, "/api/complete", vehicle=V1)
    assert status == 200
    assert body["data"]["completed_vehicle"]["mission_status"] == "COMPLETED"
    assert body["data"]["next_vehicle"]["status"] == "ready"
    assert any(e["details"].get("v2v_message") for e in events.get_events(50))


def test_complete_without_handover(api):
    drive_to_warehouse(api)
    status, body = post(api, "/api/complete", vehicle=V1, handover=False)
    assert status == 200 and body["data"]["next_vehicle"] is None
    assert state.get_vehicle_state(V2)["handover_status"] == "idle"


def test_complete_refused_under_emergency_stop(api):
    drive_to_warehouse(api)
    post(api, "/api/emergency-stop", fleet=True)
    result = post(api, "/api/complete", vehicle=V1)
    assert result[0] == 403 and code(result) == "EMERGENCY_STOP_ACTIVE"


def test_handover_never_clears_next_vehicles_emergency_stop(api):
    drive_to_warehouse(api)
    post(api, "/api/emergency-stop", fleet=True)
    post(api, "/api/emergency-release", vehicle=V1)

    result = post(api, "/api/complete", vehicle=V1)
    assert result[0] == 403 and code(result) == "NEXT_VEHICLE_EMERGENCY"
    assert state.get_vehicle_state(V2)["emergency_stop"] is True
    assert state.get_vehicle_state(V1)["mission_status"] != "COMPLETED"   # nothing changed


def test_handover_keeps_next_vehicles_hazard_reading(api):
    drive_to_warehouse(api)
    post(api, "/api/sensors/radar", vehicle=V2, obstacle=True)
    assert post(api, "/api/complete", vehicle=V1)[0] == 200
    after = state.get_vehicle_state(V2)
    assert after["radar_status"] == "obstacle" and after["obstacle_detected"] is True


def test_dashboard_uses_complete_for_handover():
    script = (BACKEND.parent / "FRONTEND" / "script.js").read_text(encoding="utf-8")
    assert "API.complete" in script
    assert "handover: false" in script


# ---- j, k) housekeeping ------------------------------------------------------

@pytest.mark.parametrize("name", ["app_backup.py", "send_yellow.py", "send_blue.py"])
def test_unused_files_removed(name):
    assert not (BACKEND / name).exists()


def test_test_field_removed(api):
    assert "test_field" not in state.get_vehicle_state(V1)


# ---- l) registry changes during a mission ------------------------------------

def test_registry_changes_blocked_during_mission(api):
    post(api, "/api/start", vehicle=V1)

    added = api.post("/api/vehicles", json={
        "name": "Vehicle_03", "device_id": "00000000-0000-4000-8000-000000000003",
        "token": "FAKE_TOKEN_0003"})
    assert added.status_code == 409 and added.json()["code"] == "FLEET_BUSY"

    renamed = api.put(f"/api/vehicles/{V2}", json={"name": "Truck_02"})
    assert renamed.status_code == 409 and renamed.json()["code"] == "FLEET_BUSY"

    recoloured = api.put(f"/api/vehicles/{V2}", json={"color": "green"})
    assert recoloured.status_code == 200

    busy_delete = api.delete(f"/api/vehicles/{V1}")
    assert busy_delete.status_code == 409 and busy_delete.json()["code"] == "FLEET_BUSY"

    idle_delete = api.delete(f"/api/vehicles/{V2}")
    assert idle_delete.status_code == 200


def test_registry_changes_allowed_after_reset(api):
    post(api, "/api/start", vehicle=V1)
    post(api, "/api/reset")
    renamed = api.put(f"/api/vehicles/{V2}", json={"name": "Truck_02"})
    assert renamed.status_code == 200


def test_deleted_device_mid_mission_is_held_before_removal(api, fake_tb):
    post(api, "/api/start", vehicle=V1)
    fake_tb.missing_devices.add(fleet.device_id(V1))

    for _ in range(3):
        body = api.get("/api/fleet").json()

    assert body["data"]["removed"] == []
    held = state.get_vehicle_state(V1)
    assert held["mission_status"] == "HELD" and held["speed"] == 0
    assert held["braking_status"] == "applied"

    body = api.get("/api/fleet").json()
    assert body["data"]["removed"] == [V1]


# ---- m) concurrent operators -------------------------------------------------

def test_idempotency_key_replays_instead_of_running_twice(api):
    headers = {"Idempotency-Key": "start-v1-0001"}
    first = api.post("/api/start", json={"vehicle": V1}, headers=headers)
    second = api.post("/api/start", json={"vehicle": V1}, headers=headers)

    assert first.status_code == second.status_code == 200
    assert second.headers.get("idempotent-replay") == "true"
    assert second.json() == first.json()

    starts = [e for e in events.get_events(200) if e["message"].startswith("[CMD] START accepted")]
    assert len(starts) == 1


def test_idempotency_key_reused_for_another_request(api):
    headers = {"Idempotency-Key": "k-1"}
    api.post("/api/start", json={"vehicle": V1}, headers=headers)
    other = api.post("/api/start", json={"vehicle": V2}, headers=headers)
    assert other.status_code == 409 and other.json()["code"] == "IDEMPOTENCY_KEY_REUSED"


def test_invalid_idempotency_key(api):
    response = api.post("/api/start", json={"vehicle": V1}, headers={"Idempotency-Key": "x" * 300})
    assert response.status_code == 400 and response.json()["code"] == "INVALID_IDEMPOTENCY_KEY"


def test_concurrent_starts_only_one_wins(api, fake_tb):
    def slow_read(device_id):
        time.sleep(0.3)      # widen the window between validation and change
        return {}

    # Only mirror mode reads ThingsBoard before a command, which is what
    # makes the window between validation and change wide here.
    settings.state_source = "thingsboard_mirror"
    fake_tb.latest_state = slow_read
    results = []

    def start():
        results.append(post(api, "/api/start", vehicle=V1))

    threads = [threading.Thread(target=start) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(status for status, _ in results) == [200, 403]
    assert {code(r) for r in results if r[0] == 403} == {"ALREADY_RUNNING"}


def test_command_waits_then_fails_while_another_holds_the_vehicle(api):
    settings.command_lock_timeout_s = 0.2
    with commands.command_lock(V1):
        result = post(api, "/api/start", vehicle=V1)
    assert result[0] == 409 and code(result) == "COMMAND_IN_PROGRESS"
    assert post(api, "/api/start", vehicle=V1)[0] == 200


def test_emergency_stop_never_waits_for_a_command_lock(api):
    with commands.command_lock(V1, V2):
        started = time.monotonic()
        status, _ = post(api, "/api/emergency-stop", fleet=True)
        elapsed = time.monotonic() - started
    assert status == 200 and elapsed < 2
    assert state.get_vehicle_state(V1)["emergency_stop"] is True


def test_transitions_recheck_emergency_atomically(api):
    drive_to_stop2(api)
    state.update_vehicle_state(V1, emergency_stop=True)
    for transition in (state.start_loading, lambda v: state.update_loading(v, 50)):
        with pytest.raises(state.TransitionBlocked) as blocked:
            transition(V1)
        assert blocked.value.code == "EMERGENCY_STOP_ACTIVE"


# ---- ThingsBoard placeholders cannot override local readings ------------------

def test_unreported_telemetry_keys_are_ignored(api):
    post(api, "/api/sensors/radar", vehicle=V1, obstacle=True)

    state.apply_telemetry_state(V1, {
        "vehicle_status": "ready", "radar_status": "clear",
        "obstacle_detected": False, "_reported": ["vehicle_status"]})
    assert state.get_vehicle_state(V1)["radar_status"] == "obstacle"

    state.apply_telemetry_state(V1, {
        "radar_status": "clear", "obstacle_detected": False,
        "_reported": ["radar_status", "obstacle_detected"]})
    assert state.get_vehicle_state(V1)["radar_status"] == "clear"


# ---- n) TLS verification on by default ---------------------------------------

def test_tls_verification_is_on_by_default():
    assert settings.thingsboard_verify_tls is True
    assert settings.thingsboard_trust_store == "system"
    assert thingsboard.VERIFY_TLS is True
