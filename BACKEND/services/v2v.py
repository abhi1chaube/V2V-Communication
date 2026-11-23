# =========================================================
# V2V MESSAGE BUS (SIMULATED VEHICLE-TO-VEHICLE RADIO)
# =========================================================
#
# Every vehicle has an onboard unit, its AGENT, which sends and
# receives messages over a simulated radio:
#
#   - Direct delivery only within V2V_COMM_RANGE_M of the sender
#     (positions from the world engine). Farther vehicles do not
#     receive the message (counted as out of range).
#   - Each hop has a latency (V2V_LATENCY_MIN_MS..MAX_MS) and is
#     lost with probability V2V_LOSS_RATE (seeded: SIM_SEED).
#   - A roadside unit (RSU, world.json "rsus") relays UNICAST
#     coordination messages when sender and receiver are each in
#     the RSU's range but too far apart for a direct hop (two
#     hops: two latencies, two loss draws). Broadcasts (CAM, BSM,
#     DENM, intents, gap advisories) are local and never relayed.
#   - Duplicates (same msg_id) and stale messages (older than
#     their ttl_ms) are rejected at the receiver and counted.
#
# The bus runs on the world engine's simulated clock (it is a
# tick hook of world.engine), so a run is deterministic.
#
# Messages (names follow V2X concepts; formats are our own JSON,
# NOT ETSI/SAE encodings; signature_stub is a SHA-256 of the
# payload for display only and is NOT real V2X security):
#
#   CAM                 heartbeat every V2V_CAM_INTERVAL_MS: position,
#                       speed, heading, mission, load, battery, stop
#   BSM                 the J2735-styled BasicSafetyMessage (j2735.py),
#                       broadcast whenever a vehicle's safety state changes
#   DENM                hazard warning: OBSTACLE, EMERGENCY_STOP,
#                       EMERGENCY_BRAKE, STALLED, LOW_BATTERY,
#                       HEARTBEAT_LOST; new / repeated / cancelled
#   HANDOVER_REQUEST / HANDOVER_ACCEPT / HANDOVER_REJECT / HANDOVER_COMPLETE
#                       negotiated hand-over of the next cycle (+ ACK)
#   SLOT_REQUEST / SLOT_GRANT / SLOT_RELEASE
#                       loading-point right-of-way, arbitrated by the RSU
#   INTENT_TO_PROCEED / YIELD
#                       right-of-way on a shared (two-way) lane
#   PLATOON_JOIN / PLATOON_ACK / PLATOON_LEAVE / GAP_ADVISORY
#                       vehicles on the same route keep V2V_TARGET_GAP_M
#   ACK                 acknowledgement of a unicast message
#
# Safety fail-safe: a neighbour in radio range that has not been
# heard for V2V_CAM_TIMEOUT_MS is LOST; its last known position
# becomes a hazard and a HEARTBEAT_LOST DENM is broadcast. A
# vehicle that has lost every vehicle in range sets comm_hold in
# its state: it holds where it is and movement commands are
# refused (V2V_COMM_LOST) until contact returns.
#
# Reacting to received hazards (braking for a DENM, gap rules)
# is the autopilot's job (upgrade Phase 4); here they are
# recorded and shown.
# =========================================================

import hashlib
import heapq
import itertools
import json
import math
import threading
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import Optional

from . import db
from . import events
from . import fleet
from . import simrng
from . import state
from . import world as world_module
from .settings import settings
from .timeutil import utc_iso


BROADCAST = "*"

# type -> priority (0 routine .. 7 safety-critical), ttl_ms, description
MESSAGE_TYPES = {
    "CAM":               (2, 1000, "Cooperative Awareness Message: periodic heartbeat with position and status."),
    "BSM":               (3, 1000, "J2735-styled BasicSafetyMessage, sent when the safety state changes."),
    "DENM":              (7, 3000, "Decentralised Environmental Notification: a hazard warning."),
    "HANDOVER_REQUEST":  (5, 3000, "Ask the next vehicle to take over the cycle."),
    "HANDOVER_ACCEPT":   (5, 3000, "The next vehicle can take over."),
    "HANDOVER_REJECT":   (5, 3000, "The next vehicle cannot take over (with a reason)."),
    "HANDOVER_COMPLETE": (5, 3000, "Hand-over confirmed: the next vehicle becomes ready."),
    "SLOT_REQUEST":      (4, 2000, "Ask the roadside unit for the loading point."),
    "SLOT_GRANT":        (4, 2000, "Answer to a slot request (granted, or queue position)."),
    "SLOT_RELEASE":      (4, 2000, "The vehicle has left the loading point."),
    "INTENT_TO_PROCEED": (6, 1500, "About to use a shared lane, or on it."),
    "YIELD":             (6, 1500, "Waiting so another vehicle can use the shared lane first."),
    "PLATOON_JOIN":      (4, 2000, "Ask the vehicle ahead to lead a platoon."),
    "PLATOON_ACK":       (4, 2000, "Accepted as platoon follower."),
    "PLATOON_LEAVE":     (4, 2000, "Leaving the platoon."),
    "GAP_ADVISORY":      (5, 1500, "Platoon leader's speed and the gap to keep."),
    "ACK":               (3, 2000, "Acknowledgement of a unicast message."),
}

# Illustrative cause codes in the style of ETSI DENM (not normative).
DENM_CAUSES = {
    "OBSTACLE":        {"code": 10, "severity": 3, "radius_m": 100, "label": "Obstacle on the road"},
    "EMERGENCY_STOP":  {"code": 99, "severity": 4, "radius_m": 150, "label": "Vehicle under emergency stop"},
    "EMERGENCY_BRAKE": {"code": 97, "severity": 4, "radius_m": 100, "label": "Hard braking"},
    "STALLED":         {"code": 91, "severity": 2, "radius_m": 80,  "label": "Vehicle stalled on its route"},
    "LOW_BATTERY":     {"code": 92, "severity": 1, "radius_m": 50,  "label": "Battery below 20 %"},
    "HEARTBEAT_LOST":  {"code": 98, "severity": 3, "radius_m": 150, "label": "No heartbeat from a vehicle in range"},
}

HISTORY_SIZE = 500
INBOX_SIZE = 100
SEEN_IDS_PER_RECEIVER = 2000

DEGRADED_QUALITY = 0.7
DENM_REPEAT_MS = 1000
STALL_MS = 5000
SLOT_RETRY_MS = 1000
INTENT_INTERVAL_MS = 500
LANE_APPROACH_M = 40.0
PLATOON_RANGE_M = 60.0
PLATOON_JOIN_RETRY_MS = 1000
GAP_GAIN_PER_S = 0.5
LOW_BATTERY_PERCENT = 20

LOADING_MISSIONS = ("MOVING_TO_LOADING", "AT_LOADING_POINT", "LOADING")


class V2VError(ValueError):
    """A V2V request was invalid (bad message type, unknown vehicle, ...)."""


# =========================================================
# CONFIGURATION (CHANGEABLE AT RUNTIME)
# =========================================================

CONFIG_LIMITS = {
    "comm_range_m": (1.0, 2000.0),
    "loss_rate": (0.0, 1.0),
    "latency_min_ms": (0.0, 5000.0),
    "latency_max_ms": (0.0, 5000.0),
    "cam_interval_ms": (50.0, 10000.0),
    "cam_timeout_ms": (100.0, 60000.0),
    "handover_timeout_ms": (100.0, 60000.0),
    "target_gap_m": (1.0, 200.0),
}


def _default_config():

    return {
        "comm_range_m": settings.v2v_comm_range_m,
        "loss_rate": settings.v2v_loss_rate,
        "latency_min_ms": settings.v2v_latency_min_ms,
        "latency_max_ms": settings.v2v_latency_max_ms,
        "rsu_enabled": settings.v2v_rsu_enabled,
        "cam_interval_ms": settings.v2v_cam_interval_ms,
        "cam_timeout_ms": settings.v2v_cam_timeout_ms,
        "handover_timeout_ms": settings.v2v_handover_timeout_ms,
        "target_gap_m": settings.v2v_target_gap_m,
    }


