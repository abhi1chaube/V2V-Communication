"""Phase 3 tests: the V2V message bus, the heartbeat fail-safe, and the protocols.

The world engine's thread is off in tests; engine.tick(dt) moves the vehicles
and runs the bus (it is a tick hook), so every run is deterministic (SIM_SEED).
"""

import pytest

from services import fleet, simrng, state, v2v, world as world_module

ENGINE = world_module.engine
WORLD = world_module.world
BUS = v2v.bus
V1, V2, V3 = "Vehicle_01", "Vehicle_02", "Vehicle_03"
DT = 0.2


def run(seconds):
    for _ in range(int(round(seconds / DT))):
        ENGINE.tick(DT)


def post(api, path, **body):
    response = api.post(path, json=body)
    return response.status_code, response.json()


def deliveries(msg_type=None, sender=None):
    return [
        (m, d) for m in BUS.messages(limit=v2v.HISTORY_SIZE, msg_type=msg_type)
        if sender is None or m["sender"] == sender
        for d in m["deliveries"]
    ]


def to_warehouse(api, vehicle=V1):
    for path in ("/api/start", "/api/loading-point", "/api/loading/start"):
        assert post(api, path, vehicle=vehicle)[0] == 200, path
    assert post(api, "/api/loading/update", vehicle=vehicle, percentage=100)[0] == 200
    assert post(api, "/api/warehouse", vehicle=vehicle)[0] == 200


RUNNING = {"status": "running", "speed": 18, "braking_status": "not_applied",
           "motor_direction": "straight", "mission_status": "IN_PROGRESS"}


# ---- transport ------------------------------------------------------------------

def test_cam_heartbeats_connect_neighbours(api):
    run(2.0)
    cams = [m for m in BUS.messages(limit=100, msg_type="CAM")]
    assert {m["sender"] for m in cams} == {V1, V2}
    assert len([m for m in cams if m["sender"] == V1]) in (4, 5)        # every 500 ms
    assert BUS.agents[V2].neighbours[V1]["state"] == "connected"
    assert cams[0]["payload"]["stop"] == "stop_1" and "signature_stub" in cams[0]


def test_messages_beyond_range_are_not_delivered(api):
    BUS.update_config({"comm_range_m": 50, "rsu_enabled": False})
    ENGINE.place(V1, node="S2")                                      # 140 m from Stop 1
    run(1.0)
    outcomes = {d["outcome"] for m, d in deliveries("CAM", V1) if d["to"] == V2}
    assert outcomes == {"out_of_range"}
    assert BUS.statistics()["out_of_range"] > 0
    assert BUS.agents[V2].neighbours[V1]["state"] == "out_of_range"


def test_loss_rate_drops_messages(api):
    BUS.update_config({"loss_rate": 1.0})
    run(1.0)
    outcomes = {d["outcome"] for _, d in deliveries("CAM")}
    assert outcomes == {"lost"} and BUS.statistics()["delivered"] == 0


def test_latency_within_limits_and_relay_adds_a_hop(api):
    run(1.0)
    for _, d in deliveries("CAM"):
        if d["outcome"] == "delivered":
            assert 20 <= d["latency_ms"] <= 80

    ENGINE.place(V1, node="S3")                                      # 260 m apart: relay needed
    message = BUS.send(V1, V2, "ACK", {"note": "relay test"})
    delivery = message.deliveries[0]
    assert delivery["via"] == "RSU1" and 40 <= delivery["latency_ms"] <= 160


def test_broadcasts_are_not_relayed(api):
    ENGINE.place(V1, node="S3")
    message = BUS.send(V1, v2v.BROADCAST, "CAM", {"x": 260})
    to_v2 = [d for d in message.deliveries if d["to"] == V2][0]
    assert to_v2["outcome"] == "out_of_range" and to_v2["via"] is None


def test_duplicates_and_stale_messages_are_rejected(api):
    BUS.send(V1, V2, "ACK", {}, msg_id="fixed-id-1")
    BUS.send(V1, V2, "ACK", {}, msg_id="fixed-id-1")
    stale = BUS.send(V1, V2, "ACK", {}, ttl_ms=1)
    run(0.4)
    outcomes = [m["deliveries"][0]["outcome"] for m in BUS.messages(limit=10, msg_type="ACK")]
    assert outcomes.count("duplicate") == 1 and "delivered" in outcomes
    assert stale.deliveries[0]["outcome"] == "stale"
    stats = BUS.statistics()
    assert stats["duplicate"] == 1 and stats["stale"] == 1


