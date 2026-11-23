"""Phase 2 tests: world model, kinematics, single owner of state, simulator.

The world engine's thread is off in tests (WORLD_TICK_MS=0); motion is driven
with engine.tick(dt), so every result is deterministic.
"""

import copy
import json
import math
import threading
from pathlib import Path

import pytest

from services import fleet, simrng, state, world as world_module
from services.settings import settings
from services.world import Pose, World, WorldError, set_path, step

BACKEND = Path(__file__).resolve().parent.parent
WORLD = world_module.world
ENGINE = world_module.engine
V1, V2, V3 = "Vehicle_01", "Vehicle_02", "Vehicle_03"
DT = 0.2


def base_world_data():
    return json.loads((BACKEND / "config" / "world.json").read_text(encoding="utf-8"))


def run(engine, seconds, dt=DT):
    for _ in range(int(round(seconds / dt))):
        engine.tick(dt)


def drive(record_changes, vehicle=V1):
    state.update_vehicle_state(vehicle, **record_changes)


RUNNING = {"status": "running", "speed": 18, "braking_status": "not_applied",
           "motor_direction": "straight", "mission_status": "IN_PROGRESS"}


# ---- the map ------------------------------------------------------------------

def test_world_loads_with_the_mission_loop_and_return_leg():
    stops = WORLD.stop_nodes
    assert {"stop_1", "stop_2", "stop_3", "charging"} <= set(stops)
    assert WORLD.route(stops["stop_1"], stops["stop_2"]) == ["depart", "outbound_1"]
    assert WORLD.route(stops["stop_3"], stops["stop_1"]) == [
        "return_1", "return_2", "return_3", "return_4", "arrive"]
    legs = WORLD.describe()["legs"]
    assert legs["stop_1->stop_2"]["length_m"] == 140.0
    assert legs["stop_3->stop_1"]["length_m"] == 420.0


def test_shared_lane_is_used_in_both_directions():
    lane = WORLD.shared_lanes["shared_s1"]
    depart, arrive = (WORLD.segments[s] for s in lane["segments"])
    assert (depart.start, depart.end) == (arrive.end, arrive.start)


@pytest.mark.parametrize("break_it, message", [
    (lambda d: d["nodes"]["S2"].pop("stop"), "missing required stops"),
    (lambda d: d["segments"].append({"id": "x", "from": "S1", "to": "NOPE"}), "unknown node"),
    (lambda d: d["segments"].append(dict(d["segments"][0])), "duplicate segment"),
    (lambda d: d.__setitem__("segments", [s for s in d["segments"] if s["id"] != "return_4"]), "No route"),
    (lambda d: d["kinematics"].__setitem__("max_accel_mps2", 0), "must be > 0"),
])
def test_invalid_world_is_rejected(break_it, message):
    data = base_world_data()
    break_it(data)
    with pytest.raises(WorldError, match=message):
        World(data)


def test_bad_world_file(tmp_path):
    path = tmp_path / "world.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(WorldError, match="not valid JSON"):
        world_module.load_world(path)


# ---- kinematics (pure) ----------------------------------------------------------

def fresh_pose(path=("outbound_1",)):
    pose = Pose(vehicle_id="t", node="J")
    set_path(WORLD, pose, list(path))
    return pose


def test_acceleration_is_limited():
    pose = fresh_pose()
    for _ in range(10):                     # 1 s
        step(WORLD, pose, 0.1, target_mps=5.0)
    assert pose.speed_mps == pytest.approx(1.0, abs=1e-6)   # max_accel 1 m/s²


def test_segment_speed_limit_is_respected():
    pose = fresh_pose(("depart",))
    pose.segment, pose.node = "depart", None
    top = 0.0
    for _ in range(100):
        step(WORLD, pose, 0.1, target_mps=5.0)
        top = max(top, pose.speed_mps)
    assert top <= 12 / 3.6 + 1e-9


