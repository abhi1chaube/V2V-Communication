# =========================================================
# WORLD MODEL: ROUTE MAP, CONTINUOUS POSITIONS, KINEMATICS
# =========================================================
#
# The yard is a directed graph of nodes (stops, junctions,
# corners, the charging bay) and segments (one-way lanes with a
# length in metres), loaded from BACKEND/config/world.json
# (setting WORLD_FILE). It is validated when the backend
# starts; a broken file stops startup with a clear message.
#
# Every vehicle has a pose: the segment it is on and how far
# along it is (metres), or the node it is parked at, plus its
# actual speed. The WorldEngine moves the poses forward on a
# fixed tick (setting WORLD_TICK_MS, default 200 ms; 0 = off),
# with acceleration and braking limits from world.json.
#
# In this phase the engine FOLLOWS the commanded vehicle state
# (services/state.py): START gives a commanded speed and the
# MOVING_TO_LOADING mission, so the engine drives the vehicle
# along the route to Stop 2; an emergency stop or an obstacle
# brakes it; a command that moves a vehicle to another stop
# (for example /api/loading-point) snaps its pose there. The
# engine does not change the mission itself: the autopilot
# (upgrade Phase 4) will. It publishes the derived values into
# state: destination, distance_to_destination, eta_seconds,
# next_action, and (when the backend owns state) last_seen as
# the simulated vehicle's heartbeat.
#
# step() is a pure function of (world, pose, dt, target speed),
# so motion is deterministic and testable with a fixed tick.
# =========================================================

import heapq
import json
import math
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from . import fleet
from . import state
from .settings import BACKEND_DIR, settings
from .timeutil import utc_iso


DEFAULT_WORLD_FILE = BACKEND_DIR / "config" / "world.json"

# Stops every world must have (the mission uses them).
REQUIRED_STOPS = ("stop_1", "stop_2", "stop_3")

ARRIVAL_TOLERANCE_M = 0.02

# A vehicle told to hold stops this far short of the hold point.
HOLD_MARGIN_M = 0.1

# While RUNNING with one of these missions, the engine drives the
# vehicle to that stop (the autopilot sets the mission per leg).
MISSION_GOALS = {
    "MOVING_TO_LOADING": "stop_2",
    "MOVING_TO_WAREHOUSE": "stop_3",
    "RETURNING_TO_STOP_1": "stop_1",
    "MOVING_TO_CHARGING": "charging",
}

# Stops with several parking bays side by side (the depot and the
# charging bay): a vehicle parked there is not in the way of one
# arriving, so it is never "the vehicle ahead" (gap, platoon).
MULTI_BAY_STOPS = ("stop_1", "charging")

# Time scale (demo): one wall-clock tick runs this many simulated
# ticks of at most WORLD_TICK_MS each, so the radio's timeouts
# and latencies stay meaningful at 8x.
TIME_SCALE_LIMITS = (0.25, 8.0)


class WorldError(ValueError):
    """world.json is invalid, or a route does not exist."""


# =========================================================
# MAP
# =========================================================

@dataclass(frozen=True)
class Node:

    id: str
    x: float
    y: float
    kind: str
    stop: Optional[str]
    label: str


@dataclass(frozen=True)
class Segment:

    id: str
    start: str
    end: str
    length: float
    lane: Optional[str]
    max_speed_mps: Optional[float]
    dx: float            # unit direction vector
    dy: float

    @property
    def heading_deg(self):
        # 0 = east (+x), 90 = north (+y), counter-clockwise.
        return math.degrees(math.atan2(self.dy, self.dx)) % 360