def test_runs_repeat_exactly_with_the_same_seed(api):
    def one_run():
        simrng.reset(99)
        BUS.reset()
        BUS.update_config({"loss_rate": 0.3})
        ENGINE.poses.clear()
        run(3.0)
        return [(m["msg_id"], [d["outcome"] for d in m["deliveries"]]) for m in BUS.messages(limit=200)]

    assert one_run() == one_run()


# ---- heartbeat fail-safe ----------------------------------------------------------

def test_silence_in_range_is_lost_and_the_vehicle_holds(api):
    run(2.0)
    BUS.update_config({"loss_rate": 1.0})
    run(2.5)

    assert BUS.agents[V2].neighbours[V1]["state"] == "lost"
    assert state.get_vehicle_state(V1)["comm_hold"] is True
    assert any(h["cause"] == "HEARTBEAT_LOST" and h["about"] == V2 for h in BUS.agents[V1].hazards.values())
    assert any(c.startswith("HEARTBEAT_LOST") for c in BUS.agents[V1].view(BUS.now_ms)["own_denms"])

    blocked = post(api, "/api/start", vehicle=V1)
    assert blocked[0] == 403 and blocked[1]["code"] == "V2V_COMM_LOST"

    BUS.update_config({"loss_rate": 0.0})
    run(1.5)
    assert state.get_vehicle_state(V1)["comm_hold"] is False
    assert BUS.agents[V1].neighbours[V2]["state"] == "connected"
    assert post(api, "/api/start", vehicle=V1)[0] == 200


def test_a_moving_vehicle_brakes_when_contact_is_lost(api):
    run(1.0)
    post(api, "/api/start", vehicle=V1)
    run(8.0)
    assert ENGINE.pose(V1)["speed_kmh"] > 5
    BUS.update_config({"loss_rate": 1.0})
    run(4.0)
    assert ENGINE.pose(V1)["speed_kmh"] == 0
    assert state.get_vehicle_state(V1)["next_action"] == "HOLD_COMM_LOST"


def test_silence_out_of_range_is_not_lost(api):
    ENGINE.place(V1, node="S3")                                      # 260 m away
    run(4.0)
    assert BUS.agents[V2].neighbours[V1]["state"] == "out_of_range"
    assert state.get_vehicle_state(V2)["comm_hold"] is False


# ---- DENM hazard warnings ---------------------------------------------------------

def test_obstacle_denm_is_raised_and_cancelled(api):
    run(1.0)
    post(api, "/api/sensors/radar", vehicle=V1, obstacle=True)
    run(1.0)
    hazards = BUS.agents[V2].hazards.values()
    assert any(h["cause"] == "OBSTACLE" and h["origin"] == V1 for h in hazards)

    post(api, "/api/sensors/radar", vehicle=V1, obstacle=False)
    run(1.0)
    assert not any(h["cause"] == "OBSTACLE" for h in BUS.agents[V2].hazards.values())
    actions = [m["payload"]["action"] for m in BUS.messages(limit=100, msg_type="DENM")
               if m["payload"]["cause"] == "OBSTACLE"]
    assert "new" in actions and "cancel" in actions


def test_emergency_stop_while_moving_raises_stop_and_hard_brake(api):
    run(1.0)
    post(api, "/api/start", vehicle=V1)
    run(10.0)
    post(api, "/api/emergency-stop", fleet=True)
    run(1.0)
    causes = {m["payload"]["cause"] for m in BUS.messages(limit=200, msg_type="DENM", vehicle=V1)
              if m["sender"] == V1}
    assert {"EMERGENCY_STOP", "EMERGENCY_BRAKE"} <= causes


def test_bsm_goes_out_on_the_radio(api):
    run(0.4)
    post(api, "/api/sensors/brake", vehicle=V1, applied=False)
    bsms = BUS.messages(limit=20, msg_type="BSM")
    assert bsms and bsms[0]["sender"] == V1 and bsms[0]["payload"]["messageId"] == 20


# ---- negotiated handover -----------------------------------------------------------

def test_handover_is_negotiated_over_the_bus(api):
    to_warehouse(api)
    status, body = post(api, "/api/complete", vehicle=V1)
    handover = body["data"]["handover"]
    assert status == 200 and handover["status"] == "completed"
    assert [m["msg_type"] for m in handover["messages"]] == [
        "HANDOVER_REQUEST", "HANDOVER_ACCEPT", "HANDOVER_COMPLETE"]
    assert all(m["via"] == "RSU1" for m in handover["messages"])           # 260 m apart
    assert state.get_vehicle_state(V2)["handover_status"] == "ready"