def test_stops_exactly_at_the_destination_without_overshoot():
    pose = Pose(vehicle_id="t", node="S1")
    set_path(WORLD, pose, WORLD.route("S1", "S2"))
    travelled, last_remaining = 0.0, math.inf
    for _ in range(2000):
        before = world_module.remaining_distance(WORLD, pose)
        step(WORLD, pose, 0.1, target_mps=5.0)
        after = world_module.remaining_distance(WORLD, pose)
        assert after <= before + 1e-9       # never moves backward or past the end
        if not pose.path:
            break
    assert pose.node == "S2" and pose.speed_mps == 0 and pose.segment is None


def test_braking_with_emergency_deceleration():
    pose = fresh_pose()
    pose.speed_mps = 5.0
    s0 = pose.s
    for _ in range(10):
        step(WORLD, pose, 0.1, target_mps=0.0, decel_mps2=WORLD.kinematics["emergency_decel_mps2"])
    assert pose.speed_mps == 0
    # v²/(2a) = 25/12 ≈ 2.1 m, plus discretisation
    assert 1.5 < pose.s - s0 < 3.0


# ---- the engine: "done when" ------------------------------------------------------

def test_three_vehicles_on_different_segments_advance_under_a_fixed_tick(api):
    fleet.add_vehicle(V3, "00000000-0000-4000-8000-000000000003", "FAKE_TOKEN_0003")

    placements = {
        V1: ("outbound_1", 10.0, 5.0, None),            # no limit
        V2: ("return_2", 0.0, 5.0, None),               # no limit
        V3: ("depart", 5.0, 12 / 3.6, 12 / 3.6),        # shared lane, 12 km/h limit
    }

    for vehicle, (segment, s, speed, _) in placements.items():
        drive({**RUNNING, "speed": speed * 3.6}, vehicle)
        ENGINE.place(vehicle, segment=segment, s=s, speed_mps=speed,
                     path=[segment, *WORLD.route(WORLD.segments[segment].end, "S2" if segment != "return_2" else "S1")])

    run(ENGINE, 2.0)          # 10 ticks of 0.2 s

    for vehicle, (segment, s, speed, _) in placements.items():
        pose = ENGINE.pose(vehicle)
        assert pose["segment"] == segment
        assert pose["distance_along_m"] == pytest.approx(s + speed * 2.0, abs=0.05)
        x, y = WORLD.point(segment, pose["distance_along_m"])
        assert (pose["x"], pose["y"]) == (pytest.approx(x, abs=0.01), pytest.approx(y, abs=0.01))
        assert pose["heading_deg"] == pytest.approx(WORLD.segments[segment].heading_deg, abs=0.1)


def test_start_drives_the_vehicle_to_stop_2_and_publishes_progress(api):
    assert api.post("/api/start", json={"vehicle": V1}).status_code == 200

    run(ENGINE, 5.0)
    record = state.get_vehicle_state(V1)
    assert record["destination"] == "stop_2"
    assert 0 < record["distance_to_destination"] < 140
    assert record["eta_seconds"] > 0
    assert record["next_action"] == "DRIVE_TO_STOP_2"
    assert record["last_seen"]                         # simulated heartbeat
    assert record["location"] == "stop_1"             # the mission is not changed

    run(ENGINE, 60.0)
    pose = ENGINE.pose(V1)
    assert pose["arrived"] and pose["stop"] == "stop_2"
    assert state.get_vehicle_state(V1)["next_action"] == "ARRIVED_AT_STOP_2"


def test_emergency_stop_brakes_the_moving_vehicle(api):
    api.post("/api/start", json={"vehicle": V1})
    run(ENGINE, 20.0)
    assert ENGINE.pose(V1)["speed_kmh"] > 10

    api.post("/api/emergency-stop", json={"fleet": True})
    run(ENGINE, 1.0)
    pose = ENGINE.pose(V1)
    assert pose["speed_kmh"] == 0 and pose["segment"] is not None   # stopped where it was
    assert state.get_vehicle_state(V1)["next_action"] == "EMERGENCY_STOP"


