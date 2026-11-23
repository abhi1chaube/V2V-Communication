"""Ordered API contract cases.

The cases run in this order against ONE fresh backend process,
so later cases depend on the state left by earlier ones (for
example, the loading cases need the vehicle at the loading
point). Do not reorder casually.

Each case: (name, method, path, options)
  options.json     JSON body
  options.raw      raw body string (sent as text/plain)
  options.remote   True = request from a non-local address
"""

FAKE_V3_DEVICE = "00000000-0000-4000-8000-000000000003"

CASES = [
    # ---- root, health, hardware ---------------------------------
    ("root", "GET", "/", {}),
    ("health", "GET", "/api/health", {}),
    ("hardware", "GET", "/api/hardware", {}),
    ("unknown_route", "GET", "/api/does-not-exist", {}),

    # ---- registry (read) ----------------------------------------
    ("vehicles_list", "GET", "/api/vehicles", {}),
    ("vehicles_search", "GET", "/api/vehicles?search=02", {}),
    ("vehicles_search_none", "GET", "/api/vehicles?search=zzz", {}),
    ("vehicle_get_id", "GET", "/api/vehicles/Vehicle_01", {}),
    ("vehicle_get_alias", "GET", "/api/vehicles/yellow", {}),
    ("vehicle_get_invalid", "GET", "/api/vehicles/Vehicle_99", {}),
    ("fleet", "GET", "/api/fleet", {}),
    ("tb_quota", "GET", "/api/thingsboard/quota", {}),
    ("tb_test", "GET", "/api/thingsboard/test", {}),

    # ---- command input validation -------------------------------
    ("start_no_vehicle", "POST", "/api/start", {"json": {}}),
    ("start_bad_vehicle", "POST", "/api/start", {"json": {"vehicle": "nope"}}),
    ("start_non_json", "POST", "/api/start", {"raw": "not json"}),
    ("load_missing_pct", "POST", "/api/loading/update", {"json": {"vehicle": "Vehicle_01"}}),
    ("load_text_pct", "POST", "/api/loading/update", {"json": {"vehicle": "Vehicle_01", "percentage": "abc"}}),
    ("load_high_pct", "POST", "/api/loading/update", {"json": {"vehicle": "Vehicle_01", "percentage": 150}}),
    ("load_start_wrong_stop", "POST", "/api/loading/start", {"json": {"vehicle": "Vehicle_01"}}),
    ("warehouse_wrong_stop", "POST", "/api/warehouse", {"json": {"vehicle": "Vehicle_01"}}),
    ("complete_wrong_stop", "POST", "/api/complete", {"json": {"vehicle": "Vehicle_01"}}),

    # ---- full cycle for vehicle 1 + handover ---------------------
    ("cycle_start", "POST", "/api/start", {"json": {"vehicle": "yellow"}}),
    ("cycle_loading_point", "POST", "/api/loading-point", {"json": {"vehicle": "yellow"}}),
    ("cycle_load_start", "POST", "/api/loading/start", {"json": {"vehicle": "Vehicle_01"}}),
    ("cycle_load_50", "POST", "/api/loading/update", {"json": {"vehicle": "Vehicle_01", "percentage": 50}}),
    ("cycle_load_100", "POST", "/api/loading/update", {"json": {"vehicle": "Vehicle_01", "load_percentage": 100}}),
    ("cycle_load_start_full", "POST", "/api/loading/start", {"json": {"vehicle": "Vehicle_01"}}),
    ("cycle_warehouse", "POST", "/api/warehouse", {"json": {"vehicle": "1"}}),
    ("cycle_complete", "POST", "/api/complete", {"json": {"vehicle": "Vehicle_01"}}),

    # ---- events, j2735, safety, comms ---------------------------
    ("events", "GET", "/api/events?limit=5", {}),
    ("events_default", "GET", "/api/events", {}),
    ("events_vehicle", "GET", "/api/events/Vehicle_01?limit=3", {}),
    ("events_bad_vehicle", "GET", "/api/events/zzz", {}),
    ("bsm_default", "GET", "/api/j2735/bsm", {}),
    ("bsm_alias", "GET", "/api/j2735/bsm?vehicle=blue", {}),
    ("bsm_bad", "GET", "/api/j2735/bsm?vehicle=zzz", {}),
    ("safety", "GET", "/api/safety", {}),
    ("safety_vehicle", "GET", "/api/safety/blue", {}),
    ("safety_bad", "GET", "/api/safety/zzz", {}),
    ("communication", "GET", "/api/communication", {}),

    # ---- sensors and manual control -----------------------------
    ("radar_obstacle", "POST", "/api/sensors/radar", {"json": {"vehicle": "blue", "obstacle": True}}),
    ("start_blocked_radar", "POST", "/api/start", {"json": {"vehicle": "blue"}}),
    ("radar_clear", "POST", "/api/sensors/radar", {"json": {"vehicle": "blue", "status": "clear"}}),
    ("radar_no_vehicle", "POST", "/api/sensors/radar", {"json": {}}),
    ("brake_apply", "POST", "/api/sensors/brake", {"json": {"vehicle": "blue", "applied": True}}),
    ("brake_release", "POST", "/api/sensors/brake", {"json": {"vehicle": "blue", "applied": "released"}}),
    ("qr_not_detected", "POST", "/api/sensors/qr", {"json": {"vehicle": "blue", "detected": False}}),
    ("qr_no_vehicle", "POST", "/api/sensors/qr", {"json": {}}),
    ("manual_bad_cmd", "POST", "/api/manual-control", {"json": {"vehicle": "blue", "command": "JUMP"}}),
    ("manual_forward", "POST", "/api/manual-control", {"json": {"vehicle": "blue", "command": "forward"}}),
    ("manual_no_vehicle", "POST", "/api/manual-control", {"json": {"command": "STOP"}}),

    # ---- emergency, release, reset ------------------------------
    ("estop", "POST", "/api/emergency-stop", {"json": {"fleet": True}}),
    ("start_blocked_estop", "POST", "/api/start", {"json": {"vehicle": "yellow"}}),
    ("manual_blocked_estop", "POST", "/api/manual-control", {"json": {"vehicle": "blue", "command": "STOP"}}),
    ("release_bad", "POST", "/api/emergency-release", {"json": {"vehicle": "zzz"}}),
    ("release_one", "POST", "/api/emergency-release", {"json": {"vehicle": "Vehicle_01"}}),
    ("release_fleet", "POST", "/api/emergency-release", {"json": {"fleet": True}}),
    ("release_nothing", "POST", "/api/emergency-release", {}),
    ("reset", "POST", "/api/reset", {}),
    ("tb_sync", "POST", "/api/thingsboard/sync", {}),

    # ---- misc device endpoints ----------------------------------
    ("servo_ok", "POST", "/api/servo", {"json": {"angle": 90}}),
    ("servo_out_of_range", "POST", "/api/servo", {"json": {"angle": 500}}),
    ("vehicle_command", "POST", "/api/vehicle/command", {"json": {"command": "PING"}}),

    # ---- registry (write) ---------------------------------------
    ("add_remote_denied", "POST", "/api/vehicles", {"json": {"name": "Vehicle_09"}, "remote": True}),
    ("put_remote_denied", "PUT", "/api/vehicles/Vehicle_01", {"json": {"color": "red"}, "remote": True}),
    ("delete_remote_denied", "DELETE", "/api/vehicles/Vehicle_01", {"remote": True}),
    ("add_bad_mode", "POST", "/api/vehicles", {"json": {"name": "Vehicle_09", "mode": "weird"}}),
    ("add_bad_name", "POST", "/api/vehicles", {"json": {"name": "1bad"}}),
    ("add_link", "POST", "/api/vehicles", {"json": {"name": "Vehicle_03", "device_id": FAKE_V3_DEVICE, "token": "FAKE_TOKEN_0003", "color": "green"}}),
    ("add_link_duplicate", "POST", "/api/vehicles", {"json": {"name": "Vehicle_03", "device_id": FAKE_V3_DEVICE, "token": "FAKE_TOKEN_0003"}}),
    ("add_link_missing_token", "POST", "/api/vehicles", {"json": {"name": "Vehicle_05", "device_id": "00000000-0000-4000-8000-000000000005"}}),
    ("fleet_three", "GET", "/api/fleet", {}),
    ("put_color", "PUT", "/api/vehicles/Vehicle_03", {"json": {"color": "red"}}),
    ("put_rename", "PUT", "/api/vehicles/Vehicle_03", {"json": {"name": "Truck_03"}}),
    ("put_not_found", "PUT", "/api/vehicles/Nope", {"json": {"color": "red"}}),
    ("delete_linked_device_refused", "DELETE", "/api/vehicles/Truck_03?delete_device=true", {}),
    ("delete_linked", "DELETE", "/api/vehicles/Truck_03", {}),
    ("delete_not_found", "DELETE", "/api/vehicles/Truck_03", {}),
    ("add_auto", "POST", "/api/vehicles", {"json": {"name": "Vehicle_04", "color": "#ff8800"}}),
    ("delete_auto_with_device", "DELETE", "/api/vehicles/Vehicle_04?delete_device=yes", {}),
    ("vehicles_after", "GET", "/api/vehicles", {}),

    # ---- events housekeeping ------------------------------------
    ("events_clear", "POST", "/api/events/clear", {}),
    ("events_after_clear", "GET", "/api/events?limit=3", {}),
]