def test_handover_rejected_when_next_vehicle_is_busy(api):
    to_warehouse(api)
    post(api, "/api/start", vehicle=V2)                                      # V2 in a mission
    status, body = post(api, "/api/complete", vehicle=V1)
    assert status == 200
    assert body["data"]["handover"]["status"] == "rejected"
    assert body["data"]["handover"]["reason"] == "BUSY_IN_MISSION"
    assert state.get_vehicle_state(V1)["mission_status"] == "COMPLETED"    # the journey still ends
    assert state.get_vehicle_state(V2)["status"] == "running"              # untouched


def test_handover_times_out_after_one_retry(api):
    to_warehouse(api)
    BUS.update_config({"loss_rate": 1.0})
    _, body = post(api, "/api/complete", vehicle=V1)
    handover = body["data"]["handover"]
    assert handover["status"] == "timeout" and handover["request_attempts"] == 2


def test_handover_retry_recovers_a_lost_request(api, monkeypatch):
    to_warehouse(api)
    BUS.update_config({"loss_rate": 0.0})        # only the injected loss below
    original = BUS._schedule
    dropped = []

    def lose_first_request(message, receiver, positions):
        if message.msg_type == "HANDOVER_REQUEST" and not dropped:
            dropped.append(message.msg_id)
            message.deliveries.append({"to": receiver, "outcome": "lost", "via": None, "latency_ms": None})
            return
        original(message, receiver, positions)

    monkeypatch.setattr(BUS, "_schedule", lose_first_request)
    _, body = post(api, "/api/complete", vehicle=V1)
    handover = body["data"]["handover"]
    assert dropped and handover["status"] == "completed" and handover["request_attempts"] == 2


# ---- loading slot ------------------------------------------------------------------

def test_loading_slot_is_granted_to_one_vehicle_at_a_time(api):
    run(0.6)
    post(api, "/api/start", vehicle=V1)
    run(1.0)
    assert BUS.dock().holder == V1

    post(api, "/api/start", vehicle=V2)
    run(1.5)
    assert BUS.agents[V2].slot["granted"] is False and BUS.agents[V2].slot["queue_position"] == 1

    blocked = post(api, "/api/loading-point", vehicle=V2)
    assert blocked[0] == 403 and blocked[1]["code"] == "LOADING_SLOT_BUSY"

    assert post(api, "/api/loading-point", vehicle=V1)[0] == 200
    post(api, "/api/loading/start", vehicle=V1)
    post(api, "/api/loading/update", vehicle=V1, percentage=100)
    post(api, "/api/warehouse", vehicle=V1)
    run(2.0)                                                              # RSU sees V1 leave
    assert BUS.dock().holder == V2 and BUS.agents[V2].slot["granted"] is True


def test_occupied_loading_point_is_refused_even_without_a_grant(api):
    BUS.update_config({"rsu_enabled": False})
    post(api, "/api/start", vehicle=V1)
    post(api, "/api/loading-point", vehicle=V1)
    post(api, "/api/start", vehicle=V2)
    blocked = post(api, "/api/loading-point", vehicle=V2)
    assert blocked[0] == 403 and blocked[1]["code"] == "LOADING_SLOT_BUSY"


# ---- shared lane right-of-way -------------------------------------------------------------

def test_shared_lane_is_never_used_in_both_directions_at_once(api):
    run(1.0)
    state.update_vehicle_state(V2, **{**RUNNING, "speed": 12})
    ENGINE.place(V2, segment="return_4", s=50, speed_mps=3.0, path=["return_4", "arrive"])
    post(api, "/api/start", vehicle=V1)

    lane = set(WORLD.shared_lanes["shared_s1"]["segments"])
    for _ in range(int(60 / DT)):
        ENGINE.tick(DT)
        on_lane = {p["segment"] for p in ENGINE.poses_snapshot()
                   if p["segment"] in lane and p["distance_along_m"] > 0.5}
        assert len(on_lane) <= 1, f"both directions on the shared lane: {on_lane}"

    assert ENGINE.pose(V2)["node"] == "S1"                     # the returning vehicle got home
    assert ENGINE.pose(V1)["segment"] != "depart"              # the departing one went after it
    assert BUS.messages(limit=50, msg_type="YIELD")


# ---- platoon ---------------------------------------------------------------------------------

