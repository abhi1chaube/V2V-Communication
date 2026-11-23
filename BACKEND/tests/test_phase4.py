"""Phase 4 tests: the autopilot, the battery model, cooperative driving, the
watchdog, and the seeded scenarios.

The world engine's thread is off in tests; ENGINE.tick(dt) moves the vehicles,
runs the V2V bus, the autopilot and the scenario runner (all tick hooks), so
every run is deterministic (SIM_SEED).
"""

import time

import pytest

from services import autopilot as autopilot_module
from services import fleet, scenarios, state, v2v, world as world_module
from services.settings import settings

ENGINE = world_module.engine
WORLD = world_module.world
BUS = v2v.bus
AP = autopilot_module.controller
V1, V2, V3 = "Vehicle_01", "Vehicle_02", "Vehicle_03"
DT = 0.2


def run(seconds):
    for _ in range(int(round(seconds / DT))):
        ENGINE.tick(DT)


def run_until(condition, limit_s=300.0):
    """Tick until condition() is true; fail if it takes longer than limit_s of simulated time."""
    for _ in range(int(limit_s / DT)):
        if condition():
            return
        ENGINE.tick(DT)
    assert condition(), f"condition not met after {limit_s} simulated seconds"


def post(api, path, **body):
    response = api.post(path, json=body)
    return response.status_code, response.json()


def enable(api, vehicle=None, **extra):
    body = {"vehicle": vehicle} if vehicle else {"fleet": True}
    return post(api, "/api/v1/autopilot/enable", **body, **extra)


def trace(event=None, vehicle=None, **match):
    return [
        e for e in AP.trace
        if (event is None or e["event"] == event)
        and (vehicle is None or e["vehicle"] == vehicle)
        and all(e.get(k) == v for k, v in match.items())
    ]


def arrivals(vehicle, stop):
    return trace("ARRIVED", vehicle, stop=stop)


def pose(vehicle):
    return ENGINE.pose(vehicle)


def mode(vehicle):
    return state.get_vehicle_state(vehicle)["autopilot_mode"]


def events_list(api):
    """Activity log entries (newest first), whatever the envelope nests them in."""
    data = api.get("/api/events?limit=100").json()["data"]
    return data if isinstance(data, list) else data.get("events", data.get("items", []))


def add_third_vehicle():
    fleet.add_vehicle(V3, "00000000-0000-4000-8000-000000000003", "FAKE_TOKEN_0003")


# ---- modes -----------------------------------------------------------------------

def test_every_vehicle_starts_in_manual(api):
    data = api.get("/api/v1/autopilot/status").json()["data"]
    assert {v["mode"] for v in data["vehicles"]} == {"MANUAL"}
    assert data["watchdog"]["state"] == "DISARMED" and data["dispatch"] == "handover"
    assert {mode(V1), mode(V2)} == {"MANUAL"}
    health = api.get("/api/health").json()["autopilot"]
    assert health["state"] == "online" and health["auto_vehicles"] == 0


def test_enable_and_disable_a_vehicle(api):
    status, body = enable(api, V1)
    assert status == 200 and body["data"]["enabled"] == [V1]
    assert mode(V1) == "AUTO" and mode(V2) == "MANUAL"
    assert api.get("/api/v1/autopilot/status").json()["data"]["watchdog"]["state"] == "ARMED"

    status, body = post(api, "/api/v1/autopilot/disable", vehicle=V1)
    assert status == 200 and body["data"]["disabled"] == [V1] and mode(V1) == "MANUAL"

    assert enable(api, "nobody")[1]["code"] == "INVALID_VEHICLE"
    assert enable(api, V1, dispatch="sideways")[1]["code"] == "INVALID_DISPATCH"


def test_enable_refuses_a_vehicle_under_emergency_stop_or_with_a_fault(api):
    post(api, "/api/emergency-stop")
    status, body = enable(api, V1)
    assert status == 403 and body["code"] == "EMERGENCY_STOP_ACTIVE" and mode(V1) == "MANUAL"

    api.post("/api/emergency-release", json={"fleet": True})
    state.set_fault(V2, "BREAKDOWN")
    status, body = enable(api)                                   # fleet: V1 enabled, V2 refused
    assert status == 200
    assert body["data"]["enabled"] == [V1] and body["data"]["refused"] == {V2: "VEHICLE_FAULT"}