def test_obstacle_brakes_the_vehicle(api):
    api.post("/api/start", json={"vehicle": V1})
    run(ENGINE, 20.0)
    api.post("/api/sensors/radar", json={"vehicle": V1, "obstacle": True})
    run(ENGINE, 1.0)
    assert ENGINE.pose(V1)["speed_kmh"] == 0
    assert state.get_vehicle_state(V1)["next_action"] == "HOLD_FOR_OBSTACLE"


def test_a_command_that_moves_the_vehicle_snaps_its_pose(api):
    api.post("/api/start", json={"vehicle": V1})
    run(ENGINE, 3.0)
    assert ENGINE.pose(V1)["segment"] == "depart"

    api.post("/api/loading-point", json={"vehicle": V1})     # dashboard: arrived (teleport)
    run(ENGINE, DT)
    pose = ENGINE.pose(V1)
    assert pose["node"] == "S2" and pose["speed_kmh"] == 0


def test_reset_puts_everyone_back_at_stop_1(api):
    api.post("/api/start", json={"vehicle": V1})
    run(ENGINE, 10.0)
    api.post("/api/reset")
    run(ENGINE, DT)
    assert ENGINE.pose(V1)["node"] == "S1"


def test_release_after_emergency_stop_leaves_the_vehicle_on_the_road(api):
    api.post("/api/start", json={"vehicle": V1})
    run(ENGINE, 15.0)
    api.post("/api/emergency-stop", json={"fleet": True})
    run(ENGINE, 2.0)
    where = ENGINE.pose(V1)
    assert where["segment"] is not None

    api.post("/api/emergency-release", json={"fleet": True})
    run(ENGINE, 2.0)
    after = ENGINE.pose(V1)
    assert after["segment"] == where["segment"]
    assert after["distance_along_m"] == pytest.approx(where["distance_along_m"])


def test_manual_forward_and_backward(api):
    assert api.post("/api/manual-control", json={"vehicle": V1, "command": "FORWARD"}).status_code == 200
    run(ENGINE, 5.0)
    forward = ENGINE.pose(V1)
    assert forward["destination_stop"] == "stop_2" and forward["distance_along_m"] > 0

    api.post("/api/manual-control", json={"vehicle": V1, "command": "BACKWARD"})
    run(ENGINE, 30.0)
    back = ENGINE.pose(V1)
    assert back["node"] == "S1" and back["speed_kmh"] == 0


def test_engine_does_not_publish_in_mirror_mode(api):
    settings.state_source = "thingsboard_mirror"
    api.post("/api/start", json={"vehicle": V1})
    run(ENGINE, 5.0)
    record = state.get_vehicle_state(V1)
    assert record["distance_to_destination"] is None and record["last_seen"] is None
    assert ENGINE.pose(V1)["distance_along_m"] > 0      # still simulated


def test_engine_thread_is_off_in_tests():
    assert ENGINE.status()["running"] is False


# ---- endpoints ----------------------------------------------------------------------

def test_world_endpoint(api):
    body = api.get("/api/v1/world").json()
    assert body["success"] is True
    assert {n["id"] for n in body["data"]["nodes"]} >= {"S1", "S2", "S3", "CB"}
    assert body["data"]["shared_lanes"]["shared_s1"]["segments"] == ["depart", "arrive"]
    assert "engine" in body["data"]


def test_pose_endpoints(api):
    poses = api.get("/api/v1/poses").json()["data"]
    assert poses["count"] == 2
    one = api.get("/api/v1/vehicles/yellow/pose").json()["data"]
    assert one["vehicle_id"] == V1 and one["stop"] == "stop_1"
    bad = api.get("/api/v1/vehicles/zzz/pose")
    assert bad.status_code == 400 and bad.json()["code"] == "INVALID_VEHICLE"


def test_new_endpoints_are_documented(api):
    spec = api.get("/openapi.json").json()
    for path in ("/api/v1/world", "/api/v1/poses", "/api/v1/vehicles/{vehicle}/pose"):
        assert path in spec["paths"]


# ---- single owner of state ------------------------------------------------------------