class World:

    def __init__(self, data, source="world.json"):

        self.source = str(source)

        try:
            self._parse(data)
        except WorldError:
            raise
        except (KeyError, TypeError, ValueError) as exc:
            raise WorldError(f"{self.source}: {exc}") from None

        self._validate_routes()

    # ---- parsing ------------------------------------------------

    def _parse(self, data):

        if not isinstance(data, dict):
            raise WorldError(f"{self.source}: the top level must be an object.")

        self.name = str(data.get("name") or "World")
        self.description = str(data.get("description") or "")

        kin = data.get("kinematics") or {}
        self.kinematics = {
            "max_accel_mps2": float(kin.get("max_accel_mps2", 1.0)),
            "max_decel_mps2": float(kin.get("max_decel_mps2", 2.0)),
            "emergency_decel_mps2": float(kin.get("emergency_decel_mps2", 6.0)),
            "creep_speed_mps": float(kin.get("creep_speed_mps", 0.3)),
        }
        for key, value in self.kinematics.items():
            if value <= 0:
                raise WorldError(f"{self.source}: kinematics.{key} must be > 0.")

        raw_nodes = data.get("nodes") or {}
        if not isinstance(raw_nodes, dict) or not raw_nodes:
            raise WorldError(f"{self.source}: 'nodes' must be a non-empty object.")

        self.nodes = {}
        self.stop_nodes = {}

        for node_id, raw in raw_nodes.items():
            node = Node(
                id=str(node_id),
                x=float(raw["x"]),
                y=float(raw["y"]),
                kind=str(raw.get("kind") or "node"),
                stop=(str(raw["stop"]) if raw.get("stop") else None),
                label=str(raw.get("label") or node_id),
            )
            self.nodes[node.id] = node
            if node.stop:
                if node.stop in self.stop_nodes:
                    raise WorldError(f"{self.source}: stop '{node.stop}' is on two nodes.")
                self.stop_nodes[node.stop] = node.id

        missing = [stop for stop in REQUIRED_STOPS if stop not in self.stop_nodes]
        if missing:
            raise WorldError(f"{self.source}: missing required stops: {', '.join(missing)}.")

        self.segments = {}
        self.outgoing = {node_id: [] for node_id in self.nodes}

        for raw in data.get("segments") or []:
            segment_id = str(raw["id"])
            if segment_id in self.segments:
                raise WorldError(f"{self.source}: duplicate segment id '{segment_id}'.")
            start, end = str(raw["from"]), str(raw["to"])
            for node_id in (start, end):
                if node_id not in self.nodes:
                    raise WorldError(f"{self.source}: segment '{segment_id}' uses unknown node '{node_id}'.")
            a, b = self.nodes[start], self.nodes[end]
            geometric = math.hypot(b.x - a.x, b.y - a.y)
            length = float(raw.get("length") or geometric)
            if length <= 0:
                raise WorldError(f"{self.source}: segment '{segment_id}' has zero length.")
            limit = raw.get("max_speed_kmh")
            segment = Segment(
                id=segment_id,
                start=start,
                end=end,
                length=length,
                lane=(str(raw["lane"]) if raw.get("lane") else None),
                max_speed_mps=(float(limit) / 3.6 if limit else None),
                dx=((b.x - a.x) / geometric) if geometric else 1.0,
                dy=((b.y - a.y) / geometric) if geometric else 0.0,
            )
            self.segments[segment_id] = segment
            self.outgoing[start].append(segment)

        if not self.segments:
            raise WorldError(f"{self.source}: 'segments' must not be empty.")

        self.shared_lanes = {}
        for lane_id, raw in (data.get("shared_lanes") or {}).items():
            members = [str(s) for s in raw.get("segments", [])]
            unknown = [s for s in members if s not in self.segments]
            if unknown:
                raise WorldError(f"{self.source}: shared lane '{lane_id}' lists unknown segments {unknown}.")
            self.shared_lanes[str(lane_id)] = {
                "label": str(raw.get("label") or lane_id),
                "segments": members,
            }

        self.next_stop = {}
        for node_id, target in (data.get("next_stop") or {}).items():
            if node_id not in self.nodes or target not in self.nodes:
                raise WorldError(f"{self.source}: next_stop {node_id} -> {target} uses an unknown node.")
            self.next_stop[str(node_id)] = str(target)

        # Roadside units (V2I): relay V2V coordination messages and
        # manage a stop (the loading dock). Optional.
        self.rsus = {}
        for raw in data.get("rsus") or []:
            rsu_id = str(raw["id"])
            if rsu_id in self.rsus or rsu_id in self.nodes:
                raise WorldError(f"{self.source}: duplicate roadside unit id '{rsu_id}'.")
            range_m = float(raw.get("range_m", 150))
            if range_m <= 0:
                raise WorldError(f"{self.source}: roadside unit '{rsu_id}' needs range_m > 0.")
            manages = raw.get("manages_stop")
            if manages and manages not in self.stop_nodes:
                raise WorldError(f"{self.source}: roadside unit '{rsu_id}' manages unknown stop '{manages}'.")
            self.rsus[rsu_id] = {
                "id": rsu_id,
                "x": float(raw["x"]),
                "y": float(raw["y"]),
                "range_m": range_m,
                "label": str(raw.get("label") or rsu_id),
                "manages_stop": manages,
            }

    def _validate_routes(self):

        s1, s2, s3 = (self.stop_nodes[s] for s in REQUIRED_STOPS)

        # The mission loop must be drivable, including the return leg.
        for start, goal in ((s1, s2), (s2, s3), (s3, s1)):
            self.route(start, goal)

        for start, goal in self.next_stop.items():
            self.route(start, goal)

    # ---- queries ------------------------------------------------

    def route(self, start, goal):
        """Shortest list of segment IDs from node `start` to node `goal`."""

        if start not in self.nodes or goal not in self.nodes:
            raise WorldError(f"Unknown node in route {start} -> {goal}.")

        if start == goal:
            return []

        best = {start: 0.0}
        previous = {}
        queue = [(0.0, start)]

        while queue:
            distance, node_id = heapq.heappop(queue)
            if node_id == goal:
                break
            if distance > best.get(node_id, math.inf):
                continue
            for segment in self.outgoing[node_id]:
                candidate = distance + segment.length
                if candidate < best.get(segment.end, math.inf):
                    best[segment.end] = candidate
                    previous[segment.end] = segment.id
                    heapq.heappush(queue, (candidate, segment.end))

        if goal not in previous:
            raise WorldError(f"No route from {start} to {goal}.")

        path = []
        node_id = goal
        while node_id != start:
            segment_id = previous[node_id]
            path.append(segment_id)
            node_id = self.segments[segment_id].start
        return list(reversed(path))

    def node_of_stop(self, stop):
        return self.stop_nodes.get(str(stop or "").strip().lower())

    def stop_of_node(self, node_id):
        node = self.nodes.get(node_id)
        return node.stop if node else None

    def point(self, segment_id, s):
        """(x, y) at distance s along a segment."""
        segment = self.segments[segment_id]
        a, b = self.nodes[segment.start], self.nodes[segment.end]
        fraction = 0.0 if segment.length == 0 else max(0.0, min(1.0, s / segment.length))
        return (a.x + (b.x - a.x) * fraction, a.y + (b.y - a.y) * fraction)

    def describe(self):

        xs = [node.x for node in self.nodes.values()]
        ys = [node.y for node in self.nodes.values()]

        return {
            "name": self.name,
            "description": self.description,
            "units": {"distance": "m", "speed": "km/h", "heading": "degrees, 0 = +x, counter-clockwise"},
            "bounds": {"min_x": min(xs), "max_x": max(xs), "min_y": min(ys), "max_y": max(ys)},
            "kinematics": dict(self.kinematics),
            "nodes": [
                {"id": n.id, "x": n.x, "y": n.y, "kind": n.kind, "stop": n.stop, "label": n.label}
                for n in self.nodes.values()
            ],
            "segments": [
                {
                    "id": s.id, "from": s.start, "to": s.end,
                    "length_m": round(s.length, 2),
                    "lane": s.lane,
                    "max_speed_kmh": round(s.max_speed_mps * 3.6, 1) if s.max_speed_mps else None,
                    "heading_deg": round(s.heading_deg, 1),
                }
                for s in self.segments.values()
            ],
            "shared_lanes": self.shared_lanes,
            "rsus": list(self.rsus.values()),
            "stops": dict(self.stop_nodes),
            "next_stop": dict(self.next_stop),
            "legs": {
                f"{self.stop_of_node(a) or a}->{self.stop_of_node(b) or b}": {
                    "segments": self.route(a, b),
                    "length_m": round(sum(self.segments[x].length for x in self.route(a, b)), 2),
                }
                for a, b in self.next_stop.items()
            },
        }