# =========================================================
# MESSAGE
# =========================================================

@dataclass
class Message:

    msg_id: str
    msg_type: str
    sender: str
    recipient: str
    seq: int
    timestamp_utc: str
    sent_at_ms: float
    ttl_ms: float
    priority: int
    payload: dict
    signature_stub: str
    deliveries: list = field(default_factory=list)

    def to_dict(self, deliveries=True):

        data = {
            "msg_id": self.msg_id,
            "msg_type": self.msg_type,
            "sender": self.sender,
            "recipient": self.recipient,
            "seq": self.seq,
            "timestamp_utc": self.timestamp_utc,
            "sent_at_ms": round(self.sent_at_ms, 1),
            "ttl_ms": self.ttl_ms,
            "priority": self.priority,
            "payload": self.payload,
            "signature_stub": self.signature_stub,
        }

        if deliveries:
            data["deliveries"] = [dict(d) for d in self.deliveries]

        return data


def signature_of(payload):
    """SHA-256 of the payload. For display only: NOT real V2X security."""

    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, default=str).encode()
    ).hexdigest()


# =========================================================
# HANDOVER NEGOTIATION
# =========================================================

@dataclass
class Negotiation:

    negotiation_id: str
    from_vehicle: str
    to_vehicle: str
    started_ms: float
    deadline_ms: float
    status: str = "requesting"        # requesting, accepted, completed, rejected, timeout
    reason: Optional[str] = None
    request_attempts: int = 1
    complete_attempts: int = 0
    finished_ms: Optional[float] = None
    next_state: Optional[dict] = None
    messages: list = field(default_factory=list)
    event: threading.Event = field(default_factory=threading.Event)

    @property
    def done(self):
        return self.status in ("completed", "rejected", "timeout")

    def to_dict(self):
        return {
            "negotiation_id": self.negotiation_id,
            "from_vehicle": self.from_vehicle,
            "to_vehicle": self.to_vehicle,
            "status": self.status,
            "reason": self.reason,
            "request_attempts": self.request_attempts,
            "complete_attempts": self.complete_attempts,
            "duration_ms": (
                round(self.finished_ms - self.started_ms, 1)
                if self.finished_ms is not None else None
            ),
            "messages": list(self.messages),
        }


# =========================================================
# ROADSIDE UNIT (RELAY + LOADING-DOCK SLOT MANAGER)
# =========================================================

class RoadsideUnit:

    def __init__(self, bus, spec):

        self.bus = bus
        self.id = spec["id"]
        self.x = spec["x"]
        self.y = spec["y"]
        self.range_m = spec["range_m"]
        self.label = spec["label"]
        self.manages_stop = spec.get("manages_stop")
        self.holder = None
        self.holder_since_ms = None
        self.queue = []
        self.requested_ms = {}        # vehicle -> when it last asked
        self.last_cam = {}            # vehicle -> (received_ms, payload)

    def heard_recently(self, vehicle, now):

        entry = self.last_cam.get(vehicle)
        return entry is not None and now - entry[0] <= self.bus.config["cam_timeout_ms"]

    def _heading_to_slot(self, vehicle, now, since_ms):
        """True/False from the vehicle's CAMs received after `since_ms`; None if none yet.

        Only CAMs newer than the grant (or request) count: an older one
        still describes the vehicle before it set off for the slot.
        """

        entry = self.last_cam.get(vehicle)

        if (
            entry is None
            or entry[0] <= (since_ms if since_ms is not None else -math.inf)
            or now - entry[0] > 2 * self.bus.config["cam_timeout_ms"]
        ):
            return None               # unknown: keep the current assumption

        payload = entry[1]

        if payload.get("status") == state.STATUS_RUNNING and payload.get("mission") == "MOVING_TO_WAREHOUSE":
            return False              # loaded and driving off (autopilot): the slot is free

        return (
            payload.get("stop") == self.manages_stop
            or payload.get("mission") in LOADING_MISSIONS
        )

    def on_message(self, message, now):

        if message.msg_type == "CAM":
            self.last_cam[message.sender] = (now, message.payload)
            return

        if not self.manages_stop:
            return

        if message.msg_type == "SLOT_REQUEST":

            vehicle = message.sender
            self.requested_ms[vehicle] = now

            if self.holder in (None, vehicle) and self._slot_free_for(vehicle):
                self._grant(vehicle, message)
                return

            if vehicle not in self.queue:
                self.queue.append(vehicle)

            self.bus.send(self.id, vehicle, "SLOT_GRANT", {
                "stop": self.manages_stop,
                "granted": False,
                "holder": self.holder,
                "queue_position": self.queue.index(vehicle) + 1,
                "in_reply_to": message.msg_id,
            })

        elif message.msg_type == "SLOT_RELEASE" and self.holder == message.sender:
            self._release(f"released by {message.sender}")

    def _slot_free_for(self, vehicle):

        occupant = state.stop_2_occupant(except_vehicle=vehicle)
        return occupant is None

    def _grant(self, vehicle, request=None):

        self.holder = vehicle
        self.holder_since_ms = self.bus.now_ms

        if vehicle in self.queue:
            self.queue.remove(vehicle)

        self.bus.send(self.id, vehicle, "SLOT_GRANT", {
            "stop": self.manages_stop,
            "granted": True,
            "holder": vehicle,
            "queue_position": 0,
            "in_reply_to": request.msg_id if request else None,
        })

        events.add_event(
            f"[V2V] {self.id} granted the loading point to {events.vehicle_name(vehicle)}.",
            vehicle,
            event_type="v2v",
            severity="info",
        )

    def _release(self, why):

        self.holder = None
        self.holder_since_ms = None

    def claim(self, vehicle):
        """A direct command moved `vehicle` to the managed stop: it holds the slot."""

        self.holder = vehicle
        self.holder_since_ms = self.bus.now_ms

        if vehicle in self.queue:
            self.queue.remove(vehicle)

    def tick(self, now):

        if not self.manages_stop:
            return

        registered = fleet.vehicle_ids()

        if self.holder is not None:
            heading = self._heading_to_slot(self.holder, now, self.holder_since_ms)
            if self.holder not in registered or heading is False:
                # The holder left (its CAM shows it), even if its
                # SLOT_RELEASE was lost.
                self._release("holder left")

        self.queue = [
            v for v in self.queue
            if v in registered and self._heading_to_slot(v, now, self.requested_ms.get(v)) is not False
        ]

        if self.holder is None and self.queue:
            order = {v: i for i, v in enumerate(registered)}
            for vehicle in sorted(self.queue, key=lambda v: order.get(v, 1_000_000)):
                if self._slot_free_for(vehicle):
                    self._grant(vehicle)
                    break

    def view(self, now):

        return {
            "id": self.id,
            "label": self.label,
            "x": self.x,
            "y": self.y,
            "range_m": self.range_m,
            "manages_stop": self.manages_stop,
            "slot_holder": self.holder,
            "queue": list(self.queue),
            "hears": sorted(v for v in self.last_cam if self.heard_recently(v, now)),
        }


# =========================================================
# VEHICLE AGENT (ONBOARD UNIT)
# =========================================================