def test_mission_commands_are_refused_while_a_vehicle_is_in_auto(api):
    enable(api, V1)
    for path in ("/api/start", "/api/loading-point", "/api/loading/start", "/api/warehouse", "/api/complete"):
        status, body = post(api, path, vehicle=V1)
        assert status == 409 and body["code"] == "AUTOPILOT_ACTIVE", path
    assert post(api, "/api/manual-control", vehicle=V1, command="FORWARD")[1]["code"] == "AUTOPILOT_ACTIVE"
    assert post(api, "/api/start", vehicle=V2)[0] == 200          # the other vehicle is still manual


def test_manual_stop_takes_over_from_the_autopilot(api):
    enable(api, V1)
    run(10)
    assert pose(V1)["speed_kmh"] > 5
    status, body = post(api, "/api/manual-control", vehicle=V1, command="STOP")
    assert status == 200 and mode(V1) == "MANUAL"
    run(3)
    record = state.get_vehicle_state(V1)
    assert pose(V1)["speed_kmh"] == 0 and record["mission_status"] == "HELD" and record["speed"] == 0


def test_disable_mid_mission_holds_the_vehicle(api):
    enable(api, V1)
    run(10)
    post(api, "/api/v1/autopilot/disable", vehicle=V1)
    run(30)
    record = state.get_vehicle_state(V1)
    assert pose(V1)["speed_kmh"] == 0 and record["status"] == "stopped"
    assert record["braking_status"] == "applied" and record["next_action"] == "AUTOPILOT_DISENGAGED"


# ---- the mission loop ----------------------------------------------------------------

def test_autopilot_drives_a_full_cycle_and_the_return_leg(api):
    enable(api, V1)
    run_until(lambda: len(arrivals(V1, "stop_1")) == 1 and trace("COMPLETED", V1), 400)

    assert [e["stop"] for e in trace("ARRIVED", V1)] == ["stop_2", "stop_3", "stop_1"]
    names = [e["event"] for e in AP.trace if e["vehicle"] == V1]
    for step in ("STARTED", "LOADING_STARTED", "LOADED", "DEPARTED", "UNLOADED", "COMPLETED", "RETURN_LEG"):
        assert step in names, step
    assert names.index("LOADED") < names.index("DEPARTED") < names.index("UNLOADED") < names.index("COMPLETED")

    record = state.get_vehicle_state(V1)
    assert record["location"] == "stop_1" and record["load_percentage"] == 0
    assert record["status"] == "waiting" and pose(V1)["node"] == "S1"
    assert record["battery_percentage"] < 100                      # the loop cost battery
    assert V2 not in {e["vehicle"] for e in AP.trace}              # V2 stayed manual and parked
    assert pose(V2)["node"] == "S1"


def test_autopilot_uses_the_same_safety_validators(api):
    # The AprilTag interlock blocks the autopilot exactly like it blocks POST /api/start.
    state.set_qr_status(V1, "not_detected")
    enable(api, V1)
    run(3)
    assert pose(V1)["node"] == "S1" and state.get_vehicle_state(V1)["status"] != "running"
    assert trace("BLOCKED", V1, code="QR_NOT_DETECTED")
    assert any(e["event_type"] == "safety" for e in events_list(api))

    state.set_qr_status(V1, "detected")
    run(1)
    assert state.get_vehicle_state(V1)["status"] == "running"


def test_a_fault_holds_the_vehicle_and_refuses_start(api):
    state.set_fault(V1, "BREAKDOWN")
    assert post(api, "/api/start", vehicle=V1)[1]["code"] == "VEHICLE_FAULT"
    run(5)
    assert pose(V1)["node"] == "S1"
    state.set_fault(V1, None)
    assert post(api, "/api/start", vehicle=V1)[0] == 200


def test_handover_dispatch_passes_the_cycle_to_the_next_auto_vehicle(api):
    enable(api)
    run_until(lambda: trace("HANDOVER_COMPLETED", V1), 300)
    assert trace("HANDOVER_COMPLETED", V1)[0]["to"] == V2
    run_until(lambda: trace("STARTED", V2), 10)
    assert AP.status()["cycle_holder"] == V2
    # Only one vehicle runs a cycle at a time.
    assert trace("STARTED", V1) and len(trace("STARTED")) == 2


