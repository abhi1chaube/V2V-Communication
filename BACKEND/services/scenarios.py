# =========================================================
# SCENARIOS AND FAULT INJECTION (UPGRADE PHASE 4)
# =========================================================
#
# A scenario is a JSON file in BACKEND/scenarios/: a fleet setup
# and a timeline of events on the SIMULATED clock, for example
#
#   {"t": 20, "action": "obstacle", "segment": "outbound_1", "s": 70, "duration_s": 15}
#   {"t": 35, "action": "link_loss", "segments": ["outbound_2"], "duration_s": 10}
#   {"t": 50, "action": "breakdown", "vehicle": "#2"}
#
# Vehicles are named by fleet position ("#1" = first vehicle in
# the registry) or by name. Running a scenario:
#   1. resets the fleet cycle (all vehicles at Stop 1, MANUAL), the
#      radio, the world clock and the random streams (seed),
#   2. applies the setup (batteries, dispatch policy, radio),
#   3. puts the listed vehicles (default: all) in AUTO,
#   4. fires the timeline as simulated time passes (a world engine
#      tick hook, so the same seed gives the same run),
#   5. at duration_s (or POST /api/v1/scenarios/stop) removes its
#      effects, disengages the autopilot (vehicles hold where they
#      are) and keeps a report: cycles, handovers, hazard holds,
#      emergency brakes, the smallest gap seen between vehicles.
#
# Actions:
#   obstacle     a road obstacle at (segment, s); vehicles see it on
#                their simulated radar (RADAR_RANGE_M), brake, and
#                warn the others with a DENM. Optional duration_s.
#   clear_obstacle   {"id": ...} removes an obstacle early.
#   radar        {"vehicle", "obstacle": true|false}: a radar reading.
#   link_loss    radio blackout for {"vehicle"} or for every vehicle
#                on {"segments": [...]}, for duration_s.
#   v2v_config   {"config": {"loss_rate": 0.5, ...}} for duration_s.
#   breakdown    {"vehicle"}: the vehicle breaks down (holds, refuses
#                handovers, warns others if on the road). Optional
#                duration_s, then it is repaired.
#   repair       {"vehicle"}.
#   battery      {"vehicle", "percent"}.
#   note         {"text"}: an entry in the activity log.
# =========================================================

import json
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from . import autopilot as autopilot_module
from . import events
from . import fleet
from . import j2735
from . import simrng
from . import state
from . import v2v
from . import world as world_module
from .settings import BACKEND_DIR, settings
from .timeutil import utc_iso


SCENARIO_DIR = BACKEND_DIR / "scenarios"

ACTIONS = {
    "obstacle": ("segment", "s"),
    "clear_obstacle": ("id",),
    "radar": ("vehicle", "obstacle"),
    "link_loss": (),
    "v2v_config": ("config",),
    "breakdown": ("vehicle",),
    "repair": ("vehicle",),
    "battery": ("vehicle", "percent"),
    "note": ("text",),
}

LOG_SIZE = 300


class ScenarioError(ValueError):

    def __init__(self, message, code, status=400):
        super().__init__(message)
        self.code = code
        self.status = status


# =========================================================
# LOADING AND VALIDATION
# =========================================================

def _validate(spec, name, world):

    if not isinstance(spec, dict):
        raise ValueError("the top level must be an object")

    duration = float(spec.get("duration_s", 0))
    if duration <= 0:
        raise ValueError("duration_s must be > 0")

    min_vehicles = int(spec.get("min_vehicles", 1))
    if min_vehicles < 1:
        raise ValueError("min_vehicles must be >= 1")

    setup = spec.get("setup") or {}
    if not isinstance(setup, dict):
        raise ValueError("setup must be an object")

    dispatch = setup.get("dispatch")
    if dispatch is not None and dispatch not in autopilot_module.DISPATCH_POLICIES:
        raise ValueError(f"setup.dispatch must be one of {autopilot_module.DISPATCH_POLICIES}")

    for ref, percent in (setup.get("battery") or {}).items():
        if not 0 <= float(percent) <= 100:
            raise ValueError(f"setup.battery.{ref} must be 0..100")

    timeline = spec.get("timeline") or []
    if not isinstance(timeline, list):
        raise ValueError("timeline must be a list")

    for index, item in enumerate(timeline):
        where = f"timeline[{index}]"
        if not isinstance(item, dict):
            raise ValueError(f"{where} must be an object")
        action = item.get("action")
        if action not in ACTIONS:
            raise ValueError(f"{where}: unknown action {action!r}; allowed: {', '.join(ACTIONS)}")
        if float(item.get("t", -1)) < 0:
            raise ValueError(f"{where}: t must be >= 0")
        missing = [key for key in ACTIONS[action] if key not in item]
        if missing:
            raise ValueError(f"{where} ({action}) needs: {', '.join(missing)}")
        if action == "obstacle" and item["segment"] not in world.segments:
            raise ValueError(f"{where}: unknown segment {item['segment']}")
        if action == "link_loss":
            if "vehicle" not in item and not item.get("segments"):
                raise ValueError(f"{where} (link_loss) needs vehicle or segments")
            for segment in item.get("segments") or []:
                if segment not in world.segments:
                    raise ValueError(f"{where}: unknown segment {segment}")
            if float(item.get("duration_s", 0)) <= 0:
                raise ValueError(f"{where} (link_loss) needs duration_s > 0")

    return {
        "name": name,
        "title": str(spec.get("title") or name),
        "description": str(spec.get("description") or ""),
        "seed": int(spec.get("seed", settings.sim_seed)),
        "duration_s": duration,
        "min_vehicles": min_vehicles,
        "setup": setup,
        "timeline": sorted(timeline, key=lambda item: float(item["t"])),
    }