class VehicleAgent:

    def __init__(self, bus, vehicle_id):

        self.bus = bus
        self.id = vehicle_id
        self.neighbours = {}          # other -> dict
        self.hazards = {}             # denm_id -> dict (heard from others)
        self.own_denms = {}           # key -> dict (raised by this vehicle)
        self.intents = {}             # other -> intent dict (heard)
        self.last_cam_ms = -math.inf
        self.ticked = False
        self.comm_hold = False
        self.stalled_since = None
        self.braking_hard = False
        # Set by the autopilot while it deliberately holds this vehicle
        # (waiting for the slot, a hazard ahead, the gap): not a stall.
        self.advised_hold = None
        self.slot = {"granted": False, "last_request_ms": -math.inf, "queue_position": None}
        self.lane = {"lane": None, "segment": None, "requested_at_ms": None,
                     "last_sent_ms": -math.inf, "status": None, "yielded_to": set(), "blocked_by": None}
        self.platoon = {"role": None, "leader": None, "followers": set(),
                        "last_join_ms": -math.inf, "last_advisory_ms": -math.inf,
                        "leader_speed_mps": None, "target_gap_m": None}

    # ---- helpers -------------------------------------------------

    def send(self, recipient, msg_type, payload):
        return self.bus.send(self.id, recipient, msg_type, payload)

    def neighbour(self, other):

        return self.neighbours.setdefault(other, {
            "state": "unknown", "last_heard_ms": None, "in_range_since_ms": None,
            "last_cam": None, "last_cam_ms": None,
        })

    def heard(self, sender, message, now):

        if sender not in self.bus.vehicle_ids_cache:
            return

        entry = self.neighbour(sender)
        entry["last_heard_ms"] = now

        if message.msg_type == "CAM":
            entry["last_cam"] = message.payload
            entry["last_cam_ms"] = now

    def lost_in_range(self):
        return [v for v, n in self.neighbours.items() if n["state"] == "lost"]

    # ---- one tick -------------------------------------------------

    def tick(self, now, positions, record):

        self.ticked = True
        me = positions.get(self.id)

        if me is None:
            return

        if now - self.last_cam_ms >= self.bus.config["cam_interval_ms"]:
            self._send_cam(me, record, now)

        self._evaluate_neighbours(now, positions)
        self._update_comm_hold(record)
        self._own_hazards(now, me, record)
        self._slot(now, me, record)
        self._lane(now, me, record)
        self._platoon(now, me, record)

        for denm_id in [k for k, h in self.hazards.items() if h["expires_ms"] < now]:
            del self.hazards[denm_id]

        for other in [k for k, i in self.intents.items() if i["expires_ms"] < now]:
            del self.intents[other]

    def _send_cam(self, me, record, now):

        battery = record.get("battery_level")
        if battery is None:
            battery = record.get("battery_percentage")

        self.send(BROADCAST, "CAM", {
            "x": me["x"], "y": me["y"],
            "segment": me["segment"], "s": me["distance_along_m"], "node": me["node"],
            "heading_deg": me["heading_deg"], "speed_mps": me["speed_mps"],
            "destination": me["destination"],
            "status": record.get("status"), "mission": record.get("mission_status"),
            "stop": record.get("location"), "load_percentage": record.get("load_percentage"),
            "battery": battery, "emergency_stop": record.get("emergency_stop"),
            "comm_hold": record.get("comm_hold"),
        })
        self.last_cam_ms = now

    # ---- heartbeat fail-safe -----------------------------------------

    def _evaluate_neighbours(self, now, positions):

        timeout = self.bus.config["cam_timeout_ms"]
        me = positions.get(self.id)

        for other in self.bus.vehicle_ids_cache:

            if other == self.id:
                continue

            entry = self.neighbour(other)
            previous = entry["state"]

            them = positions.get(other)
            in_range = them is not None and _distance(me, them) <= self.bus.config["comm_range_m"]
            heard_recent = entry["last_heard_ms"] is not None and now - entry["last_heard_ms"] <= timeout

            if in_range and entry["in_range_since_ms"] is None:
                entry["in_range_since_ms"] = now
            if not in_range:
                entry["in_range_since_ms"] = None

            if heard_recent:
                quality = self.bus.link_quality(other, self.id)
                current = "degraded" if quality < DEGRADED_QUALITY else "connected"
            elif in_range:
                # Silent for a whole timeout since it came into range (a
                # vehicle that comes back into range is not "lost" before
                # its next heartbeat had a chance to arrive).
                silent_since = max(entry["last_heard_ms"] or -math.inf, entry["in_range_since_ms"])
                current = "lost" if now - silent_since > timeout else "pending"
            else:
                current = "out_of_range"

            entry["state"] = current

            if current == "lost" and previous != "lost":
                self._heartbeat_lost(other, entry, now)
            elif previous == "lost" and current != "lost":
                self._heartbeat_restored(other, current)

        for other in [v for v in self.neighbours if v not in self.bus.vehicle_ids_cache]:
            del self.neighbours[other]

    def _heartbeat_lost(self, other, entry, now):

        cam = entry.get("last_cam") or {}
        position = {"x": cam.get("x"), "y": cam.get("y"), "segment": cam.get("segment")}

        self._raise_denm(f"HEARTBEAT_LOST:{other}", "HEARTBEAT_LOST", now, position, about=other)

        # Treat the silent vehicle as an unknown obstacle at its
        # last known position until it is heard again.
        self.hazards[f"self:HEARTBEAT_LOST:{other}"] = {
            "denm_id": f"self:HEARTBEAT_LOST:{other}", "cause": "HEARTBEAT_LOST",
            "origin": self.id, "about": other, "position": position,
            "severity": DENM_CAUSES["HEARTBEAT_LOST"]["severity"],
            "relevance_radius_m": DENM_CAUSES["HEARTBEAT_LOST"]["radius_m"],
            "received_ms": now, "expires_ms": math.inf,
        }

        events.add_event(
            f"[V2V] {events.vehicle_name(self.id)} lost contact with "
            f"{events.vehicle_name(other)} (no heartbeat).",
            self.id, event_type="v2v", severity="warning",
            details={"v2v": {"link": f"{other}->{self.id}", "state": "lost", "last_known": position}},
        )

    def _heartbeat_restored(self, other, current):

        self.hazards.pop(f"self:HEARTBEAT_LOST:{other}", None)
        self._cancel_denm(f"HEARTBEAT_LOST:{other}", self.bus.now_ms)

        if current != "out_of_range":
            events.add_event(
                f"[V2V] {events.vehicle_name(self.id)} hears {events.vehicle_name(other)} again.",
                self.id, event_type="v2v", severity="info",
            )

    def _update_comm_hold(self, record):

        in_range = [n for n in self.neighbours.values() if n["state"] in ("connected", "degraded", "pending", "lost")]
        hold = bool(in_range) and all(n["state"] == "lost" for n in in_range)

        if hold == bool(record.get("comm_hold")):
            self.comm_hold = hold
            return

        self.comm_hold = hold

        try:
            state.update_vehicle_state(self.id, comm_hold=hold)
        except ValueError:
            return

        events.add_event(
            (
                f"[SAFETY] {events.vehicle_name(self.id)} holds: V2V contact with every "
                "vehicle in range is lost."
                if hold
                else f"[SAFETY] {events.vehicle_name(self.id)} V2V contact restored; hold released."
            ),
            self.id, event_type="safety", severity="critical" if hold else "info",
        )

    # ---- own hazards -> DENM ----------------------------------------------

    def _own_hazards(self, now, me, record):

        obstacle = record.get("radar_status") == state.RADAR_OBSTACLE or bool(record.get("obstacle_detected"))
        estop = bool(record.get("emergency_stop"))
        speed = me["speed_mps"] or 0.0

        if (estop or obstacle) and speed > 1.0:
            self.braking_hard = True
        if speed <= 0.05:
            self.braking_hard = False

        battery = record.get("battery_level")
        if battery is None:
            battery = record.get("battery_percentage")
        try:
            low_battery = battery is not None and float(battery) < settings.battery_low_pct
        except (TypeError, ValueError):
            low_battery = False

        moving_intended = record.get("status") == state.STATUS_RUNNING and bool(me["path"])
        explained = obstacle or estop or record.get("comm_hold") or self.lane["blocked_by"] \
            or (self.platoon["role"] == "follower") or self.advised_hold

        if moving_intended and speed < 0.05 and not explained:
            if self.stalled_since is None:
                self.stalled_since = now
        else:
            self.stalled_since = None

        stalled = self.stalled_since is not None and now - self.stalled_since >= STALL_MS

        # A breakdown on the road blocks it: warn at once (not at a stop,
        # where a parked vehicle is in nobody's way).
        if record.get("fault") and me["segment"]:
            stalled = True

        position = {"x": me["x"], "y": me["y"], "segment": me["segment"]}

        for cause, active in (
            ("OBSTACLE", obstacle), ("EMERGENCY_STOP", estop),
            ("EMERGENCY_BRAKE", self.braking_hard), ("STALLED", stalled),
            ("LOW_BATTERY", low_battery),
        ):
            if active:
                self._raise_denm(cause, cause, now, position)
            else:
                self._cancel_denm(cause, now)

    def _raise_denm(self, key, cause, now, position, about=None):

        info = DENM_CAUSES[cause]
        existing = self.own_denms.get(key)

        if existing is not None and now - existing["last_sent_ms"] < DENM_REPEAT_MS:
            return

        action = "update" if existing else "new"
        since = existing["since_ms"] if existing else now
        denm_id = existing["denm_id"] if existing else f"{self.id}:{key}:{int(now)}"

        message = self.send(BROADCAST, "DENM", {
            "denm_id": denm_id, "action": action, "cause": cause,
            "cause_code": info["code"], "cause_label": info["label"],
            "severity": info["severity"], "relevance_radius_m": info["radius_m"],
            "position": position, "about": about or self.id,
        })

        self.own_denms[key] = {"denm_id": denm_id, "cause": cause, "about": about or self.id,
                               "since_ms": since, "last_sent_ms": now, "position": position}

        if action == "new" and cause not in ("HEARTBEAT_LOST",):
            events.add_event(
                f"[V2V] {events.vehicle_name(self.id)} DENM: {info['label']}.",
                self.id, event_type="v2v", severity="warning",
                details={"v2v": message.to_dict(deliveries=False)},
            )

    def _cancel_denm(self, key, now):

        existing = self.own_denms.pop(key, None)

        if existing is None:
            return

        info = DENM_CAUSES[existing["cause"]]

        self.send(BROADCAST, "DENM", {
            "denm_id": existing["denm_id"], "action": "cancel", "cause": existing["cause"],
            "cause_code": info["code"], "cause_label": info["label"],
            "severity": info["severity"], "relevance_radius_m": info["radius_m"],
            "position": existing["position"], "about": existing["about"],
        })

    # ---- loading slot (client side) ---------------------------------------------

    def _slot(self, now, me, record):

        dock = self.bus.dock()

        if dock is None or not self.bus.config["rsu_enabled"]:
            return

        heading = record.get("mission_status") == "MOVING_TO_LOADING"
        at_slot = record.get("location") == dock.manages_stop and not state.departed_stop_2(record)

        if heading and not self.slot["granted"] and now - self.slot["last_request_ms"] >= SLOT_RETRY_MS:
            self.send(dock.id, "SLOT_REQUEST", {"stop": dock.manages_stop})
            self.slot["last_request_ms"] = now

        if self.slot["granted"] and not heading and not at_slot and record.get("mission_status") not in LOADING_MISSIONS:
            self.send(dock.id, "SLOT_RELEASE", {"stop": dock.manages_stop})
            self.slot.update(granted=False, queue_position=None)

    # ---- shared lane right-of-way -------------------------------------------------

    def _lane_entry(self, me):
        """(lane_id, segment) this vehicle is about to enter or is on, and its status."""

        lanes = self.bus.lane_of_segment
        path = me["path"]

        if me["segment"] in lanes:
            on_lane = me["distance_along_m"] > 0.5 or me["speed_mps"] > 0.1
            return lanes[me["segment"]], me["segment"], ("on_lane" if on_lane else "approaching")

        if len(path) > 1 and path[1] in lanes:
            remaining = self.bus.world.segments[path[0]].length - me["distance_along_m"]
            if remaining <= LANE_APPROACH_M:
                return lanes[path[1]], path[1], "approaching"

        return None, None, None

    def _lane(self, now, me, record):

        lane, segment, status = self._lane_entry(me)

        if lane is None or record.get("status") != state.STATUS_RUNNING:
            self.lane.update(lane=None, segment=None, requested_at_ms=None, status=None,
                             blocked_by=None, yielded_to=set())
            return

        if self.lane["lane"] != lane or self.lane["segment"] != segment:
            self.lane.update(lane=lane, segment=segment, requested_at_ms=now, yielded_to=set())

        self.lane["status"] = status

        if now - self.lane["last_sent_ms"] >= INTENT_INTERVAL_MS:
            self.send(BROADCAST, "INTENT_TO_PROCEED", {
                "lane": lane, "segment": segment, "status": status,
                "requested_at_ms": self.lane["requested_at_ms"],
            })
            self.lane["last_sent_ms"] = now

        blocker = self._lane_blocker(lane, segment, status) if status == "approaching" else None
        self.lane["blocked_by"] = blocker

        if blocker and blocker not in self.lane["yielded_to"] and blocker in self.bus.vehicle_ids_cache:
            self.send(blocker, "YIELD", {"lane": lane, "segment": segment})
            self.lane["yielded_to"].add(blocker)

    def _lane_blocker(self, lane, segment, my_status):
        """Who must go first on this lane, or None if this vehicle may enter."""

        order = {v: i for i, v in enumerate(self.bus.vehicle_ids_cache)}
        mine = self.lane["requested_at_ms"]

        # Fail-safe: a silent vehicle in range might be anywhere.
        lost = self.lost_in_range()
        if lost:
            return lost[0]

        for other, entry in self.neighbours.items():
            cam = entry.get("last_cam") or {}
            other_lane = self.bus.lane_of_segment.get(cam.get("segment"))
            if other_lane == lane and cam.get("segment") != segment:
                return other          # on the lane, coming the other way

        for other, intent in self.intents.items():
            if intent["lane"] != lane or intent["segment"] == segment:
                continue
            if intent["status"] == "on_lane":
                return other
            theirs = intent["requested_at_ms"]
            if theirs < mine or (theirs == mine and order.get(other, 0) < order.get(self.id, 0)):
                return other

        return None

    # ---- platoon ------------------------------------------------------------------

    def _platoon(self, now, me, record):

        timeout = self.bus.config["cam_timeout_ms"]
        me_pose = _pose_like(me)

        # Leader: drop followers that went silent; advise the others.
        for follower in list(self.platoon["followers"]):
            n = self.neighbours.get(follower)
            if n is None or n["last_heard_ms"] is None or now - n["last_heard_ms"] > timeout:
                self.platoon["followers"].discard(follower)

        if self.platoon["followers"]:
            self.platoon["role"] = "leader" if self.platoon["role"] != "follower" else "follower"
            if now - self.platoon["last_advisory_ms"] >= INTENT_INTERVAL_MS:
                self.send(BROADCAST, "GAP_ADVISORY", {
                    "leader_speed_mps": me["speed_mps"],
                    "target_gap_m": self.bus.config["target_gap_m"],
                    "followers": sorted(self.platoon["followers"]),
                })
                self.platoon["last_advisory_ms"] = now
        elif self.platoon["role"] == "leader":
            self.platoon["role"] = None

        # Follower: stay with the leader while it is ahead on our route.
        if self.platoon["role"] == "follower":
            leader = self.platoon["leader"]
            gap = self.gap_to(leader, me_pose, now)
            n = self.neighbours.get(leader, {})
            if gap is None or gap > 2 * PLATOON_RANGE_M or n.get("state") in ("lost", "out_of_range", None):
                self.send(leader, "PLATOON_LEAVE", {"reason": "not ahead on route"})
                self.platoon.update(role=None, leader=None, leader_speed_mps=None, target_gap_m=None)
            return

        if record.get("status") != state.STATUS_RUNNING or not me["path"]:
            return

        candidate, best = None, None
        for other, entry in self.neighbours.items():
            if entry["state"] != "connected" or not entry.get("last_cam"):
                continue
            gap = self.gap_to(other, me_pose, now)
            if gap is not None and gap <= PLATOON_RANGE_M and (best is None or gap < best):
                candidate, best = other, gap

        if candidate and now - self.platoon["last_join_ms"] >= PLATOON_JOIN_RETRY_MS:
            self.send(candidate, "PLATOON_JOIN", {"gap_m": round(best, 1)})
            self.platoon["last_join_ms"] = now

    def gap_to(self, other, me_pose, now):
        """Metres along this vehicle's route to `other` (from its last CAM), or None."""

        entry = self.neighbours.get(other)
        if not entry or not entry.get("last_cam"):
            return None

        cam = entry["last_cam"]
        if not cam.get("segment"):
            # Parked at a node: the start of no segment; use the
            # segment ending there if it is on our route.
            node = cam.get("node")
            if (
                node is not None
                and node == me_pose.destination
                and self.bus.world.stop_of_node(node) in world_module.MULTI_BAY_STOPS
            ):
                return None           # parked in another bay of our destination
            for segment_id in me_pose.path:
                if self.bus.world.segments[segment_id].end == node:
                    return world_module.path_gap(
                        self.bus.world, me_pose, segment_id, self.bus.world.segments[segment_id].length)
            return None

        age_s = max(0.0, (now - (entry["last_cam_ms"] or now)) / 1000)
        s = cam.get("s", 0.0) + (cam.get("speed_mps") or 0.0) * age_s
        return world_module.path_gap(self.bus.world, me_pose, cam["segment"], s)

    # ---- incoming messages -------------------------------------------------------------

    def on_message(self, message, now):

        kind = message.msg_type
        payload = message.payload or {}

        if kind == "DENM":
            denm_id = payload.get("denm_id") or message.msg_id
            if payload.get("action") == "cancel":
                self.hazards.pop(denm_id, None)
            else:
                self.hazards[denm_id] = {
                    "denm_id": denm_id, "cause": payload.get("cause"), "origin": message.sender,
                    "about": payload.get("about"), "position": payload.get("position"),
                    "severity": payload.get("severity"),
                    "relevance_radius_m": payload.get("relevance_radius_m"),
                    "received_ms": now, "expires_ms": now + message.ttl_ms,
                }

        elif kind == "INTENT_TO_PROCEED":
            self.intents[message.sender] = {
                "lane": payload.get("lane"), "segment": payload.get("segment"),
                "status": payload.get("status"), "requested_at_ms": payload.get("requested_at_ms", now),
                "expires_ms": now + message.ttl_ms,
            }

        elif kind == "SLOT_GRANT":
            if payload.get("granted"):
                self.slot.update(granted=True, queue_position=0)
            else:
                self.slot.update(granted=False, queue_position=payload.get("queue_position"))

        elif kind == "PLATOON_JOIN":
            self.platoon["followers"].add(message.sender)
            if self.platoon["role"] is None:
                self.platoon["role"] = "leader"
            self.send(message.sender, "PLATOON_ACK", {
                "target_gap_m": self.bus.config["target_gap_m"], "in_reply_to": message.msg_id})

        elif kind == "PLATOON_ACK":
            if self.platoon["role"] is None:
                self.platoon.update(role="follower", leader=message.sender,
                                    target_gap_m=payload.get("target_gap_m"))

        elif kind == "PLATOON_LEAVE":
            self.platoon["followers"].discard(message.sender)

        elif kind == "GAP_ADVISORY":
            if self.platoon["role"] == "follower" and self.platoon["leader"] == message.sender:
                self.platoon.update(leader_speed_mps=payload.get("leader_speed_mps"),
                                    target_gap_m=payload.get("target_gap_m"))

        elif kind in ("HANDOVER_REQUEST", "HANDOVER_COMPLETE", "HANDOVER_ACCEPT", "HANDOVER_REJECT"):
            self.bus.on_handover_message(self, message, now)

    # ---- advice for the world engine ----------------------------------------------------

    def advise(self, pose, now):

        advice = {}
        me = _snapshot_of(pose)

        lane, segment, status = self._lane_entry(me)
        if lane is not None and status == "approaching" and self.lane.get("blocked_by"):
            if pose.segment == segment:
                stop_within = 0.0                     # waiting at the lane entry
            else:
                stop_within = self.bus.world.segments[pose.segment].length - pose.s
            advice.update(stop_within_m=stop_within, action="YIELD_SHARED_LANE")

        if self.platoon["role"] == "follower":
            gap = self.gap_to(self.platoon["leader"], pose, now)
            if gap is not None:
                leader_speed = self.platoon["leader_speed_mps"]
                if leader_speed is None:
                    cam = (self.neighbours.get(self.platoon["leader"]) or {}).get("last_cam") or {}
                    leader_speed = cam.get("speed_mps") or 0.0
                target_gap = self.platoon["target_gap_m"] or self.bus.config["target_gap_m"]
                cap = max(0.0, leader_speed + GAP_GAIN_PER_S * (gap - target_gap))
                advice["speed_cap_mps"] = cap
                advice.setdefault("action", "PLATOON_FOLLOW")

        return advice

    def view(self, now):

        return {
            "vehicle_id": self.id,
            "comm_hold": self.comm_hold,
            "neighbours": {
                other: {
                    "state": n["state"],
                    "last_heard_ms_ago": (round(now - n["last_heard_ms"], 1) if n["last_heard_ms"] is not None else None),
                    "link_quality": round(self.bus.link_quality(other, self.id), 3),
                }
                for other, n in sorted(self.neighbours.items())
            },
            "hazards": [
                {k: v for k, v in h.items() if k != "expires_ms"} for h in self.hazards.values()
            ],
            "own_denms": sorted(d["cause"] if d["about"] == self.id else f"{d['cause']}:{d['about']}"
                                for d in self.own_denms.values()),
            "slot": {"granted": self.slot["granted"], "queue_position": self.slot["queue_position"]},
            "lane": {
                "lane": self.lane["lane"], "segment": self.lane["segment"],
                "status": self.lane["status"], "blocked_by": self.lane["blocked_by"],
            },
            "platoon": {
                "role": self.platoon["role"], "leader": self.platoon["leader"],
                "followers": sorted(self.platoon["followers"]),
                "target_gap_m": self.platoon["target_gap_m"],
            },
        }