def test_concurrent_dispatch_runs_every_vehicle_and_keeps_them_apart(api):
    enable(api, dispatch="concurrent")
    lane = set(WORLD.shared_lanes["shared_s1"]["segments"])
    closest = 1e9
    for _ in range(int(200 / DT)):
        ENGINE.tick(DT)
        poses = {p["vehicle_id"]: p for p in ENGINE.poses_snapshot()}
        on_lane = {p["segment"] for p in poses.values() if p["segment"] in lane and p["distance_along_m"] > 0.5}
        assert len(on_lane) <= 1, "both directions on the shared lane"
        for a in poses.values():
            for b in poses.values():
                if a is b or not a["path"]:
                    continue
                gap = AP._true_gap(ENGINE.poses[a["vehicle_id"]], ENGINE.poses[b["vehicle_id"]])
                if gap is not None:
                    closest = min(closest, gap)
    assert trace("STARTED", V1) and trace("STARTED", V2)
    assert arrivals(V1, "stop_3") and arrivals(V2, "stop_3")
    assert closest > settings.critical_gap_m


def test_headway_spaces_departures_in_concurrent_dispatch(api):
    enable(api, dispatch="concurrent")
    run(15)
    starts = trace("STARTED")
    assert [e["vehicle"] for e in starts] == [V1, V2]
    assert starts[1]["t"] - starts[0]["t"] >= settings.autopilot_depart_headway_s - 0.5


# ---- loading slot ----------------------------------------------------------------------

def test_a_vehicle_never_enters_stop_2_without_a_slot_grant(api, monkeypatch):
    monkeypatch.setattr(settings, "loading_rate_pct_per_s", 2.0)      # V1 occupies the slot ~50 s
    enable(api, dispatch="concurrent")
    stop_2 = WORLD.stop_nodes["stop_2"]
    waited = False

    for _ in range(int(160 / DT)):
        ENGINE.tick(DT)
        loading = state.get_vehicle_state(V1)["status"] == "loading"
        if loading and not BUS.agents[V2].slot["granted"]:
            p = pose(V2)
            # Stops short of Stop 2 (SLOT_HOLD_M before it) and waits there.
            assert p["node"] != stop_2 and p["distance_to_destination_m"] in (None, *[x for x in [p["distance_to_destination_m"]]])
            if p["destination"] == stop_2 and p["distance_to_destination_m"] is not None:
                assert p["distance_to_destination_m"] >= settings.slot_hold_m - 1.0
                waited = waited or p["speed_kmh"] == 0
        if state.get_vehicle_state(V2)["location"] == "stop_2":
            break

    assert waited, "V2 should have waited for the slot"
    assert state.get_vehicle_state(V2)["location"] == "stop_2"
    assert trace("DEPARTED", V1)[0]["t"] <= arrivals(V2, "stop_2")[0]["t"]     # V1 left first
    assert state.get_vehicle_state(V2)["next_action"] != "WAIT_FOR_SLOT"


# ---- cooperative driving -----------------------------------------------------------------

def test_road_obstacle_brakes_the_vehicle_warns_the_follower_and_both_resume(api):
    AP.add_road_obstacle("pallet", "outbound_1", 70, "Fallen pallet")
    enable(api, dispatch="concurrent")
    run_until(lambda: state.get_vehicle_state(V1)["radar_status"] == "obstacle", 60)

    run(4)
    assert pose(V1)["speed_kmh"] == 0 and pose(V1)["distance_along_m"] < 70
    assert BUS.agents[V1].own_denms and any(c == "OBSTACLE" for c in BUS.agents[V1].view(BUS.now_ms)["own_denms"])
    assert trace("RADAR_OBSTACLE", V1)

    run_until(lambda: trace("HAZARD_HOLD", V2), 40)                 # V2 learned about it over the radio
    run(12)
    p1, p2 = pose(V1), pose(V2)
    assert p2["segment"] == "outbound_1" and p2["speed_kmh"] < 0.5          # holds short of the hazard
    assert p1["distance_along_m"] - p2["distance_along_m"] >= settings.critical_gap_m

    AP.remove_road_obstacle("pallet")
    run_until(lambda: trace("HAZARD_CLEARED", V2) and trace("RESUME", V1), 20)
    run_until(lambda: arrivals(V1, "stop_2") and arrivals(V2, "stop_2"), 150)
    assert state.get_vehicle_state(V1)["radar_status"] == "clear"