def load_all(directory=None):
    """(valid scenarios by name, {file: error} for invalid ones)."""

    directory = Path(directory or SCENARIO_DIR)
    valid, invalid = {}, {}

    for path in sorted(directory.glob("*.json")):
        try:
            spec = json.loads(path.read_text(encoding="utf-8"))
            valid[path.stem] = _validate(spec, path.stem, world_module.world)
        except (ValueError, TypeError, KeyError) as exc:
            invalid[path.name] = str(exc)

    return valid, invalid


# =========================================================
# A RUN
# =========================================================

@dataclass
class Run:

    spec: dict
    seed: int
    vehicles: dict                         # ref -> vehicle id
    started_at: str
    pending: list
    status: str = "running"
    finished_at: Optional[str] = None
    finished_t: Optional[float] = None
    reverts: list = field(default_factory=list)       # (t, description, callable)
    blackout_vehicles: dict = field(default_factory=dict)   # vehicle -> until t
    blackout_zones: list = field(default_factory=list)      # (segments set, until t)
    obstacles: set = field(default_factory=set)
    log: deque = field(default_factory=lambda: deque(maxlen=LOG_SIZE))
    previous_dispatch: Optional[str] = None             # restored when the run ends
    min_gap_m: Optional[float] = None
    min_gap_pair: Optional[list] = None
    near_misses: int = 0
    _close: set = field(default_factory=set)

    @property
    def name(self):
        return self.spec["name"]