def load_world(path=None):

    path = Path(path or settings.world_file or DEFAULT_WORLD_FILE)

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise WorldError(f"World file not found: {path}") from None
    except json.JSONDecodeError as exc:
        raise WorldError(f"{path} is not valid JSON: {exc}") from None

    return World(data, source=path.name)


# =========================================================
# POSE AND KINEMATICS (PURE)
# =========================================================

@dataclass
class Pose:

    vehicle_id: str
    node: Optional[str] = None            # parked at this node, or None on a segment
    segment: Optional[str] = None
    s: float = 0.0                        # metres along `segment`
    path: list = field(default_factory=list)   # remaining segments, current first
    speed_mps: float = 0.0
    target_mps: float = 0.0
    destination: Optional[str] = None     # node the current path ends at
    heading_deg: float = 0.0
    reversing: bool = False
    last_placement: int = 0               # state.placement_seq() last seen by the engine
    next_action: str = "IDLE"


def remaining_distance(world, pose):
    """Metres left on the current path."""

    if not pose.path:
        return 0.0

    total = world.segments[pose.path[0]].length - pose.s

    for segment_id in pose.path[1:]:
        total += world.segments[segment_id].length

    return max(0.0, total)


def park(pose, node_id):
    """Put the pose at rest on a node (also used to snap after a command)."""

    pose.node = node_id
    pose.segment = None
    pose.s = 0.0
    pose.path = []
    pose.speed_mps = 0.0
    pose.reversing = False