def thingsboard_says_stop_3(device_id):
    return {"vehicle_status": "completed", "current_stop": "stop_3", "location": "stop_3",
            "load_percentage": 100.0, "_reported": ["vehicle_status", "current_stop", "load_percentage"]}


def test_backend_mode_never_applies_thingsboard_telemetry(api, fake_tb):
    fake_tb.latest_state = thingsboard_says_stop_3
    vehicle = api.get("/api/fleet").json()["data"]["vehicles"][0]
    assert vehicle["location"] == "stop_1"                  # local state wins
    assert "_thingsboard_error" not in vehicle
    assert vehicle["thingsboard_link"]["reachable"] is True


def test_mirror_mode_still_applies_thingsboard_telemetry(api, fake_tb):
    settings.state_source = "thingsboard_mirror"
    fake_tb.latest_state = thingsboard_says_stop_3
    vehicle = api.get("/api/fleet").json()["data"]["vehicles"][0]
    assert vehicle["location"] == "stop_3"


def test_commands_do_not_read_thingsboard_in_backend_mode(api, fake_tb):
    reads = []

    def counting_read(device_id):
        reads.append(device_id)
        raise RuntimeError("ThingsBoard down")

    fake_tb.latest_state = counting_read
    assert api.post("/api/start", json={"vehicle": V1}).status_code == 200
    assert reads == []


def test_vehicle_endpoint_survives_thingsboard_outage_in_backend_mode(api, fake_tb):
    def down(device_id):
        raise RuntimeError("connection refused")

    fake_tb.latest_state = down
    response = api.get(f"/api/vehicles/{V1}")
    assert response.status_code == 200
    link = response.json()["data"]["thingsboard_link"]
    assert link["reachable"] is False and "connection refused" in link["read_error"]


def test_push_result_is_tracked_per_vehicle(api, fake_tb):
    api.post("/api/start", json={"vehicle": V1})
    assert api.get(f"/api/vehicles/{V1}").json()["data"]["thingsboard_link"]["last_push_ok"] is True

    fake_tb.push_error = "down"
    api.post("/api/sensors/brake", json={"vehicle": V1, "applied": True})
    link = api.get(f"/api/vehicles/{V1}").json()["data"]["thingsboard_link"]
    assert link["last_push_ok"] is False and "down" in link["last_push_error"]


# ---- seeded randomness ------------------------------------------------------------------

def test_seeded_streams_repeat_and_are_independent():
    simrng.reset(42)
    first = [simrng.stream("v2v.loss").random() for _ in range(5)]
    other = [simrng.stream("v2v.latency").random() for _ in range(5)]
    simrng.reset(42)
    again = [simrng.stream("v2v.loss").random() for _ in range(5)]
    simrng.reset(43)
    seeded = [simrng.stream("v2v.loss").random() for _ in range(5)]
    assert first == again and first != other and first != seeded
    simrng.reset(1234)


# ---- the simulator is an API client ---------------------------------------------------------

def test_simulator_no_longer_writes_to_thingsboard():
    source = (BACKEND / "vehicle_simulator.py").read_text(encoding="utf-8")
    assert "services" not in source and "thingsboard.send" not in source
    assert "/api/start" in source and "/api/complete" in source


def test_simulator_runs_a_full_cycle_through_the_api(api, monkeypatch):
    import vehicle_simulator as sim

    monkeypatch.setattr(sim.time, "sleep", lambda seconds: None)

    backend = sim.Backend("", V1)
    backend.session = api            # TestClient speaks the same API as requests

    done = threading.Event()

    def simulate_time():             # fast-forward the world while the simulator waits
        while not done.is_set():
            ENGINE.tick(DT)

    clock = threading.Thread(target=simulate_time, daemon=True)
    clock.start()
    try:
        sim.run_cycle(backend, first_in_order=True, handover=True, arrival_timeout=30)
    finally:
        done.set()
        clock.join(timeout=5)

    v1, v2 = state.get_vehicle_state(V1), state.get_vehicle_state(V2)
    assert v1["location"] == "stop_3" and v1["mission_status"] == "COMPLETED"
    assert v2["handover_status"] == "ready" and v2["status"] == "ready"