def test_follower_keeps_the_gap_and_stops_behind_the_leader(api):
    run(1.0)
    state.update_vehicle_state(V1, **{**RUNNING, "speed": 9})            # slow leader
    state.update_vehicle_state(V2, **RUNNING)                            # faster follower
    ENGINE.place(V1, segment="outbound_1", s=40, speed_mps=2.5, path=["outbound_1"])
    ENGINE.place(V2, segment="outbound_1", s=10, speed_mps=5.0, path=["outbound_1"])

    closest = 1e9
    for _ in range(int(45 / DT)):
        ENGINE.tick(DT)
        p1, p2 = ENGINE.pose(V1), ENGINE.pose(V2)
        if p2["segment"] == "outbound_1" and p1["segment"] == "outbound_1":
            closest = min(closest, p1["distance_along_m"] - p2["distance_along_m"])

    assert BUS.agents[V2].platoon["role"] == "follower" and BUS.agents[V2].platoon["leader"] == V1
    assert BUS.messages(limit=20, msg_type="GAP_ADVISORY")
    assert closest > 12                                              # never closes in
    assert ENGINE.pose(V1)["node"] == "S2"
    assert ENGINE.pose(V2)["distance_to_destination_m"] >= 15       # waits behind, not on top


# ---- endpoints ---------------------------------------------------------------------------------

def test_v2v_read_endpoints(api):
    run(2.0)
    messages = api.get("/api/v1/v2v/messages?type=CAM&vehicle=1&limit=3").json()["data"]
    assert messages["count"] == 3 and all(m["msg_type"] == "CAM" for m in messages["messages"])
    assert api.get("/api/v1/v2v/messages?type=NOPE").json()["code"] == "INVALID_MESSAGE_TYPE"

    links = api.get("/api/v1/v2v/links").json()["data"]["links"]
    assert any(l["from"] == V1 and l["to"] == V2 and l["state"] == "connected" for l in links)

    inbox = api.get("/api/v1/v2v/inbox/2").json()["data"]
    assert inbox["count"] > 0 and inbox["messages"][0]["sender"] in (V1, "RSU1")

    agents = api.get("/api/v1/v2v/agents").json()["data"]
    assert {a["vehicle_id"] for a in agents["vehicles"]} == {V1, V2}
    assert agents["rsus"][0]["id"] == "RSU1"

    stats = api.get("/api/v1/v2v/stats").json()["data"]
    assert stats["sent"] > 0 and stats["by_type"]["CAM"] > 0


def test_config_endpoint_validates_and_is_local_only(api, remote):
    ok = api.post("/api/v1/v2v/config", json={"loss_rate": 0.25, "comm_range_m": 90})
    assert ok.status_code == 200 and ok.json()["data"]["loss_rate"] == 0.25
    bad = api.post("/api/v1/v2v/config", json={"loss_rate": 2})
    assert bad.status_code == 400 and bad.json()["code"] == "INVALID_V2V_CONFIG"
    swapped = api.post("/api/v1/v2v/config", json={"latency_min_ms": 90, "latency_max_ms": 10})
    assert swapped.status_code == 400
    assert remote.post("/api/v1/v2v/config", json={"loss_rate": 0.5}).status_code == 403


def test_injected_denm_reaches_neighbours_and_injection_is_local_only(api, remote):
    run(1.0)
    sent = api.post("/api/v1/v2v/send", json={
        "sender": "1", "recipient": "*", "msg_type": "DENM",
        "payload": {"denm_id": "demo-1", "action": "new", "cause": "OBSTACLE", "about": V1}})
    assert sent.status_code == 200
    run(0.4)
    assert "demo-1" in BUS.agents[V2].hazards

    assert remote.post("/api/v1/v2v/send", json={"sender": "1", "msg_type": "CAM"}).status_code == 403
    assert api.post("/api/v1/v2v/send", json={"sender": "1", "msg_type": "NOPE"}).json()["code"] == "INVALID_MESSAGE_TYPE"
    assert api.post("/api/v1/v2v/send", json={"sender": "zzz", "msg_type": "CAM"}).json()["code"] == "INVALID_SENDER"


def test_communication_endpoint_reports_the_radio(api):
    run(2.0)
    data = api.get("/api/communication").json()["data"]
    assert data["vehicle_statuses"] == {V1: "connected", V2: "connected"}
    detail = data["details"][V1]
    assert detail["neighbours"] == 1 and detail["messages_per_second"] > 0 and detail["rsu_link"] is True

    BUS.update_config({"loss_rate": 1.0})
    run(2.5)
    data = api.get("/api/communication").json()["data"]
    assert set(data["vehicle_statuses"].values()) == {"lost"} and data["connected"] is False


def test_registry_delete_forgets_the_agent(api):
    fleet.add_vehicle(V3, "00000000-0000-4000-8000-000000000003", "FAKE_TOKEN_0003")
    run(1.0)
    assert V3 in BUS.agents
    assert api.delete(f"/api/vehicles/{V3}").status_code == 200
    assert V3 not in BUS.agents and all(V3 not in k for k in BUS._links)