def set_path(world, pose, path):
    """Start following `path` (segment IDs) from where the pose is."""

    pose.path = list(path)
    pose.reversing = False

    if not pose.path:
        return

    first = world.segments[pose.path[0]]

    if pose.segment != first.id:
        # Leaving a node onto the first segment.
        pose.segment = first.id
        pose.s = 0.0

    pose.node = None
    pose.destination = world.segments[pose.path[-1]].end


def step(world, pose, dt, target_mps, decel_mps2=None, stop_within_m=None):
    """Advance `pose` by `dt` seconds toward `target_mps`. Mutates and returns it.

    Accelerates and brakes within the world's kinematic limits,
    respects segment speed limits, and slows down early enough to
    stop exactly at the end of its path (no overshoot).

    `stop_within_m` is a hold point: the vehicle must stop within that
    many metres from where it is now (for example at a junction while it
    yields a shared lane). 0 keeps it where it is.
    """

    if stop_within_m is not None and not pose.reversing:
        return _step_to_hold_point(world, pose, dt, target_mps, decel_mps2, max(0.0, stop_within_m))

    kin = world.kinematics
    accel = kin["max_accel_mps2"]
    decel = decel_mps2 or kin["max_decel_mps2"]

    if pose.reversing:
        return _step_reverse(world, pose, dt, target_mps, accel, decel)

    target = max(0.0, target_mps) if pose.path else 0.0

    if pose.segment:
        limit = world.segments[pose.segment].max_speed_mps
        if limit:
            target = min(target, limit)

    v = pose.speed_mps

    if target > v:
        v = min(target, v + accel * dt)
    else:
        v = max(target, v - decel * dt)

    remaining = remaining_distance(world, pose)

    if pose.path and target > 0:
        # Fastest speed that still allows a normal stop at the end,
        # with a small creep speed so the vehicle actually arrives.
        cap = max(math.sqrt(2 * kin["max_decel_mps2"] * remaining), kin["creep_speed_mps"])
        v = min(v, cap)

    pose.speed_mps = v
    pose.target_mps = target

    _advance(world, pose, v * dt)

    if pose.segment:
        pose.heading_deg = world.segments[pose.segment].heading_deg

    return pose