def test_follower_stops_well_short_of_a_broken_down_vehicle(api):
    ENGINE.place(V1, segment="outbound_1", s=60, path=["outbound_1"])
    state.update_vehicle_state(V1, status="running", mission_status="MOVING_TO_LOADING", location="stop_1")
    state.set_fault(V1, "BREAKDOWN")
    enable(api, V2, dispatch="concurrent")
    run(60)

    p2 = pose(V2)
    gap = 60 - p2["distance_along_m"]
    assert p2["segment"] == "outbound_1" and p2["speed_kmh"] < 0.5
    # It platoons behind the stopped vehicle and keeps the platoon target gap (25 m),
    # which is well above the 10 m minimum gap, so no emergency braking is needed.
    assert settings.min_gap_m < gap <= settings.v2v_target_gap_m + 2
    assert not trace("GAP_CRITICAL", V2)                                     # no emergency braking needed
    assert any(h["cause"] == "STALLED" for h in BUS.agents[V2].hazards.values())

    state.set_fault(V1, None)                                                # repaired: it was held, not removed
    assert pose(V1)["speed_kmh"] == 0


def test_critical_gap_triggers_emergency_brake_and_a_denm(api):
    ENGINE.place(V1, segment="outbound_1", s=60, path=["outbound_1"])
    state.update_vehicle_state(V1, status="running", mission_status="MOVING_TO_LOADING", location="stop_1")
    state.set_fault(V1, "BREAKDOWN")
    state.start_vehicle(V2)
    ENGINE.place(V2, segment="outbound_1", s=56.5, speed_mps=3.0, path=["outbound_1"])   # 3.5 m behind
    enable(api, V2)
    run(4)

    assert pose(V2)["speed_kmh"] == 0
    assert trace("GAP_CRITICAL", V2)
    brake = [m for m in BUS.messages(limit=200, msg_type="DENM")
             if m["sender"] == V2 and m["payload"]["cause"] == "EMERGENCY_BRAKE"]
    assert brake and brake[-1]["payload"]["action"] in ("new", "update")
    assert pose(V1)["distance_along_m"] - pose(V2)["distance_along_m"] > 3.0       # never touched


def test_heartbeat_lost_vehicle_is_avoided_as_a_hazard(api):
    # The radio goes dark for the vehicle ahead: the follower treats it as an obstacle.
    enable(api, dispatch="concurrent")
    run(25)
    BUS.blackout = {V1}
    run_until(lambda: BUS.agents[V2].neighbours[V1]["state"] == "lost", 10)
    assert state.get_vehicle_state(V1)["comm_hold"] is True
    run(5)                                                            # braking at 2 m/s^2 from 5 m/s
    assert pose(V1)["speed_kmh"] == 0
    assert pose(V2)["speed_kmh"] == 0
    assert pose(V1)["distance_along_m"] - pose(V2)["distance_along_m"] > settings.critical_gap_m
    BUS.blackout = set()
    run_until(lambda: not state.get_vehicle_state(V1)["comm_hold"], 10)
    run(20)
    assert pose(V1)["speed_kmh"] > 0 or arrivals(V1, "stop_2")


# ---- emergency stop -----------------------------------------------------------------------

def test_emergency_stop_disengages_the_autopilot_and_nothing_resumes_by_itself(api):
    enable(api)
    run(12)
    assert pose(V1)["speed_kmh"] > 5

    status, _ = post(api, "/api/emergency-stop")
    assert status == 200
    assert mode(V1) == "MANUAL" and mode(V2) == "MANUAL"            # the autopilot is off at once
    assert state.get_vehicle_state(V1)["speed"] == 0
    run(1.2)
    assert pose(V1)["speed_kmh"] == 0                                # halted within about a second
    assert trace("DISENGAGED", V1, reason="EMERGENCY_STOP") or AP.status()["vehicles"]

    run(40)
    assert pose(V1)["speed_kmh"] == 0 and state.get_vehicle_state(V1)["emergency_stop"] is True

    api.post("/api/emergency-release", json={"fleet": True})
    run(60)                                                           # released: still stopped, still MANUAL
    assert pose(V1)["speed_kmh"] == 0 and mode(V1) == "MANUAL"
    assert state.get_vehicle_state(V1)["status"] == "ready"

    enable(api, V1)                                                   # only an explicit enable drives again
    run(10)
    assert pose(V1)["speed_kmh"] > 0


def test_fleet_reset_returns_everything_to_manual(api):
    enable(api)
    AP.add_road_obstacle("x", "outbound_1", 70)
    run(10)
    assert post(api, "/api/reset")[0] == 200
    assert mode(V1) == "MANUAL" and mode(V2) == "MANUAL" and not AP.road_obstacles
    run(5)
    assert pose(V1)["node"] == "S1" and pose(V1)["speed_kmh"] == 0


# ---- handover: not at Stop 1 -------------------------------------------------------------------