def _distance(a, b):

    if a is None or b is None:
        return math.inf

    return math.hypot(a["x"] - b["x"], a["y"] - b["y"])


def _pose_like(snapshot):
    """A Pose with the fields path_gap() needs, from a pose snapshot."""

    return world_module.Pose(
        vehicle_id=snapshot["vehicle_id"], node=snapshot["node"], segment=snapshot["segment"],
        s=snapshot["distance_along_m"], path=list(snapshot["path"]), speed_mps=snapshot["speed_mps"],
        destination=snapshot.get("destination"),
    )


def _snapshot_of(pose):

    return {
        "vehicle_id": pose.vehicle_id, "node": pose.node, "segment": pose.segment,
        "distance_along_m": pose.s, "path": list(pose.path), "speed_mps": pose.speed_mps,
    }


# =========================================================
# THE BUS
# =========================================================

class Bus:

    def __init__(self, world):

        self.world = world
        self.lock = threading.RLock()
        self.lane_of_segment = {
            segment: lane_id
            for lane_id, lane in world.shared_lanes.items()
            for segment in lane["segments"]
        }

        # Extension points for the autopilot (services/autopilot.py):
        #   refusal_hooks:  callable(vehicle, record, position) -> reason or None,
        #                   extra reasons a vehicle refuses a handover
        #   handover_hooks: callable(negotiation), after a negotiation ends
        # message_hooks: callable(message_dict), every message sent (Phase 5:
        # services/live.py pushes it to the SSE "v2v" channel). Kept across reset().
        self.refusal_hooks = []
        self.handover_hooks = []
        self.message_hooks = []

        self.reset()

    def reset(self):

        with self.lock:
            self.config = _default_config()
            self.now_ms = 0.0
            self.ticks = 0
            self._queue = []
            self._order = itertools.count()
            self.history = deque(maxlen=HISTORY_SIZE)
            self.inboxes = {}
            self.outboxes = {}
            self._seq = {}
            self._seen = {}
            self._links = {}
            self._sent_times = {}
            self.agents = {}
            self.rsus = {rid: RoadsideUnit(self, spec) for rid, spec in self.world.rsus.items()}
            self.negotiations = {}
            self.vehicle_ids_cache = []
            self._tick_positions = {}
            self.blackout = set()          # vehicles whose radio is down (scenarios)
            self.stats = {
                "sent": 0, "deliveries_attempted": 0, "delivered": 0, "lost": 0,
                "out_of_range": 0, "duplicate": 0, "stale": 0, "relayed": 0,
                "by_type": {},
            }

    # ---- configuration --------------------------------------------------------

    def update_config(self, values):

        errors = []
        changes = {}

        for key, value in (values or {}).items():
            if key == "rsu_enabled":
                if not isinstance(value, bool):
                    errors.append("rsu_enabled must be true or false.")
                else:
                    changes[key] = value
                continue
            if key not in CONFIG_LIMITS:
                errors.append(f"Unknown setting '{key}'.")
                continue
            try:
                number = float(value)
            except (TypeError, ValueError):
                errors.append(f"{key} must be a number.")
                continue
            low, high = CONFIG_LIMITS[key]
            if not low <= number <= high:
                errors.append(f"{key} must be between {low:g} and {high:g}.")
                continue
            changes[key] = number

        with self.lock:
            merged = {**self.config, **changes}
            if merged["latency_min_ms"] > merged["latency_max_ms"]:
                errors.append("latency_min_ms must not be greater than latency_max_ms.")
            if errors:
                raise V2VError(" ".join(errors))
            self.config = merged
            return dict(self.config)

    # ---- positions ---------------------------------------------------------------

    def _positions(self):
        """Vehicle pose snapshots by ID. Call WITHOUT holding the bus lock."""

        return {p["vehicle_id"]: p for p in world_module.engine.poses_snapshot()}

    def _node_position(self, node_id, positions):

        if node_id in self.rsus:
            rsu = self.rsus[node_id]
            return {"x": rsu.x, "y": rsu.y}

        return positions.get(node_id)

    # ---- links ---------------------------------------------------------------------

    def _link(self, sender, receiver):

        return self._links.setdefault((sender, receiver), {
            "quality": 1.0, "attempts": 0, "received": 0, "lost": 0, "last_ms": None,
        })

    def link_quality(self, sender, receiver):

        link = self._links.get((sender, receiver))
        return link["quality"] if link else 1.0

    def _hop(self, sender, receiver):
        """One radio hop: (lost?, latency_ms). Updates the link's quality."""

        lost = simrng.stream("v2v.loss").random() < self.config["loss_rate"]
        latency = simrng.stream("v2v.latency").uniform(
            self.config["latency_min_ms"], self.config["latency_max_ms"])

        # Scenario fault injection: a vehicle whose radio is blacked
        # out neither sends nor receives (drawn after the random
        # numbers, so the rest of the run is not shifted).
        if sender in self.blackout or receiver in self.blackout:
            lost = True

        link = self._link(sender, receiver)
        link["attempts"] += 1
        link["last_ms"] = self.now_ms
        link["quality"] = 0.9 * link["quality"] + (0.0 if lost else 0.1)
        if lost:
            link["lost"] += 1
        else:
            link["received"] += 1

        return lost, latency

    # ---- sending -----------------------------------------------------------------------

    def send(self, sender, recipient, msg_type, payload, priority=None, ttl_ms=None,
             msg_id=None, positions=None):
        """Put a message on the radio. Returns the Message (deliveries filled in)."""

        if msg_type not in MESSAGE_TYPES:
            raise V2VError(f"Unknown message type {msg_type}. Allowed: {', '.join(MESSAGE_TYPES)}.")

        outside_tick = positions is None and not self._lock_held()

        if outside_tick:
            positions = self._positions()

        with self.lock:

            if outside_tick:
                # Sent from the API, not from an agent during a tick:
                # use the current fleet, not the last tick's.
                self.vehicle_ids_cache = fleet.vehicle_ids()

            if positions is None:
                positions = self._tick_positions

            default_priority, default_ttl, _ = MESSAGE_TYPES[msg_type]

            seq = self._seq.get(sender, 0) + 1
            self._seq[sender] = seq

            message = Message(
                msg_id=msg_id or str(uuid.UUID(int=simrng.stream("v2v.ids").getrandbits(128))),
                msg_type=msg_type, sender=sender, recipient=recipient, seq=seq,
                timestamp_utc=utc_iso(milliseconds=True), sent_at_ms=self.now_ms,
                ttl_ms=float(ttl_ms if ttl_ms is not None else default_ttl),
                priority=int(priority if priority is not None else default_priority),
                payload=payload or {}, signature_stub=signature_of(payload or {}),
            )

            self.stats["sent"] += 1
            self.stats["by_type"][msg_type] = self.stats["by_type"].get(msg_type, 0) + 1
            self._sent_times.setdefault(sender, deque(maxlen=400)).append(self.now_ms)

            if sender not in self.rsus:
                self.outboxes.setdefault(sender, deque(maxlen=INBOX_SIZE)).append(message.to_dict(deliveries=False))

            self.history.append(message)

            for receiver in self._receivers(sender, recipient):
                self._schedule(message, receiver, positions)

        # Outside the lock: persistence and the SSE hook are each
        # non-blocking (a queue put), but never hold the bus lock
        # for anything that is not pure bus bookkeeping.
        data = message.to_dict()

        db.record(
            "v2v_messages", msg_id=data["msg_id"], timestamp_utc=data["timestamp_utc"],
            msg_type=data["msg_type"], sender=data["sender"], recipient=data["recipient"],
            priority=data["priority"], payload_json=json.dumps(data["payload"], default=str),
            deliveries_json=json.dumps(data["deliveries"], default=str),
        )

        for hook in list(self.message_hooks):
            try:
                hook(data)
            except Exception as exc:
                print(f"[v2v] message hook failed: {exc}")

        return message

    def _lock_held(self):
        # RLock has no public "owned by me" check; _is_owned is the
        # CPython implementation detail threading.Condition uses.
        return self.lock._is_owned()

    def _receivers(self, sender, recipient):

        if recipient == BROADCAST:
            others = [v for v in self.vehicle_ids_cache if v != sender]
            if self.config["rsu_enabled"]:
                others += [r for r in self.rsus if r != sender]
            return others

        return [recipient]

    def _schedule(self, message, receiver, positions):

        self.stats["deliveries_attempted"] += 1

        delivery = {"to": receiver, "outcome": "pending", "via": None, "latency_ms": None}
        message.deliveries.append(delivery)

        a = self._node_position(message.sender, positions)
        b = self._node_position(receiver, positions)

        # Vehicle <-> vehicle uses the V2V range; anything with a
        # roadside unit uses that unit's range (none when disabled).
        direct_range = self.config["comm_range_m"]
        for node in (message.sender, receiver):
            if node in self.rsus:
                direct_range = self.rsus[node].range_m if self.config["rsu_enabled"] else -1.0

        if a is not None and b is not None and _distance(a, b) <= direct_range:
            hops = [(message.sender, receiver)]
        else:
            relay = self._relay_for(message, receiver, a, b)
            if relay is None:
                delivery["outcome"] = "out_of_range"
                self.stats["out_of_range"] += 1
                return
            hops = [(message.sender, relay), (relay, receiver)]
            delivery["via"] = relay
            self.stats["relayed"] += 1

        latency = 0.0
        for hop_from, hop_to in hops:
            lost, hop_latency = self._hop(hop_from, hop_to)
            latency += hop_latency
            if lost:
                delivery["outcome"] = "lost"
                delivery["lost_on_hop"] = f"{hop_from}->{hop_to}"
                self.stats["lost"] += 1
                return

        delivery["latency_ms"] = round(latency, 1)
        heapq.heappush(self._queue, (self.now_ms + latency, next(self._order), message, receiver, delivery))

    def _relay_for(self, message, receiver, a, b):

        if (
            message.recipient == BROADCAST
            or not self.config["rsu_enabled"]
            or a is None or b is None
            or message.sender in self.rsus or receiver in self.rsus
        ):
            return None

        for rsu in self.rsus.values():
            here = {"x": rsu.x, "y": rsu.y}
            if _distance(a, here) <= rsu.range_m and _distance(b, here) <= rsu.range_m:
                return rsu.id

        return None

    def publish_bsm(self, vehicle, bsm):
        """Broadcast a J2735-styled BSM from `vehicle` (called when its safety state changes)."""

        try:
            self.send(vehicle, BROADCAST, "BSM", bsm)
        except V2VError:
            pass

    # ---- one tick ------------------------------------------------------------------------

    def on_engine_tick(self, dt):
        """World engine tick hook (runs inside the engine's tick)."""

        self.tick(dt, self._positions())

    def tick(self, dt, positions=None):

        if positions is None:
            positions = self._positions()

        with self.lock:

            self._tick_positions = positions
            self.now_ms += dt * 1000
            self.ticks += 1
            now = self.now_ms

            self.vehicle_ids_cache = fleet.vehicle_ids()

            for vehicle in self.vehicle_ids_cache:
                if vehicle not in self.agents:
                    self.agents[vehicle] = VehicleAgent(self, vehicle)

            for vehicle in [v for v in self.agents if v not in self.vehicle_ids_cache]:
                self._forget(vehicle)

            while self._queue and self._queue[0][0] <= now:
                _, _, message, receiver, delivery = heapq.heappop(self._queue)
                self._deliver(message, receiver, delivery, now)

            for vehicle in self.vehicle_ids_cache:
                try:
                    record = state.get_vehicle_state(vehicle)
                except ValueError:
                    continue
                self.agents[vehicle].tick(now, positions, record)

            for rsu in self.rsus.values():
                rsu.tick(now)

            self._negotiation_timers(now)

    def _deliver(self, message, receiver, delivery, now):

        if now - message.sent_at_ms > message.ttl_ms:
            delivery["outcome"] = "stale"
            self.stats["stale"] += 1
            return

        seen_set, seen_order = self._seen.setdefault(receiver, (set(), deque()))

        if message.msg_id in seen_set:
            delivery["outcome"] = "duplicate"
            self.stats["duplicate"] += 1
            return

        seen_set.add(message.msg_id)
        seen_order.append(message.msg_id)
        if len(seen_order) > SEEN_IDS_PER_RECEIVER:
            seen_set.discard(seen_order.popleft())

        delivery["outcome"] = "delivered"
        delivery["received_at_ms"] = round(now, 1)
        self.stats["delivered"] += 1

        if receiver in self.rsus:
            self.rsus[receiver].on_message(message, now)
            return

        self.inboxes.setdefault(receiver, deque(maxlen=INBOX_SIZE)).append({
            **message.to_dict(deliveries=False), "received_at_ms": round(now, 1), "via": delivery["via"],
        })

        agent = self.agents.get(receiver)
        if agent is not None:
            agent.heard(message.sender, message, now)
            agent.on_message(message, now)

    # ---- handover negotiation --------------------------------------------------------------

    def start_handover(self, from_vehicle, to_vehicle):

        positions = self._positions()

        with self.lock:

            self._tick_positions = positions
            self.vehicle_ids_cache = fleet.vehicle_ids()

            for vehicle in (from_vehicle, to_vehicle):
                if vehicle not in self.agents:
                    self.agents[vehicle] = VehicleAgent(self, vehicle)

            negotiation_id = str(uuid.UUID(int=simrng.stream("v2v.ids").getrandbits(128)))
            timeout = self.config["handover_timeout_ms"]

            negotiation = Negotiation(
                negotiation_id=negotiation_id, from_vehicle=from_vehicle, to_vehicle=to_vehicle,
                started_ms=self.now_ms, deadline_ms=self.now_ms + timeout,
            )
            self.negotiations[negotiation_id] = negotiation

            if len(self.negotiations) > 50:
                for old in list(self.negotiations)[:-50]:
                    del self.negotiations[old]

            self._send_handover(negotiation, "HANDOVER_REQUEST", from_vehicle, to_vehicle)

            return negotiation

    def _send_handover(self, negotiation, kind, sender, recipient, extra=None):

        message = self.send(sender, recipient, kind, {
            "negotiation_id": negotiation.negotiation_id,
            "from_vehicle": negotiation.from_vehicle,
            "to_vehicle": negotiation.to_vehicle,
            **(extra or {}),
        }, positions=self._tick_positions)

        negotiation.messages.append({
            "msg_id": message.msg_id, "msg_type": kind,
            "via": message.deliveries[0]["via"] if message.deliveries else None,
            "outcome_at_send": message.deliveries[0]["outcome"] if message.deliveries else None,
        })

        return message

    def on_handover_message(self, agent, message, now):

        payload = message.payload or {}
        negotiation = self.negotiations.get(payload.get("negotiation_id"))

        if negotiation is None:
            return

        kind = message.msg_type

        if kind == "HANDOVER_REQUEST" and agent.id == negotiation.to_vehicle:
            reason = self._handover_refusal(agent.id)
            if reason:
                self._send_handover(negotiation, "HANDOVER_REJECT", agent.id, message.sender, {"reason": reason})
            else:
                self._send_handover(negotiation, "HANDOVER_ACCEPT", agent.id, message.sender)

        elif kind == "HANDOVER_ACCEPT" and agent.id == negotiation.from_vehicle:
            if negotiation.status == "requesting":
                negotiation.status = "accepted"
                negotiation.complete_attempts = 1
                negotiation.deadline_ms = now + self.config["handover_timeout_ms"]
                self._send_handover(negotiation, "HANDOVER_COMPLETE", agent.id, message.sender)

        elif kind == "HANDOVER_REJECT" and agent.id == negotiation.from_vehicle:
            if not negotiation.done:
                self._finish(negotiation, "rejected", payload.get("reason"), now)

        elif kind == "HANDOVER_COMPLETE" and agent.id == negotiation.to_vehicle:
            if not negotiation.done:
                try:
                    negotiation.next_state = state.prepare_for_handover(agent.id)
                    self._finish(negotiation, "completed", None, now)
                except state.TransitionBlocked as exc:
                    self._finish(negotiation, "rejected", exc.code, now)
            self.send(agent.id, message.sender, "ACK", {
                "ack_of": message.msg_id, "negotiation_id": negotiation.negotiation_id,
                "status": negotiation.status,
            }, positions=self._tick_positions)

    def _handover_refusal(self, vehicle):
        """Why `vehicle` cannot take over now, or None."""

        try:
            record = state.get_vehicle_state(vehicle)
        except ValueError:
            return "UNKNOWN_VEHICLE"

        if record.get("emergency_stop"):
            return "EMERGENCY_ACTIVE"
        if record.get("fault"):
            return "VEHICLE_FAULT"
        if record.get("comm_hold"):
            return "COMM_HOLD"
        if state.in_mission(record):
            return "BUSY_IN_MISSION"

        # Stopped somewhere on the road (not parked at a stop): taking
        # over would teleport it to Stop 1. (A manually driven vehicle
        # parked at Stop 3 is still returned to Stop 1 by the handover,
        # as before; the autopilot drives the return leg instead.)
        position = self._tick_positions.get(vehicle)
        if position is not None and position.get("segment"):
            return "NOT_AT_STOP_1"

        battery = record.get("battery_level")
        try:
            if battery is not None and float(battery) < settings.battery_low_pct:
                return "LOW_BATTERY"
        except (TypeError, ValueError):
            pass

        for hook in self.refusal_hooks:
            reason = hook(vehicle, record, position)
            if reason:
                return reason

        return None

    def _negotiation_timers(self, now):

        for negotiation in self.negotiations.values():

            if negotiation.done or now < negotiation.deadline_ms:
                continue

            timeout = self.config["handover_timeout_ms"]

            if negotiation.status == "requesting" and negotiation.request_attempts < 2:
                negotiation.request_attempts += 1
                negotiation.deadline_ms = now + timeout
                self._send_handover(negotiation, "HANDOVER_REQUEST",
                                    negotiation.from_vehicle, negotiation.to_vehicle)

            elif negotiation.status == "accepted" and negotiation.complete_attempts < 2:
                negotiation.complete_attempts += 1
                negotiation.deadline_ms = now + timeout
                self._send_handover(negotiation, "HANDOVER_COMPLETE",
                                    negotiation.from_vehicle, negotiation.to_vehicle)

            else:
                self._finish(negotiation, "timeout", "NO_ANSWER", now)

    def _finish(self, negotiation, status, reason, now):

        negotiation.status = status
        negotiation.reason = reason
        negotiation.finished_ms = now
        negotiation.event.set()

        for hook in self.handover_hooks:
            hook(negotiation)

        if status != "completed":
            events.add_event(
                f"[V2V] Handover {events.vehicle_name(negotiation.from_vehicle)} -> "
                f"{events.vehicle_name(negotiation.to_vehicle)} {status}"
                + (f": {reason}" if reason else "") + ".",
                negotiation.from_vehicle, event_type="v2v", severity="warning",
                details={"v2v": negotiation.to_dict()},
            )

    def wait_for(self, negotiation, step_s=0.1):
        """Block until the negotiation is done.

        With the world engine running, its thread delivers the
        messages and this just waits. Without it (tests,
        WORLD_TICK_MS=0), the bus is stepped here on its own clock.
        """

        limit_ms = self.config["handover_timeout_ms"] * 4 + 1000

        if world_module.engine.status()["running"]:
            # Simulated time runs time_scale times faster than the wall clock.
            scale = max(world_module.engine.time_scale, 0.01)
            negotiation.event.wait(timeout=limit_ms / 1000 / scale + 2)
        else:
            stepped = 0.0
            while not negotiation.done and stepped <= limit_ms:
                self.tick(step_s)
                stepped += step_s * 1000

        with self.lock:
            if not negotiation.done:
                self._finish(negotiation, "timeout", "NO_ANSWER", self.now_ms)
            return negotiation

    # ---- loading dock ----------------------------------------------------------------------

    def dock(self):

        for rsu in self.rsus.values():
            if rsu.manages_stop:
                return rsu
        return None

    def slot_holder_conflict(self, vehicle):
        """Another vehicle holding the loading-point slot, or None."""

        with self.lock:
            dock = self.dock()
            if dock is None or not self.config["rsu_enabled"]:
                return None
            holder = dock.holder
            if holder is None or holder == vehicle or holder not in fleet.vehicle_ids():
                return None
            return holder

    def claim_slot(self, vehicle):

        with self.lock:
            dock = self.dock()
            if dock is not None:
                dock.claim(vehicle)
            agent = self.agents.get(vehicle)
            if agent is not None:
                agent.slot.update(granted=True, queue_position=0)

    # ---- registry changes ----------------------------------------------------------------------

    def _forget(self, vehicle):

        self.agents.pop(vehicle, None)
        self.inboxes.pop(vehicle, None)
        self.outboxes.pop(vehicle, None)
        self._seen.pop(vehicle, None)
        self._sent_times.pop(vehicle, None)
        for key in [k for k in self._links if vehicle in k]:
            del self._links[key]
        for rsu in self.rsus.values():
            rsu.last_cam.pop(vehicle, None)
            if rsu.holder == vehicle:
                rsu.holder = None
            if vehicle in rsu.queue:
                rsu.queue.remove(vehicle)

    def forget(self, *vehicles):

        with self.lock:
            for vehicle in vehicles:
                self._forget(vehicle)

    # ---- advice ---------------------------------------------------------------------------------

    def advise(self, vehicle_id, pose, record):
        """World engine advisor: platoon speed cap, shared-lane hold point."""

        with self.lock:
            agent = self.agents.get(vehicle_id)
            if agent is None:
                return {}
            return agent.advise(pose, self.now_ms)

    # ---- views for the API --------------------------------------------------------------------

    def messages(self, limit=50, msg_type=None, vehicle=None):

        with self.lock:
            result = []
            for message in reversed(self.history):
                if msg_type and message.msg_type != msg_type:
                    continue
                if vehicle and vehicle not in (message.sender, message.recipient) \
                        and not any(d["to"] == vehicle for d in message.deliveries):
                    continue
                result.append(message.to_dict())
                if len(result) >= limit:
                    break
            return result

    def inbox(self, vehicle, limit=50):

        with self.lock:
            return list(reversed(list(self.inboxes.get(vehicle, []))))[:limit]

    def links(self):

        positions = self._positions()

        with self.lock:
            result = []
            for (sender, receiver), link in sorted(self._links.items()):
                a = self._node_position(sender, positions)
                b = self._node_position(receiver, positions)
                agent = self.agents.get(receiver)
                link_state = None
                if agent is not None and sender in agent.neighbours:
                    link_state = agent.neighbours[sender]["state"]
                elif receiver in self.rsus:
                    link_state = "connected" if self.rsus[receiver].heard_recently(sender, self.now_ms) else "silent"
                result.append({
                    "from": sender, "to": receiver,
                    "state": link_state,
                    "quality": round(link["quality"], 3),
                    "distance_m": round(_distance(a, b), 1) if a and b else None,
                    "attempts": link["attempts"], "received": link["received"], "lost": link["lost"],
                    "last_activity_ms_ago": round(self.now_ms - link["last_ms"], 1) if link["last_ms"] is not None else None,
                })
            return result

    def messages_per_second(self, sender, window_ms=5000.0):

        times = self._sent_times.get(sender)
        if not times:
            return 0.0
        recent = sum(1 for t in times if self.now_ms - t <= window_ms)
        span = min(window_ms, max(self.now_ms, 1.0))
        return round(recent / (span / 1000), 2)

    def agents_view(self):

        with self.lock:
            return {
                "clock_ms": round(self.now_ms, 1),
                "vehicles": [self.agents[v].view(self.now_ms) for v in self.vehicle_ids_cache if v in self.agents],
                "rsus": [rsu.view(self.now_ms) for rsu in self.rsus.values()],
                "negotiations": [n.to_dict() for n in list(self.negotiations.values())[-10:]],
            }

    def statistics(self):

        with self.lock:
            return {
                **{k: v for k, v in self.stats.items() if k != "by_type"},
                "by_type": dict(sorted(self.stats["by_type"].items())),
                "in_flight": len(self._queue),
                "clock_ms": round(self.now_ms, 1),
                "ticks": self.ticks,
                "config": dict(self.config),
            }

    def communication_status(self):
        """Per-vehicle V2V status for /api/communication."""

        positions = self._positions()

        with self.lock:
            now = self.now_ms
            result = {}
            for vehicle in fleet.vehicle_ids():
                agent = self.agents.get(vehicle)
                if agent is None or not agent.ticked:
                    result[vehicle] = {"status": "unknown", "neighbours": 0, "connected": 0, "lost": 0,
                                       "link_quality": None, "messages_per_second": 0.0, "rsu_link": False,
                                       "comm_hold": False}
                    continue
                in_range = {v: n for v, n in agent.neighbours.items()
                            if n["state"] in ("connected", "degraded", "pending", "lost")}
                connected = [v for v, n in in_range.items() if n["state"] == "connected"]
                degraded = [v for v, n in in_range.items() if n["state"] == "degraded"]
                lost = [v for v, n in in_range.items() if n["state"] == "lost"]
                rsu_link = any(
                    rsu.heard_recently(vehicle, now) and _distance(positions.get(vehicle), {"x": rsu.x, "y": rsu.y}) <= rsu.range_m
                    for rsu in self.rsus.values()
                ) if self.config["rsu_enabled"] else False
                qualities = [self.link_quality(v, vehicle) for v in in_range]
                if agent.comm_hold:
                    status = "lost"
                elif lost or degraded:
                    status = "degraded"
                elif connected or rsu_link:
                    status = "connected"
                else:
                    status = "isolated"
                result[vehicle] = {
                    "status": status,
                    "neighbours": len(in_range),
                    "connected": len(connected),
                    "lost": len(lost),
                    "link_quality": round(sum(qualities) / len(qualities), 3) if qualities else None,
                    "messages_per_second": self.messages_per_second(vehicle),
                    "rsu_link": rsu_link,
                    "comm_hold": agent.comm_hold,
                }
            return result


# =========================================================
# THE SINGLETON, WIRED INTO THE WORLD ENGINE
# =========================================================

bus = Bus(world_module.world)

world_module.engine.add_tick_hook(bus.on_engine_tick)

world_module.engine.advisor = bus.advise