def _step_to_hold_point(world, pose, dt, target_mps, decel_mps2, stop_within):
    """Like step(), but stop HOLD_MARGIN_M short of the hold point.

    The margin matters when the hold point is the end of a segment
    (a junction before a shared lane): reaching the end exactly would
    count as crossing onto the next segment.
    """

    kin = world.kinematics
    decel = decel_mps2 or kin["max_decel_mps2"]

    stop_within = max(0.0, stop_within - HOLD_MARGIN_M)

    if stop_within <= ARRIVAL_TOLERANCE_M:
        # At the hold point: brake to a stop without moving on.
        pose.speed_mps = max(0.0, pose.speed_mps - kin["emergency_decel_mps2"] * dt)
        if pose.speed_mps <= 0.05:
            pose.speed_mps = 0.0
        pose.target_mps = 0.0
        return pose

    target = max(0.0, target_mps) if pose.path else 0.0

    if pose.segment:
        limit = world.segments[pose.segment].max_speed_mps
        if limit:
            target = min(target, limit)

    v = pose.speed_mps
    v = min(target, v + kin["max_accel_mps2"] * dt) if target > v else max(target, v - decel * dt)

    # Must be able to stop at the hold point with normal braking.
    v = min(v, math.sqrt(2 * kin["max_decel_mps2"] * stop_within), max(target, 0.0))

    distance = min(v * dt, stop_within)

    pose.speed_mps = v
    pose.target_mps = target

    _advance(world, pose, distance)

    if pose.segment:
        pose.heading_deg = world.segments[pose.segment].heading_deg

    return pose


def path_gap(world, follower, leader_segment, leader_s):
    """Metres along the follower's path to a point (segment, s) ahead, or None."""

    if not follower.path:
        return None

    if follower.path[0] == leader_segment:
        gap = leader_s - follower.s
        return gap if gap >= 0 else None

    total = world.segments[follower.path[0]].length - follower.s

    for segment_id in follower.path[1:]:
        if segment_id == leader_segment:
            return total + leader_s
        total += world.segments[segment_id].length

    return None


def _advance(world, pose, distance):

    while pose.path and distance > 0:

        segment = world.segments[pose.path[0]]
        left = segment.length - pose.s

        if distance < left - ARRIVAL_TOLERANCE_M:
            pose.s += distance
            return

        distance -= left
        pose.heading_deg = segment.heading_deg
        pose.path.pop(0)

        if pose.path:
            pose.segment = pose.path[0]
            pose.s = 0.0
        else:
            park(pose, segment.end)        # arrived at the destination
            return

    # Close enough to the end without a full step: arrive.
    if pose.path and remaining_distance(world, pose) <= ARRIVAL_TOLERANCE_M:
        last = world.segments[pose.path[-1]]
        pose.heading_deg = last.heading_deg
        park(pose, last.end)


def _step_reverse(world, pose, dt, target_mps, accel, decel):
    """Manual BACKWARD: roll back along the current segment, stop at its start."""

    if not pose.segment:
        pose.reversing = False
        pose.speed_mps = 0.0
        return pose

    target = max(0.0, target_mps)
    v = pose.speed_mps
    v = min(target, v + accel * dt) if target > v else max(target, v - decel * dt)
    v = min(v, max(math.sqrt(2 * decel * pose.s), world.kinematics["creep_speed_mps"]))

    pose.speed_mps = v
    pose.target_mps = target
    pose.s -= v * dt

    if pose.s <= ARRIVAL_TOLERANCE_M:
        start = world.segments[pose.segment].start
        park(pose, start)
        pose.destination = None

    return pose


def position(world, pose):
    """(x, y) of a pose."""

    if pose.segment:
        return world.point(pose.segment, pose.s)

    node = world.nodes[pose.node]
    return (node.x, node.y)


def pose_snapshot(world, pose):
    """The pose as returned by the API."""

    x, y = position(world, pose)
    remaining = remaining_distance(world, pose)
    cruise = max(pose.speed_mps, pose.target_mps)
    arrived = not pose.path and pose.destination is not None and pose.node == pose.destination
    segment = world.segments.get(pose.segment) if pose.segment else None

    if pose.destination is None:
        eta = None
    elif arrived:
        eta = 0.0
    elif cruise > 0:
        eta = round(remaining / cruise, 1)
    else:
        eta = None

    return {
        "vehicle_id": pose.vehicle_id,
        "node": pose.node,
        "stop": world.stop_of_node(pose.node) if pose.node else None,
        "segment": pose.segment,
        "lane": segment.lane if segment else None,
        "distance_along_m": round(pose.s, 2),
        "x": round(x, 2),
        "y": round(y, 2),
        "heading_deg": round(pose.heading_deg, 1),
        "speed_mps": round(pose.speed_mps, 3),
        "speed_kmh": round(pose.speed_mps * 3.6, 2),
        "target_kmh": round(pose.target_mps * 3.6, 2),
        "reversing": pose.reversing,
        "path": list(pose.path),
        "destination": pose.destination,
        "destination_stop": world.stop_of_node(pose.destination) if pose.destination else None,
        "distance_to_destination_m": round(remaining, 1) if pose.destination else None,
        "eta_seconds": eta,
        "arrived": arrived,
        "next_action": pose.next_action,
    }