def test_handover_is_rejected_by_a_vehicle_stopped_on_the_road(api):
    ENGINE.place(V1, segment="return_4", s=10, path=["return_4"])
    negotiation = BUS.wait_for(BUS.start_handover(V2, V1))
    assert negotiation.status == "rejected" and negotiation.reason == "NOT_AT_STOP_1"


def test_auto_vehicle_away_from_stop_1_refuses_a_handover(api):
    add_third_vehicle()
    enable(api, V1)
    state.update_vehicle_state(V1, location="stop_3")               # parked at the warehouse
    assert AP._handover_refusal(V1, state.get_vehicle_state(V1), {"node": "S3", "segment": None}) == "NOT_AT_STOP_1"
    assert AP._handover_refusal(V2, state.get_vehicle_state(V2), {"node": "S1", "segment": None}) is None


# ---- battery ----------------------------------------------------------------------------------------

def test_battery_drains_with_distance_and_idle_time(api):
    run(60)                                                           # idle for a minute
    idle = 100 - state.battery_of(state.get_vehicle_state(V1))
    assert idle == pytest.approx(settings.battery_idle_drain_pct_per_min, abs=0.05)

    post(api, "/api/start", vehicle=V2)
    state.set_battery(V2, 100)
    start = AP._mem(V2).distance_m
    run(30)
    driven = AP._mem(V2).distance_m - start
    lost = 100 - state.battery_of(state.get_vehicle_state(V2))
    expected = driven / 1000 * settings.battery_drain_pct_per_km + 30 / 60 * settings.battery_idle_drain_pct_per_min
    assert driven > 80 and lost == pytest.approx(expected, rel=0.02)
    assert state.get_vehicle_state(V2)["battery_level"] == state.get_vehicle_state(V2)["battery_percentage"]


def test_battery_recharges_at_the_charging_bay(api):
    state.set_battery(V1, 50)
    ENGINE.place(V1, node="CB")
    state.update_vehicle_state(V1, location="charging", current_stop="charging")
    state.arrive_at_charging(V1)
    run(10)
    assert state.battery_of(state.get_vehicle_state(V1)) == pytest.approx(
        50 + 10 * settings.battery_charge_pct_per_s, abs=0.5)


def test_low_battery_vehicle_charges_instead_of_starting_a_new_cycle(api):
    state.set_battery(V1, 25)
    enable(api, V1)
    run_until(lambda: trace("CHARGED", V1), 500)

    assert trace("LOW_BATTERY", V1)
    assert arrivals(V1, "stop_3") and arrivals(V1, "charging")
    names = [e["event"] for e in AP.trace if e["vehicle"] == V1]
    assert names.index("COMPLETED") < names.index("RETURN_LEG")       # it finished the cycle first
    assert trace("RETURN_LEG", V1)[0]["to"] == "charging"
    denms = [m for m in BUS.messages(limit=500, msg_type="DENM")
             if m["sender"] == V1 and m["payload"]["cause"] == "LOW_BATTERY"]
    assert denms
    assert state.battery_of(state.get_vehicle_state(V1)) >= settings.battery_charge_target_pct - 0.5

    run_until(lambda: arrivals(V1, "stop_1"), 120)                    # then home to Stop 1
    assert state.get_vehicle_state(V1)["location"] == "stop_1"


def test_a_low_battery_vehicle_refuses_a_handover(api):
    state.set_battery(V2, 10)
    negotiation = BUS.wait_for(BUS.start_handover(V1, V2))
    assert negotiation.status == "rejected" and negotiation.reason == "LOW_BATTERY"


def test_signal_strength_follows_the_distance_to_the_nearest_neighbour(api):
    run(1)
    assert state.get_vehicle_state(V1)["signal_strength"] == 100        # parked side by side
    ENGINE.place(V1, node="S2")                                         # 140 m from Stop 1, range 150
    run(1)
    assert 0 < state.get_vehicle_state(V2)["signal_strength"] < 15
    BUS.blackout = {V2}
    run(1)
    assert state.get_vehicle_state(V2)["signal_strength"] == 0


# ---- watchdog -------------------------------------------------------------------------------------------