class ScenarioRunner:

    def __init__(self, engine, bus, autopilot):

        self.engine = engine
        self.bus = bus
        self.autopilot = autopilot
        self.active = None
        self.last = None
        self.directory = SCENARIO_DIR

        autopilot.on_trip.append(self._on_watchdog_trip)

    # ---- listing --------------------------------------------------------

    def scenarios(self):
        return load_all(self.directory)

    def get(self, name):

        valid, invalid = self.scenarios()

        if name in valid:
            return valid[name]

        if f"{name}.json" in invalid:
            raise ScenarioError(f"Scenario {name} is invalid: {invalid[name + '.json']}", "SCENARIO_INVALID", 400)

        raise ScenarioError(f"No scenario named {name}. See GET /api/v1/scenarios.", "SCENARIO_NOT_FOUND", 404)

    def overview(self):

        valid, invalid = self.scenarios()

        return {
            "scenarios": [
                {
                    "name": spec["name"], "title": spec["title"], "description": spec["description"],
                    "min_vehicles": spec["min_vehicles"], "duration_s": spec["duration_s"],
                    "seed": spec["seed"], "dispatch": spec["setup"].get("dispatch", self.autopilot.dispatch),
                    "events": len(spec["timeline"]),
                }
                for spec in valid.values()
            ],
            "invalid": invalid,
            "fleet_size": fleet.fleet_size(),
            "active": self.view(self.active) if self.active else None,
            "last_run": self.last,
        }

    # ---- references ---------------------------------------------------------

    @staticmethod
    def _resolve(ref, ids):

        text = str(ref).strip()

        if text.startswith("#"):
            try:
                index = int(text[1:]) - 1
            except ValueError:
                index = -1
            if 0 <= index < len(ids):
                return ids[index]
            raise ScenarioError(f"Vehicle {text} does not exist (the fleet has {len(ids)}).",
                                "SCENARIO_NEEDS_VEHICLES", 409)

        vehicle = fleet.resolve(text)

        if vehicle is None:
            raise ScenarioError(f"Unknown vehicle {text} in the scenario.", "SCENARIO_INVALID", 400)

        return vehicle

    def _refs(self, spec, ids):

        refs = set()
        refs.update((spec["setup"].get("battery") or {}).keys())
        autopilot_refs = spec["setup"].get("autopilot", "fleet")
        if isinstance(autopilot_refs, list):
            refs.update(autopilot_refs)
        for item in spec["timeline"]:
            if "vehicle" in item:
                refs.add(item["vehicle"])

        return {ref: self._resolve(ref, ids) for ref in refs}

    # ---- start / stop ----------------------------------------------------------

    def start(self, name, seed=None, time_scale=None):

        spec = self.get(name)

        if self.active is not None:
            raise ScenarioError(f"Scenario {self.active.name} is running. Stop it first.", "SCENARIO_RUNNING", 409)

        if self.autopilot.failed:
            raise ScenarioError("The autopilot watchdog has tripped; recover it first.", "AUTOPILOT_FAILED", 409)

        ids = fleet.vehicle_ids()

        if len(ids) < spec["min_vehicles"]:
            raise ScenarioError(
                f"Scenario {name} needs {spec['min_vehicles']} vehicles; the fleet has {len(ids)}.",
                "SCENARIO_NEEDS_VEHICLES", 409)

        stopped = [v for v in ids if state.get_vehicle_state(v).get("emergency_stop")]

        if stopped:
            raise ScenarioError(
                f"Release the emergency stop first ({', '.join(stopped)}).", "EMERGENCY_STOP_ACTIVE", 409)

        refs = self._refs(spec, ids)
        seed = int(spec["seed"] if seed is None else seed)

        if time_scale is not None:
            self.engine.set_time_scale(time_scale)

        setup = spec["setup"]

        with self.engine._lock:          # nothing moves while the world is rebuilt

            previous_dispatch = self.autopilot.dispatch

            simrng.reset(seed)
            state.reset_cycle()
            state.ignore_telemetry_before(ids)
            self.autopilot.reset_all()
            self.bus.reset()
            if setup.get("v2v"):
                self.bus.update_config(setup["v2v"])
            self.engine.reset_clock()
            j2735.forget(*ids)

            for ref, percent in (setup.get("battery") or {}).items():
                state.set_battery(refs[ref], percent)

            run = Run(
                spec=spec, seed=seed, vehicles=refs, started_at=utc_iso(milliseconds=True),
                pending=list(spec["timeline"]), previous_dispatch=previous_dispatch,
            )
            self.active = run
            self.autopilot.scenario_active = True

            targets = setup.get("autopilot", "fleet")
            vehicles = ids if targets == "fleet" else [refs[ref] for ref in targets]
            result = self.autopilot.enable(vehicles, dispatch=setup.get("dispatch", settings.autopilot_dispatch))

            self._note(run, None, "START", f"Scenario {spec['title']} started (seed {seed}).")

        return {"scenario": self.view(run), "autopilot": result}

    def stop(self, reason="stopped"):

        if self.active is None:
            return None

        with self.engine._lock:
            return self._finish(reason)

    def abort(self, reason):
        """Emergency stop or fleet reset: end the run, the autopilot is already off."""

        if self.active is None:
            return None

        with self.engine._lock:
            return self._finish(reason, disengage=False)

    def _on_watchdog_trip(self):

        # Watchdog thread: the engine may be stuck, so take no engine
        # or bus lock; the trip already stopped and disengaged everyone.
        run = self.active
        if run is None:
            return
        self.bus.blackout = set()
        self.autopilot.road_obstacles.clear()
        self.autopilot.scenario_active = False
        if run.previous_dispatch:
            self.autopilot.dispatch = run.previous_dispatch
        run.status = "aborted"
        run.finished_at = utc_iso(milliseconds=True)
        run.finished_t = round(self.engine.sim_seconds, 1)
        self.last = self.report(run)
        self.active = None

    def _finish(self, status, disengage=True):

        run = self.active

        if run is None:
            return None

        # Undo every temporary effect (obstacles, blackouts, radio settings).
        for _, _, undo in sorted(run.reverts, key=lambda item: item[0]):
            try:
                undo()
            except Exception:
                pass
        run.reverts.clear()

        for obstacle_id in list(run.obstacles):
            self.autopilot.remove_road_obstacle(obstacle_id)
        run.obstacles.clear()

        with self.bus.lock:
            self.bus.blackout = set()

        run.status = status
        run.finished_at = utc_iso(milliseconds=True)
        run.finished_t = round(self.engine.sim_seconds, 1)

        self._note(run, None, "END", f"Scenario {run.spec['title']} {status} at t = {run.finished_t:g} s.")

        self.autopilot.scenario_active = False
        self.active = None

        if disengage:
            self.autopilot.disable(self.autopilot.auto_vehicles(), reason=f"SCENARIO_{status.upper()}")

        # The scenario's dispatch policy was for this run only.
        if run.previous_dispatch:
            self.autopilot.dispatch = run.previous_dispatch
            self.autopilot.baton = None

        self.last = self.report(run)
        return self.last

    # ---- tick ---------------------------------------------------------------------

    def tick(self, dt):

        run = self.active

        if run is None:
            return

        now = self.engine.sim_seconds

        while run.pending and float(run.pending[0]["t"]) <= now + 1e-9:
            self._apply(run, run.pending.pop(0))

        due = [item for item in run.reverts if item[0] <= now + 1e-9]
        if due:
            run.reverts = [item for item in run.reverts if item[0] > now + 1e-9]
            for _, description, undo in sorted(due, key=lambda item: item[0]):
                undo()
                self._note(run, None, "REVERT", description)

        self._update_blackout(run, now)
        self._measure(run)

        if now + 1e-9 >= run.spec["duration_s"]:
            self._finish("completed")

    def _update_blackout(self, run, now):

        out = {v for v, until in run.blackout_vehicles.items() if until > now}
        run.blackout_vehicles = {v: until for v, until in run.blackout_vehicles.items() if until > now}
        run.blackout_zones = [(segments, until) for segments, until in run.blackout_zones if until > now]

        if run.blackout_zones:
            for vehicle, pose in self.engine.poses.items():
                if any(pose.segment in segments for segments, _ in run.blackout_zones):
                    out.add(vehicle)

        with self.bus.lock:
            if out != self.bus.blackout:
                self.bus.blackout = out

    def _measure(self, run):
        """Smallest gap between two vehicles following each other (ground truth)."""

        poses = dict(self.engine.poses)
        close = set()

        for a, pose_a in poses.items():
            if not pose_a.path:
                continue
            for b, pose_b in poses.items():
                if a == b:
                    continue
                gap = self.autopilot._true_gap(pose_a, pose_b)
                if gap is None:
                    continue
                if run.min_gap_m is None or gap < run.min_gap_m:
                    run.min_gap_m = round(gap, 2)
                    run.min_gap_pair = [a, b]
                if gap < settings.critical_gap_m:
                    close.add((a, b))

        run.near_misses += len(close - run._close)
        run._close = close

    # ---- actions --------------------------------------------------------------------

    def _note(self, run, vehicle, event, text, **detail):

        t = round(self.engine.sim_seconds, 1)
        run.log.append({"t": t, "vehicle": vehicle, "event": event, "text": text, **detail})
        self.autopilot._trace(vehicle, f"SCENARIO_{event}", text=text)
        events.add_event(f"[SCENARIO] t={t:g} s: {text}", vehicle, event_type="scenario", severity="info")

    def _apply(self, run, item):

        action = item["action"]
        vehicle = run.vehicles.get(item["vehicle"]) if "vehicle" in item else None
        name = events.vehicle_name(vehicle) if vehicle else None
        now = self.engine.sim_seconds
        duration = float(item.get("duration_s") or 0)
        until = now + duration if duration > 0 else None

        def later(description, undo):
            if until is not None:
                run.reverts.append((until, description, undo))

        if action == "obstacle":
            obstacle_id = str(item.get("id") or f"obstacle-{len(run.log)}")
            self.autopilot.add_road_obstacle(obstacle_id, item["segment"], item["s"], item.get("label"))
            run.obstacles.add(obstacle_id)
            self._note(run, None, "OBSTACLE",
                       f"obstacle on {item['segment']} at {float(item['s']):g} m"
                       + (f" for {duration:g} s" if until else ""))

            def undo(obstacle_id=obstacle_id):
                self.autopilot.remove_road_obstacle(obstacle_id)
                run.obstacles.discard(obstacle_id)

            later(f"obstacle {obstacle_id} removed", undo)

        elif action == "clear_obstacle":
            self.autopilot.remove_road_obstacle(str(item["id"]))
            run.obstacles.discard(str(item["id"]))
            self._note(run, None, "CLEAR_OBSTACLE", f"obstacle {item['id']} removed")

        elif action == "radar":
            obstacle = bool(item["obstacle"])
            state.set_radar_status(vehicle, state.RADAR_OBSTACLE if obstacle else state.RADAR_CLEAR)
            (events.log_radar_obstacle if obstacle else events.log_radar_clear)(vehicle)
            self._note(run, vehicle, "RADAR", f"{name} radar: {'obstacle' if obstacle else 'clear'}")

            def undo(vehicle=vehicle):
                state.set_radar_status(vehicle, state.RADAR_CLEAR)
                events.log_radar_clear(vehicle)

            if obstacle:
                later(f"{name} radar clear", undo)

        elif action == "link_loss":
            if vehicle:
                run.blackout_vehicles[vehicle] = until
                self._note(run, vehicle, "LINK_LOSS", f"{name} radio blackout for {duration:g} s")
            else:
                segments = set(item["segments"])
                run.blackout_zones.append((segments, until))
                self._note(run, None, "LINK_LOSS",
                           f"radio dead zone on {', '.join(sorted(segments))} for {duration:g} s")

        elif action == "v2v_config":
            previous = {key: self.bus.config[key] for key in item["config"] if key in self.bus.config}
            self.bus.update_config(item["config"])
            self._note(run, None, "V2V_CONFIG", f"radio settings {item['config']}"
                       + (f" for {duration:g} s" if until else ""))
            later(f"radio settings restored {previous}", lambda previous=previous: self.bus.update_config(previous))

        elif action == "breakdown":
            state.set_fault(vehicle, "BREAKDOWN")
            self._note(run, vehicle, "BREAKDOWN", f"{name} breaks down")
            self.autopilot._sync(vehicle)

            def undo(vehicle=vehicle):
                state.set_fault(vehicle, None)
                self.autopilot._sync(vehicle)

            later(f"{events.vehicle_name(vehicle)} repaired", undo)

        elif action == "repair":
            state.set_fault(vehicle, None)
            self._note(run, vehicle, "REPAIR", f"{name} repaired")
            self.autopilot._sync(vehicle)

        elif action == "battery":
            state.set_battery(vehicle, item["percent"])
            self._note(run, vehicle, "BATTERY", f"{name} battery set to {float(item['percent']):g} %")

        elif action == "note":
            self._note(run, None, "NOTE", str(item["text"]))

    # ---- views ----------------------------------------------------------------------

    def view(self, run):

        if run is None:
            return None

        now = self.engine.sim_seconds if run.status == "running" else run.finished_t

        return {
            "name": run.name,
            "title": run.spec["title"],
            "status": run.status,
            "seed": run.seed,
            "t": round(now or 0.0, 1),
            "duration_s": run.spec["duration_s"],
            "started_at": run.started_at,
            "finished_at": run.finished_at,
            "vehicles": dict(run.vehicles),
            "next_events": [
                {"t": item["t"], "action": item["action"]} for item in run.pending[:5]
            ],
            "log": list(run.log)[-50:],
        }

    def report(self, run):

        trace = list(self.autopilot.trace)
        ids = fleet.vehicle_ids()

        def count(event, vehicle=None):
            return sum(1 for e in trace if e["event"] == event and (vehicle is None or e["vehicle"] == vehicle))

        per_vehicle = {}
        for vehicle in ids:
            mem = self.autopilot.memory.get(vehicle)
            per_vehicle[vehicle] = {
                "cycles_completed": count("COMPLETED", vehicle),
                "charges": sum(
                    1 for e in trace if e["event"] == "ARRIVED" and e["vehicle"] == vehicle
                    and e.get("stop") == "charging"),
                "hazard_holds": count("HAZARD_HOLD", vehicle),
                "comm_holds": count("COMM_HOLD", vehicle),
                "emergency_brakes": count("GAP_CRITICAL", vehicle),
                "distance_driven_m": round(mem.distance_m, 1) if mem else None,
            }

        return {
            **self.view(run),
            "summary": {
                "cycles_completed": count("COMPLETED"),
                "handovers": [
                    {"t": e["t"], "from": e["vehicle"], "to": e.get("to")}
                    for e in trace if e["event"] == "HANDOVER_COMPLETED"
                ],
                "handovers_refused": [
                    {"t": e["t"], "from": e["vehicle"], "to": e.get("to"), "reason": e.get("reason")}
                    for e in trace if e["event"] in ("HANDOVER_REJECTED", "HANDOVER_TIMEOUT")
                ],
                "hazard_holds": count("HAZARD_HOLD"),
                "comm_holds": count("COMM_HOLD"),
                "emergency_brakes": count("GAP_CRITICAL"),
                "min_gap_m": run.min_gap_m,
                "min_gap_pair": run.min_gap_pair,
                "near_misses": run.near_misses,
                "vehicles": per_vehicle,
            },
            "trace_length": len(trace),
        }


runner = ScenarioRunner(world_module.engine, v2v.bus, autopilot_module.controller)

world_module.engine.add_tick_hook(runner.tick)