# =========================================================
# ENGINE: FOLLOWS THE COMMANDED STATE
# =========================================================

class WorldEngine:

    def __init__(self, world):

        self.world = world
        self.poses = {}
        self._lock = threading.RLock()
        self._thread = None
        self._stop = threading.Event()
        self.ticks = 0
        self.sim_seconds = 0.0
        self.tick_ms = 0
        self.last_tick_at = None
        self.last_error = None

        # For the autopilot watchdog: when the last tick finished
        # (time.monotonic), and a counter of ticks that raised.
        self.last_tick_mono = None
        self.error_count = 0
        self.last_exception = None

        self.time_scale = float(settings.time_scale)

        # Called after every tick with (dt). The V2V bus registers
        # here, so messages run on the same simulated clock.
        self.tick_hooks = []

        # Optional callable(vehicle_id, pose, record) -> dict with
        # "speed_cap_mps", "stop_within_m", "action" (V2V advice:
        # platoon gap keeping, shared-lane yielding). Set by v2v.
        self.advisor = None

    def add_tick_hook(self, hook):

        if hook not in self.tick_hooks:
            self.tick_hooks.append(hook)

    # ---- pose bookkeeping ----------------------------------------

    def _ensure(self, vehicle_id, record):

        pose = self.poses.get(vehicle_id)

        if pose is None:
            node = self.world.node_of_stop(record.get("location")) or self.world.stop_nodes["stop_1"]
            pose = Pose(vehicle_id=vehicle_id, node=node,
                        last_placement=state.placement_seq(vehicle_id))
            self.poses[vehicle_id] = pose

        return pose

    def place(self, vehicle_id, segment=None, s=0.0, node=None, speed_mps=0.0, path=None):
        """Put a vehicle somewhere explicitly (tests, scenarios)."""

        with self._lock:
            record = state.get_vehicle_state(vehicle_id)
            pose = self._ensure(vehicle_id, record)
            pose.last_placement = state.placement_seq(vehicle_id)
            if node is not None:
                park(pose, node)
                pose.destination = None
            if segment is not None:
                if segment not in self.world.segments:
                    raise WorldError(f"Unknown segment {segment}.")
                pose.node = None
                pose.segment = segment
                pose.s = max(0.0, min(float(s), self.world.segments[segment].length))
                pose.path = list(path) if path else [segment]
                if pose.path[0] != segment:
                    pose.path.insert(0, segment)
                pose.destination = self.world.segments[pose.path[-1]].end
                pose.heading_deg = self.world.segments[segment].heading_deg
            pose.speed_mps = max(0.0, float(speed_mps))
            return pose_snapshot(self.world, pose)

    def forget(self, *vehicle_ids):
        with self._lock:
            for vehicle_id in vehicle_ids:
                self.poses.pop(vehicle_id, None)

    def rename(self, old_id, new_id):
        with self._lock:
            pose = self.poses.pop(old_id, None)
            if pose is not None:
                pose.vehicle_id = new_id
                self.poses[new_id] = pose

    def pose(self, vehicle_id):
        with self._lock:
            record = state.get_vehicle_state(vehicle_id)
            pose = self._ensure(vehicle_id, record)
            # Apply a placement made since the last tick (a command that
            # moved the vehicle to a stop), so readers never see a stale
            # position even when the engine thread is not running.
            self._follow_location(pose, record)
            return pose_snapshot(self.world, pose)

    def poses_snapshot(self):
        with self._lock:
            result = []
            for vehicle_id in fleet.vehicle_ids():
                try:
                    result.append(self.pose(vehicle_id))
                except ValueError:
                    continue
            return result

    # ---- one tick ------------------------------------------------

    def tick(self, dt):

        with self._lock:

            try:
                self._tick(dt)
            except Exception as exc:
                # Counted for the autopilot watchdog, then re-raised.
                import traceback
                self.error_count += 1
                self.last_exception = "".join(
                    traceback.format_exception_only(type(exc), exc)).strip()
                raise

            self.last_tick_mono = time.monotonic()

    def _tick(self, dt):

        registered = fleet.vehicle_ids()

        for vehicle_id in list(self.poses):
            if vehicle_id not in registered:
                del self.poses[vehicle_id]

        for vehicle_id in registered:
            try:
                record = state.get_vehicle_state(vehicle_id)
            except ValueError:
                continue          # removed meanwhile
            self._tick_vehicle(vehicle_id, record, dt)

        self.ticks += 1
        self.sim_seconds += dt
        self.last_tick_at = utc_iso(milliseconds=True)

        for hook in list(self.tick_hooks):
            hook(dt)

    def reset_clock(self):
        """Forget every pose and restart the simulated clock (scenario start)."""

        with self._lock:
            self.poses.clear()
            self.ticks = 0
            self.sim_seconds = 0.0

    def set_time_scale(self, scale):

        scale = float(scale)
        low, high = TIME_SCALE_LIMITS

        if not low <= scale <= high:
            raise ValueError(f"Time scale must be between {low:g} and {high:g}.")

        self.time_scale = scale
        return scale

    def _tick_vehicle(self, vehicle_id, record, dt):

        pose = self._ensure(vehicle_id, record)

        self._follow_location(pose, record)

        target, decel, plan_to, reverse, action = self._intent(pose, record)

        if plan_to and pose.destination != plan_to and pose.node != plan_to:
            self._plan(pose, plan_to)

        if reverse and pose.segment:
            pose.reversing = True
        elif not reverse:
            pose.reversing = False

        stop_within = None

        if self.advisor is not None and target > 0 and not pose.reversing:
            advice = self.advisor(vehicle_id, pose, record) or {}
            cap = advice.get("speed_cap_mps")
            if cap is not None:
                target = min(target, max(0.0, cap))
            stop_within = advice.get("stop_within_m")
            if advice.get("action"):
                action = advice["action"]

        step(self.world, pose, dt, target, decel, stop_within)

        if not pose.path and pose.destination and pose.node == pose.destination:
            stop = self.world.stop_of_node(pose.node)
            if action.startswith(("DRIVE", "PLATOON", "YIELD")):
                action = f"ARRIVED_AT_{(stop or pose.node).upper()}"

        pose.next_action = action

        self._publish(pose, record)

    def _follow_location(self, pose, record):
        """Snap the pose when a command placed the vehicle at a stop.

        Driven by state.placement_seq(), not by the location text: a
        vehicle stopped on the road (emergency stop, then release) still
        says "stop_1" but must stay where it is, while a reset or a
        handover must bring it back even though "stop_1" did not change.
        """

        seq = state.placement_seq(pose.vehicle_id)

        if seq == pose.last_placement:
            return

        pose.last_placement = seq

        node = self.world.node_of_stop(record.get("location"))

        if node and pose.node != node:
            park(pose, node)
            pose.destination = None

    def _plan(self, pose, goal):

        try:
            if pose.segment:
                current = self.world.segments[pose.segment]
                path = [current.id] + self.world.route(current.end, goal)
            else:
                path = self.world.route(pose.node, goal)
        except WorldError:
            return
        set_path(self.world, pose, path)

    def _intent(self, pose, record):
        """(target m/s, deceleration, node to drive to, reverse?, next_action)."""

        kin = self.world.kinematics

        if record.get("emergency_stop"):
            return 0.0, kin["emergency_decel_mps2"], None, False, "EMERGENCY_STOP"

        if record.get("radar_status") == state.RADAR_OBSTACLE or record.get("obstacle_detected"):
            return 0.0, kin["emergency_decel_mps2"], None, False, "HOLD_FOR_OBSTACLE"

        if record.get("comm_hold"):
            # V2V fail-safe (services/v2v.py): every vehicle in radio
            # range went silent, so this vehicle holds where it is.
            return 0.0, None, None, False, "HOLD_COMM_LOST"

        if record.get("fault"):
            return 0.0, kin["emergency_decel_mps2"], None, False, "HOLD_FAULT"

        battery = state.battery_of(record)
        if battery is not None and battery <= 0:
            return 0.0, None, None, False, "BATTERY_EMPTY"

        if record.get("status") != state.STATUS_RUNNING:
            return 0.0, None, None, False, ("HELD" if record.get("mission_status") == "HELD" else "IDLE")

        try:
            commanded = max(0.0, float(record.get("speed") or 0)) / 3.6
        except (TypeError, ValueError):
            commanded = 0.0

        mission = record.get("mission_status")

        if mission == "MANUAL_CONTROL":
            direction = record.get("motor_direction")
            if direction == "stopped" or commanded == 0:
                return 0.0, None, None, False, "MANUAL_STOP"
            if direction == "backward":
                return commanded, None, None, True, "MANUAL_BACKWARD"
            goal = None
            if not pose.path:
                here = pose.node or (self.world.segments[pose.segment].end if pose.segment else None)
                goal = self.world.next_stop.get(here)
            return commanded, None, goal, False, f"MANUAL_{str(direction).upper()}"

        if mission in MISSION_GOALS and MISSION_GOALS[mission] in self.world.stop_nodes:
            stop = MISSION_GOALS[mission]
            return commanded, None, self.world.stop_nodes[stop], False, f"DRIVE_TO_{stop.upper()}"

        if pose.path:
            stop = self.world.stop_of_node(pose.destination)
            return commanded, None, None, False, f"DRIVE_TO_{(stop or pose.destination).upper()}"

        return 0.0, None, None, False, "IDLE"

    def _publish(self, pose, record):
        """Write the derived values into the vehicle's state (backend owns state)."""

        if settings.state_source != "backend":
            return            # ThingsBoard (simulator) provides these in mirror mode

        snap = pose_snapshot(self.world, pose)

        changes = {
            "destination": snap["destination_stop"] or snap["destination"],
            "distance_to_destination": snap["distance_to_destination_m"],
            "eta_seconds": snap["eta_seconds"],
            "last_seen": utc_iso(milliseconds=True),
        }

        # HELD keeps the hold reason; under the autopilot, the
        # autopilot publishes next_action itself (it knows the plan).
        if record.get("mission_status") != "HELD" and record.get("autopilot_mode") != "AUTO":
            changes["next_action"] = pose.next_action

        changed = {k: v for k, v in changes.items() if record.get(k) != v}

        if changed:
            try:
                state.update_vehicle_state(pose.vehicle_id, **changed)
            except ValueError:
                pass          # vehicle removed meanwhile

    # ---- background thread ------------------------------------------

    def start(self, tick_ms):

        if tick_ms <= 0 or (self._thread and self._thread.is_alive()):
            return False

        self.tick_ms = tick_ms
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="world-engine", daemon=True)
        self._thread.start()
        return True

    def stop(self):

        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)
        self._thread = None

    def _run(self):

        dt = self.tick_ms / 1000
        due = time.monotonic()
        reported = None

        while not self._stop.is_set():

            # Time scale: run `steps` simulated ticks of at most dt
            # each per wall-clock tick (8x = 8 ticks of 200 ms).
            simulated = dt * self.time_scale
            steps = max(1, math.ceil(simulated / dt - 1e-9))

            try:
                for _ in range(steps):
                    self.tick(simulated / steps)
                self.last_error = None
            except Exception as exc:          # keep simulating; report once per new error
                self.last_error = f"{type(exc).__name__}: {exc}"
                if self.last_error != reported:
                    print(f"[world] tick failed: {self.last_error}")
                    reported = self.last_error

            due += dt
            delay = due - time.monotonic()
            if delay < 0:
                due = time.monotonic()
                delay = 0
            self._stop.wait(delay)

    def status(self):

        return {
            "running": bool(self._thread and self._thread.is_alive()),
            "tick_ms": self.tick_ms,
            "ticks": self.ticks,
            "simulated_seconds": round(self.sim_seconds, 1),
            "last_tick_at": self.last_tick_at,
            "last_error": self.last_error,
            "time_scale": self.time_scale,
            "state_source": settings.state_source,
        }


# The world is loaded (and validated) when the backend starts.
world = load_world()

engine = WorldEngine(world)