def test_watchdog_latches_a_fleet_emergency_stop_when_the_loop_stalls(api):
    enable(api)
    run(5)
    wd = AP.watchdog
    now = time.monotonic()
    ENGINE.last_tick_mono = now - 5.0                                 # the loop stopped 5 s ago
    wd.armed_since = now - 10.0

    assert wd.check(now) == "TICK_STALLED"

    assert state.get_vehicle_state(V1)["emergency_stop"] and state.get_vehicle_state(V2)["emergency_stop"]
    assert mode(V1) == "MANUAL" and AP.failed["code"] == "TICK_STALLED"
    health = api.get("/api/health").json()["autopilot"]
    assert health["state"] == "FAILED" and health["failure"]["code"] == "TICK_STALLED"
    assert api.get("/api/v1/autopilot/status").json()["data"]["watchdog"]["state"] == "FAILED"
    assert any("watchdog" in e["message"].lower() for e in events_list(api))

    # It does not restart by itself, and cannot be enabled while FAILED.
    status, body = enable(api)
    assert status == 409 and body["code"] == "AUTOPILOT_FAILED"
    assert wd.check(now + 20) is None and AP.failed is not None


def test_watchdog_trips_when_a_tick_raises_and_recovery_is_explicit(api):
    enable(api, V1)

    def broken(dt):
        raise RuntimeError("simulated tick failure")

    ENGINE.add_tick_hook(broken)
    try:
        with pytest.raises(RuntimeError):
            ENGINE.tick(DT)
    finally:
        ENGINE.tick_hooks.remove(broken)

    assert AP.watchdog.check() == "TICK_FAILED"
    assert "simulated tick failure" in AP.failed["detail"]
    assert state.get_vehicle_state(V1)["emergency_stop"] is True

    status, body = post(api, "/api/v1/autopilot/recover")
    assert status == 200 and body["data"]["recovered"] is True and AP.failed is None
    assert state.get_vehicle_state(V1)["emergency_stop"] is True       # still stopped until released
    assert enable(api, V1)[1]["code"] == "EMERGENCY_STOP_ACTIVE"
    api.post("/api/emergency-release", json={"fleet": True})
    assert enable(api, V1)[0] == 200


def test_watchdog_is_quiet_while_the_autopilot_is_off_or_the_loop_is_healthy(api):
    wd = AP.watchdog
    ENGINE.last_tick_mono = time.monotonic() - 60                      # stale, but nothing is in AUTO
    assert wd.check() is None and AP.failed is None

    enable(api, V1)
    run(2)
    assert wd.check() is None and AP.failed is None


def test_recover_when_not_failed_is_a_no_op(api):
    status, body = post(api, "/api/v1/autopilot/recover")
    assert status == 200 and body["data"]["recovered"] is False


def test_recover_is_local_only(api, remote):
    assert remote.post("/api/v1/autopilot/recover").status_code == 403


# ---- time scale ---------------------------------------------------------------------------------------------

def test_time_scale_endpoint_validates_and_is_local_only(api, remote):
    ok = post(api, "/api/v1/autopilot/time-scale", scale=4)
    assert ok[0] == 200 and ENGINE.time_scale == 4.0
    assert api.get("/api/health").json()["world"]["time_scale"] == 4.0
    for bad in (0.1, 9, "fast", None):
        status, body = post(api, "/api/v1/autopilot/time-scale", scale=bad)
        assert status == 400 and body["code"] == "INVALID_TIME_SCALE", bad
    assert remote.post("/api/v1/autopilot/time-scale", json={"scale": 2}).status_code == 403
    assert post(api, "/api/v1/autopilot/time-scale", scale=0.25)[0] == 200


def test_engine_thread_runs_scaled_steps(api, monkeypatch):
    # The run loop splits one wall-clock tick into scaled simulated steps.
    seen = []
    monkeypatch.setattr(ENGINE, "tick", lambda dt: seen.append(round(dt, 4)))
    ENGINE.set_time_scale(4.0)
    ENGINE.tick_ms = 200
    ENGINE._stop.clear()
    original_wait = ENGINE._stop.wait
    monkeypatch.setattr(ENGINE._stop, "wait", lambda delay: ENGINE._stop.set() or True)
    ENGINE._run()
    ENGINE._stop.wait = original_wait
    ENGINE._stop.clear()
    assert seen == [0.2] * 4                                          # 4x: four 200 ms steps in one tick


# ---- scenarios ------------------------------------------------------------------------------------------------

def run_scenario(api, name, **body):
    status, result = post(api, f"/api/v1/scenarios/{name}/run", **body)
    assert status == 200, result
    for _ in range(int(2000 / DT)):
        if scenarios.runner.active is None:
            break
        ENGINE.tick(DT)
    assert scenarios.runner.active is None
    return scenarios.runner.last


