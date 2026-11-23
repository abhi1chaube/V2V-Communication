# =========================================================
# LIVE PUSH FEED: SSE BROADCASTER (UPGRADE PHASE 5)
# =========================================================
#
# The dashboard used to poll GET /api/fleet every 800 ms and
# re-read every vehicle from ThingsBoard on every poll. This
# module is the backend side of a push feed instead: a `Hub`
# that bridges the SYNC world (the engine's tick thread, the V2V
# bus, the autopilot, events.add_event, alerts) to the ASYNC
# world (uvicorn's event loop, where SSE connections live).
#
# Channels: fleet (state deltas), poses, v2v (bus messages),
# events (activity log), alerts, autopilot, scenario.
#
# A tick hook (on the engine's thread) computes per-vehicle state
# and pose DELTAS against the last broadcast snapshot and calls
# hub.broadcast(channel, data) — which is thread-safe: it hands
# the message to the event loop with call_soon_threadsafe, so the
# actual queue bookkeeping always runs on the loop thread. Every
# message gets a monotonically increasing `seq`, so a client can
# detect a gap. api/routes_stream.py sends a full snapshot when a
# client connects and a fresh one every SSE_RESYNC_S (default 30s)
# per "state" channel (fleet, poses, alerts, autopilot); the
# others (events, v2v, scenario) are logs, not state, so a resync
# is a no-op for them.
#
# The world engine also feeds services/db.py's state_timeline
# table here, downsampled to about 1 Hz (Phase 7 session replay,
# and the safe-restore-after-restart read in services/state.py's
# caller in app.py).
# =========================================================

import asyncio
import itertools
import json
import threading

from . import alerts as alerts_module
from . import db
from . import events
from . import fleet
from . import state
from . import v2v
from . import world as world_module
from .settings import settings
from .timeutil import utc_iso


STATE_CHANNELS = ("fleet", "poses", "alerts", "autopilot")
LOG_CHANNELS = ("events", "v2v", "scenario")
CHANNELS = STATE_CHANNELS + LOG_CHANNELS

TIMELINE_INTERVAL_S = 1.0


class Hub:

    def __init__(self):

        self.loop = None
        self._lock = threading.Lock()
        self._clients = {}             # client_id -> {"queue": asyncio.Queue, "channels": set}
        self._next_client_id = itertools.count(1)
        self._seq = itertools.count(1)

    def bind_loop(self, loop):
        """Called once, from the lifespan handler (on the loop thread)."""
        self.loop = loop

    def unbind_loop(self):
        self.loop = None

    # ---- connections (called from an async route, on the loop thread) --------

    def subscribe(self, channels):

        queue = asyncio.Queue(maxsize=settings.sse_queue_size)

        with self._lock:
            client_id = next(self._next_client_id)
            self._clients[client_id] = {"queue": queue, "channels": set(channels)}

        return client_id, queue

    def unsubscribe(self, client_id):

        with self._lock:
            self._clients.pop(client_id, None)

    def client_count(self):

        with self._lock:
            return len(self._clients)

    # ---- publishing (callable from ANY thread) --------------------------------

    def broadcast(self, channel, data):

        seq = next(self._seq)
        message = {"channel": channel, "seq": seq, "ts": utc_iso(milliseconds=True), "data": data}

        loop = self.loop

        if loop is None:
            return seq

        try:
            loop.call_soon_threadsafe(self._dispatch, channel, message)
        except RuntimeError:
            pass          # the loop is closing (shutdown)

        return seq

    def _dispatch(self, channel, message):
        """Runs ON the loop thread (scheduled by call_soon_threadsafe)."""

        with self._lock:
            targets = [c["queue"] for c in self._clients.values() if channel in c["channels"]]

        for queue in targets:

            if queue.full():
                try:
                    queue.get_nowait()          # drop the oldest rather than back up forever
                except asyncio.QueueEmpty:
                    pass

            try:
                queue.put_nowait(message)
            except asyncio.QueueFull:
                pass

    def status(self):

        with self._lock:
            return {
                "clients": len(self._clients),
                "channels": {
                    channel: sum(1 for c in self._clients.values() if channel in c["channels"])
                    for channel in CHANNELS
                },
            }

    def reset_for_tests(self):
        with self._lock:
            self._clients.clear()


hub = Hub()


# =========================================================
# SNAPSHOTS (full state, for "connect" and the periodic resync)
# =========================================================