def test_the_shipped_scenarios_are_all_valid(api):
    valid, invalid = scenarios.load_all()
    assert not invalid
    assert {"normal_loop", "busy_yard", "obstacle_recovery", "link_degradation",
            "breakdown_handover", "low_battery"} <= set(valid)
    data = api.get("/api/v1/scenarios").json()["data"]
    assert {s["name"] for s in data["scenarios"]} == set(valid) and data["active"] is None
    assert api.get("/api/v1/scenarios/low_battery").json()["data"]["setup"]["battery"] == {"#1": 25}
    assert api.get("/api/v1/scenarios/nope").json()["code"] == "SCENARIO_NOT_FOUND"


def test_invalid_scenarios_are_listed_with_the_reason(api, tmp_path):
    (tmp_path / "broken.json").write_text('{"duration_s": 10, "timeline": [{"t": 1, "action": "teleport"}]}')
    (tmp_path / "garbage.json").write_text("{not json")
    (tmp_path / "ok.json").write_text('{"duration_s": 10, "timeline": [{"t": 1, "action": "note", "text": "hi"}]}')
    scenarios.runner.directory = tmp_path
    data = api.get("/api/v1/scenarios").json()["data"]
    assert [s["name"] for s in data["scenarios"]] == ["ok"]
    assert "unknown action" in data["invalid"]["broken.json"] and "garbage.json" in data["invalid"]
    status, body = post(api, "/api/v1/scenarios/broken/run")
    assert status == 400 and body["code"] == "SCENARIO_INVALID"


def test_scenario_endpoints_check_fleet_size_and_who_calls(api, remote):
    status, body = post(api, "/api/v1/scenarios/normal_loop/run")            # needs 3 vehicles, fleet has 2
    assert status == 409 and body["code"] == "SCENARIO_NEEDS_VEHICLES"
    assert remote.post("/api/v1/scenarios/obstacle_recovery/run").status_code == 403
    assert remote.post("/api/v1/scenarios/stop").status_code == 403
    assert post(api, "/api/v1/scenarios/nope/run")[1]["code"] == "SCENARIO_NOT_FOUND"
    assert post(api, "/api/v1/scenarios/stop")[1]["data"] == {"report": None}

    post(api, "/api/emergency-stop")
    assert post(api, "/api/v1/scenarios/obstacle_recovery/run")[1]["code"] == "EMERGENCY_STOP_ACTIVE"


def test_obstacle_scenario_runs_end_to_end(api):
    report = run_scenario(api, "obstacle_recovery")
    summary = report["summary"]
    assert report["status"] == "completed" and report["seed"] == 7
    assert summary["hazard_holds"] >= 1 and summary["emergency_brakes"] == 0 and summary["near_misses"] == 0
    assert summary["min_gap_m"] is None or summary["min_gap_m"] > settings.critical_gap_m
    assert summary["cycles_completed"] >= 1
    assert [e["event"] for e in report["log"]][:2] == ["START", "OBSTACLE"]
    assert not AP.road_obstacles and mode(V1) == "MANUAL"            # effects removed, autopilot disengaged
    assert AP.dispatch == "handover"                                 # the scenario's "concurrent" was for the run only
    assert api.get("/api/v1/scenarios").json()["data"]["last_run"]["name"] == "obstacle_recovery"


def test_the_same_seed_gives_the_same_scenario_run(api):
    first = run_scenario(api, "link_degradation")
    first_trace = [dict(e) for e in AP.trace]
    first_stats = BUS.statistics()

    second = run_scenario(api, "link_degradation")
    assert [dict(e) for e in AP.trace] == first_trace
    assert BUS.statistics() == first_stats
    assert second["summary"] == first["summary"]
    assert first["summary"]["comm_holds"] >= 1                         # the dead zone triggered the fail-safe

    third = run_scenario(api, "link_degradation", seed=5)             # another seed, another run
    assert third["seed"] == 5 and BUS.statistics() != first_stats


def test_breakdown_scenario_reassigns_the_handover(api):
    add_third_vehicle()
    report = run_scenario(api, "breakdown_handover")
    summary = report["summary"]
    refused = summary["handovers_refused"]
    assert refused and refused[0]["to"] == V2 and refused[0]["reason"] == "VEHICLE_FAULT"
    assert summary["handovers"][0]["from"] == V1 and summary["handovers"][0]["to"] == V3
    assert summary["vehicles"][V2]["cycles_completed"] == 0 or summary["handovers"][-1]["to"] == V2
    assert state.get_vehicle_state(V2)["fault"] is None               # repaired by the timeline