def snapshot(channel):

    if channel == "fleet":
        return {"vehicles": state.get_all_vehicle_states()}
    if channel == "poses":
        return {"poses": world_module.engine.poses_snapshot()}
    if channel == "alerts":
        return {"alerts": alerts_module.list_alerts()}
    if channel == "autopilot":
        from . import autopilot as autopilot_module
        return autopilot_module.controller.status()
    if channel == "events":
        return {"events": events.get_events(50)}
    if channel == "v2v":
        return {"messages": v2v.bus.messages(limit=50)}
    if channel == "scenario":
        from . import scenarios as scenarios_module
        return scenarios_module.runner.overview()

    return {}


# =========================================================
# TICK HOOK: FLEET + POSE DELTAS, AND THE STATE TIMELINE
# =========================================================

_last_fleet = {}
_last_poses = {}
_last_timeline_s = {}


def _reset():

    global _last_fleet, _last_poses, _last_timeline_s
    _last_fleet = {}
    _last_poses = {}
    _last_timeline_s = {}


def _tick_hook(dt):

    _publish_fleet_deltas()
    _publish_pose_deltas()
    _write_timeline()


def _publish_fleet_deltas():

    global _last_fleet

    current = state.get_all_vehicle_states()
    changed = []

    for vehicle_id, record in current.items():
        previous = _last_fleet.get(vehicle_id)
        if previous is None:
            changed.append(record)
            continue
        diff = {k: v for k, v in record.items() if previous.get(k) != v}
        if diff:
            diff["vehicle_id"] = vehicle_id
            changed.append(diff)

    removed = [vid for vid in _last_fleet if vid not in current]

    if changed or removed:
        hub.broadcast("fleet", {"changed": changed, "removed": removed})

    _last_fleet = {vid: dict(record) for vid, record in current.items()}


def _publish_pose_deltas():

    global _last_poses

    current = {p["vehicle_id"]: p for p in world_module.engine.poses_snapshot()}
    changed = [pose for vid, pose in current.items() if _last_poses.get(vid) != pose]
    removed = [vid for vid in _last_poses if vid not in current]

    if changed or removed:
        hub.broadcast("poses", {"changed": changed, "removed": removed})

    _last_poses = current


def _write_timeline():

    now = world_module.engine.sim_seconds

    for vehicle_id in fleet.vehicle_ids():

        if now - _last_timeline_s.get(vehicle_id, -1e9) < TIMELINE_INTERVAL_S:
            continue

        try:
            record = state.get_vehicle_state(vehicle_id)
            pose = world_module.engine.pose(vehicle_id)
        except ValueError:
            continue

        _last_timeline_s[vehicle_id] = now

        db.record(
            "state_timeline",
            timestamp_utc=utc_iso(milliseconds=True), vehicle=vehicle_id,
            x=pose["x"], y=pose["y"], heading_deg=pose["heading_deg"], node=pose["node"],
            segment=pose["segment"], distance_along_m=pose["distance_along_m"], speed_kmh=pose["speed_kmh"],
            status=record.get("status"), mission_status=record.get("mission_status"),
            location=record.get("location"), load_percentage=record.get("load_percentage"),
            battery=state.battery_of(record), emergency_stop=1 if record.get("emergency_stop") else 0,
            fault=record.get("fault"), autopilot_mode=record.get("autopilot_mode"),
            next_action=record.get("next_action"),
        )


# =========================================================
# LOG-STYLE CHANNELS: EVENTS, V2V MESSAGES, AUTOPILOT/SCENARIO TRACE, ALERTS
# =========================================================

def _on_event(entry):
    hub.broadcast("events", entry)


def _on_message(data):
    hub.broadcast("v2v", data)


def _on_trace(entry):
    channel = "scenario" if str(entry.get("event", "")).startswith("SCENARIO_") else "autopilot"
    hub.broadcast(channel, entry)


def _on_alert(alert):
    hub.broadcast("alerts", alert)


# =========================================================
# WIRING
# =========================================================

events.subscribers.append(_on_event)
v2v.bus.message_hooks.append(_on_message)
alerts_module.on_change = _on_alert

world_module.engine.add_tick_hook(_tick_hook)


def wire_autopilot(controller):
    """Called once autopilot.py's singleton exists (it imports v2v, which this
    module also imports, so it is wired from app.py after both are loaded)."""

    if _on_trace not in controller.trace_hooks:
        controller.trace_hooks.append(_on_trace)