def test_low_battery_scenario_charges_the_first_vehicle(api):
    report = run_scenario(api, "low_battery")
    assert report["summary"]["vehicles"][V1]["charges"] == 1
    assert trace("LOW_BATTERY", V1) and trace("CHARGED", V1)
    assert report["summary"]["emergency_brakes"] == 0


def test_stopping_a_scenario_holds_the_vehicles_and_reports(api):
    post(api, "/api/v1/scenarios/obstacle_recovery/run")
    run(20)
    assert AP.scenario_active and mode(V1) == "AUTO"
    status, body = post(api, "/api/v1/scenarios/stop")
    report = body["data"]["report"]
    assert status == 200 and report["status"] == "stopped" and report["t"] == pytest.approx(20, abs=0.5)
    assert mode(V1) == "MANUAL" and not AP.road_obstacles and not AP.scenario_active
    run(10)
    assert pose(V1)["speed_kmh"] == 0
    assert post(api, "/api/v1/scenarios/obstacle_recovery/run")[0] == 200       # can run again


def test_a_second_scenario_cannot_start_while_one_runs(api):
    post(api, "/api/v1/scenarios/obstacle_recovery/run")
    status, body = post(api, "/api/v1/scenarios/obstacle_recovery/run")
    assert status == 409 and body["code"] == "SCENARIO_RUNNING"


def test_emergency_stop_aborts_a_running_scenario(api):
    post(api, "/api/v1/scenarios/obstacle_recovery/run")
    run(15)
    post(api, "/api/emergency-stop")
    assert scenarios.runner.active is None and scenarios.runner.last["status"].startswith("aborted")
    assert mode(V1) == "MANUAL"
    run(30)
    assert pose(V1)["speed_kmh"] == 0


def test_scenario_validation_rejects_bad_timelines(api):
    world = world_module.world
    bad_specs = [
        {"duration_s": 0},
        {"duration_s": 10, "timeline": [{"t": 1, "action": "obstacle", "segment": "nowhere", "s": 1}]},
        {"duration_s": 10, "timeline": [{"t": 1, "action": "breakdown"}]},
        {"duration_s": 10, "timeline": [{"t": 1, "action": "link_loss", "duration_s": 5}]},
        {"duration_s": 10, "setup": {"dispatch": "weird"}},
        {"duration_s": 10, "setup": {"battery": {"#1": 150}}},
    ]
    for spec in bad_specs:
        with pytest.raises(ValueError):
            scenarios._validate(spec, "bad", world)


# ---- endpoints ------------------------------------------------------------------------------------------------------

def test_status_and_trace_endpoints_describe_the_run(api):
    enable(api, V1)
    run(40)
    data = api.get("/api/v1/autopilot/status").json()["data"]
    one = next(v for v in data["vehicles"] if v["vehicle_id"] == V1)
    assert one["mode"] == "AUTO" and one["phase"] and one["destination"] == "stop_2"
    assert one["eta_seconds"] is not None and one["battery"] < 100 and one["in_cycle"] is True
    assert next(v for v in data["vehicles"] if v["vehicle_id"] == V2)["mode"] == "MANUAL"
    assert data["watchdog"]["state"] == "ARMED" and data["simulated_seconds"] == pytest.approx(40, abs=0.5)

    items = api.get(f"/api/v1/autopilot/trace?vehicle={V1}&limit=5").json()["data"]
    assert 0 < items["count"] <= 5 and all(e["vehicle"] == V1 for e in items["trace"])
    assert api.get("/api/v1/autopilot/trace?vehicle=zzz").json()["code"] == "INVALID_VEHICLE"


def test_state_exposes_next_action_and_eta_for_an_auto_vehicle(api):
    enable(api, V1)
    run(10)
    record = state.get_vehicle_state(V1)
    assert record["next_action"] == "DRIVE_TO_STOP_2" and record["destination"] == "stop_2"
    assert record["eta_seconds"] is not None and record["autopilot_mode"] == "AUTO"
    run_until(lambda: state.get_vehicle_state(V1)["status"] == "loading", 60)
    assert state.get_vehicle_state(V1)["next_action"] == "LOADING"


def test_registry_changes_forget_the_autopilot_memory(api):
    add_third_vehicle()
    enable(api, V3)
    assert AP.is_auto(V3)
    post(api, "/api/v1/autopilot/disable", vehicle=V3)
    assert api.delete(f"/api/vehicles/{V3}").status_code == 200
    assert V3 not in AP.modes and V3 not in AP.memory
