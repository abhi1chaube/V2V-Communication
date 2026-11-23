# =========================================================
# AUTOPILOT: THE BACKEND DRIVES THE MISSION (UPGRADE PHASE 4)
# =========================================================
#
# Until Phase 3 the BROWSER drove every cycle with timers: close
# the tab and the fleet stopped. The autopilot moves that logic
# into the backend.
#
# Per vehicle mode: MANUAL (the default after every backend
# start: the dashboard / an operator sends the commands, as
# before) or AUTO (the backend drives the whole loop):
#
#   Stop 1 --start--> Stop 2 (wait for the SLOT_GRANT, load 0-100 %)
#     --> Stop 3 (unload) --> [handover of the next cycle]
#     --> return leg --> Stop 1, or the charging bay if the battery
#     is low --> next cycle
#
# It uses the SAME safety validators (services/safety.py) and the
# same atomic state transitions (services/state.py) as the REST
# commands. Nothing here bypasses them.
#
# Where it runs: inside the world engine's tick (a tick hook, like
# the V2V bus), so motion, radio and decisions share one simulated
# clock and a seeded run repeats exactly. The engine thread is the
# background tick loop (WORLD_TICK_MS, default 200 ms). A separate
# WATCHDOG thread checks that this loop keeps running.
#
# Cooperative driving (the engine asks advise() every tick for an
# AUTO vehicle; the most restrictive rule wins):
#   - DENM hazard ahead on the route, within its relevance radius:
#     hold HAZARD_STANDOFF_M short of it; resume when it is cleared.
#     A silent neighbour (heartbeat lost) counts as a hazard at its
#     last known position.
#   - Gap to the vehicle ahead (V2V CAMs and the simulated radar):
#     follow so that a stopped leader is passed by no closer than
#     MIN_GAP_M; below MIN_GAP_M slow down; below CRITICAL_GAP_M
#     emergency brake and raise an EMERGENCY_BRAKE DENM.
#   - Never drive into Stop 2 without a SLOT_GRANT (wait SLOT_HOLD_M
#     before it); the shared lane needs right-of-way and platoon
#     followers keep the target gap (both from services/v2v.py).
#   - Merges (two lanes into one): the vehicle farther from the
#     merge point waits.
#   - comm_hold (heartbeat fail-safe), obstacles, faults and the
#     emergency stop already stop the vehicle in the engine.
#
# The emergency latch wins over everything: an emergency stop
# disengages the autopilot (all vehicles go to MANUAL). It never
# resumes by itself: release the stop, then enable it again.
#
# Battery model (every vehicle, also MANUAL): drain per metre
# driven and per second idling, recharge at the charging bay.
# Below BATTERY_LOW_PCT the vehicle raises a low-battery DENM (v2v),
# finishes its current cycle and goes to charge instead of
# starting a new one.
#
# Signal strength (every vehicle): from the distance to the
# nearest neighbour, relative to the radio range.
#
# Dispatch policy (AUTOPILOT_DISPATCH):
#   handover   one vehicle runs a cycle at a time (the original
#              process); at Stop 3 it negotiates the handover with
#              the next AUTO vehicle (Phase 3 messages). A refusal
#              (busy, fault, low battery, not at Stop 1, ...) moves
#              on to the following vehicle.
#   concurrent every AUTO vehicle cycles; departures from Stop 1
#              are spaced by AUTOPILOT_DEPART_HEADWAY_S.
# =========================================================

import math
import threading
import time
from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Optional

from . import events
from . import fleet
from . import hardware
from . import safety
from . import state
from . import v2v
from . import world as world_module
from .settings import settings
from .timeutil import utc_iso


MODE_MANUAL = "MANUAL"
MODE_AUTO = "AUTO"

DISPATCH_POLICIES = ("handover", "concurrent")

# Stops where several vehicles may park side by side (depot bays):
# a vehicle parked there is not "ahead" of one arriving.
MULTI_BAY_STOPS = world_module.MULTI_BAY_STOPS

MERGE_ZONE_M = 25.0
MERGE_WAIT_M = 3.0
BINDING_STOP_M = 30.0
LOW_BATTERY_HYSTERESIS = 5.0
PROGRESS_SYNC_STEP = 20           # push to ThingsBoard every 20 % of (un)loading / charging
TRACE_SIZE = 3000

# Rule priorities (which rule names next_action when several apply).
PRIORITY = {
    "EMERGENCY_BRAKE_GAP": 100,
    "HOLD_FOR_HAZARD": 80,
    "WAIT_FOR_SLOT": 60,
    "YIELD_SHARED_LANE": 55,
    "YIELD_MERGE": 50,
    "SLOW_GAP": 40,
    "PLATOON_FOLLOW": 30,
    "FOLLOW_GAP": 20,
}


class AutopilotError(ValueError):
    """An autopilot request was refused (the API answers with status/code)."""

    def __init__(self, message, code, status=409):
        super().__init__(message)
        self.code = code
        self.status = status


@dataclass
class Rule:
    stop_within_m: Optional[float]
    speed_cap_mps: Optional[float]
    action: str

    @property
    def priority(self):
        return PRIORITY.get(self.action, 10)


@dataclass
class Memory:
    """What the autopilot remembers about one vehicle (not vehicle state)."""

    phase: str = "MANUAL"
    cycle_active: bool = False        # between leaving Stop 1 and completing at Stop 3
    cycles: int = 0
    handover_done: bool = False
    handover_tried: list = field(default_factory=list)
    negotiation: object = None
    last_block: Optional[str] = None
    hazard: Optional[str] = None      # denm_id it is holding for
    gap_critical: bool = False
    low_battery: bool = False
    radar_by_sim: bool = False        # the radar reading was set by a road obstacle
    comm_hold: bool = False
    battery_exact: Optional[float] = None      # unrounded; state shows it rounded to 0.01
    battery_published: float = 100.0
    sync_bucket: Optional[int] = None
    distance_m: float = 0.0
    charges: int = 0

    def fresh(self, phase="MANUAL"):
        """A clean plan that keeps the counters (cycles, distance, charges)."""

        return Memory(phase=phase, cycles=self.cycles, distance_m=self.distance_m,
                      charges=self.charges, low_battery=self.low_battery,
                      radar_by_sim=self.radar_by_sim, comm_hold=self.comm_hold)


def _vehicle_order():
    return {v: i for i, v in enumerate(fleet.vehicle_ids())}


# =========================================================
# THE AUTOPILOT
# =========================================================

class Autopilot:

    def __init__(self, engine, bus):

        self.engine = engine
        self.bus = bus
        self.world = engine.world

        # Leaf lock: guards modes/memory/baton. Nothing else is
        # acquired while it is held (lock order engine -> bus -> state).
        self._lock = threading.RLock()

        self.modes = {}
        self.memory = {}
        self.dispatch = settings.autopilot_dispatch
        self.baton = None             # handover dispatch: who runs the current cycle
        self.last_departure_s = -math.inf
        self.failed = None            # watchdog trip report
        self.road_obstacles = {}      # id -> {"segment", "s", "label"} (scenarios)
        self.trace = deque(maxlen=TRACE_SIZE)
        self.scenario_active = False

        # Phase 5: callable(entry_dict) after every trace entry. services/live.py
        # pushes it to the SSE "autopilot" channel (or "scenario" for a
        # SCENARIO_* entry, which scenarios.py also raises through here).
        self.trace_hooks = []

        # Wired by the API layer (api/routes_autopilot.py):
        #   on_transition(vehicle, radio=True): BSM + ThingsBoard push
        #   try_lock(vehicle): context manager -> bool (command lock, non-blocking)
        #   on_trip(): called after a watchdog trip (scenario runner stops)
        self.on_transition = None
        self.try_lock = None
        self.on_trip = []

        self.watchdog = Watchdog(self)

        merges = {}
        for segment in self.world.segments.values():
            merges.setdefault(segment.end, []).append(segment.id)
        self._merge_nodes = {node for node, incoming in merges.items() if len(incoming) > 1}

    # ---- small helpers ------------------------------------------------

    def _mem(self, vehicle):

        with self._lock:
            return self.memory.setdefault(vehicle, Memory())

    def is_auto(self, vehicle):
        return self.modes.get(vehicle) == MODE_AUTO

    def auto_vehicles(self):
        with self._lock:
            return [v for v in fleet.vehicle_ids() if self.modes.get(v) == MODE_AUTO]

    def armed(self):
        return bool(self.auto_vehicles()) or self.scenario_active

    @property
    def cruise_kmh(self):
        return float(settings.autopilot_cruise_kmh)

    def _trace(self, vehicle, event, **detail):

        entry = {"t": round(self.engine.sim_seconds, 1), "vehicle": vehicle, "event": event}
        entry.update({k: v for k, v in detail.items() if v is not None})
        self.trace.append(entry)

        for hook in list(self.trace_hooks):
            try:
                hook(entry)
            except Exception as exc:
                print(f"[autopilot] trace hook failed: {exc}")

    def _log(self, vehicle, message, severity="info", details=None):

        events.add_event(f"[AUTOPILOT] {message}", vehicle, event_type="autopilot",
                         severity=severity, details=details)

    def _sync(self, vehicle, radio=True):

        if self.on_transition is not None:
            try:
                self.on_transition(vehicle, radio)
            except Exception as exc:          # telemetry is best effort
                print(f"[autopilot] sync for {vehicle} skipped: {exc}")

    @contextmanager
    def _vehicle_lock(self, vehicle):

        if self.try_lock is None:
            yield True
            return

        with self.try_lock(vehicle) as acquired:
            yield acquired

    # ---- modes ---------------------------------------------------------

    def enable(self, vehicles, dispatch=None):
        """Put vehicles in AUTO. Returns {"enabled": [...], "refused": {vehicle: code}}."""

        if self.failed:
            raise AutopilotError(
                "The autopilot watchdog has tripped. Fix the cause, then POST "
                "/api/v1/autopilot/recover before enabling it again.",
                "AUTOPILOT_FAILED")

        if dispatch is not None:
            dispatch = str(dispatch).strip().lower()
            if dispatch not in DISPATCH_POLICIES:
                raise AutopilotError(
                    f"dispatch must be one of: {', '.join(DISPATCH_POLICIES)}.", "INVALID_DISPATCH", 400)
            with self._lock:
                if dispatch != self.dispatch:
                    self.dispatch = dispatch
                    self.baton = None

        enabled, refused = [], {}

        for vehicle in vehicles:

            record = state.get_vehicle_state(vehicle)

            if record.get("emergency_stop"):
                refused[vehicle] = "EMERGENCY_STOP_ACTIVE"
                continue

            if record.get("fault"):
                refused[vehicle] = "VEHICLE_FAULT"
                continue

            with self._lock:
                already = self.modes.get(vehicle) == MODE_AUTO
                self.modes[vehicle] = MODE_AUTO
                mem = self.memory.setdefault(vehicle, Memory())
                mem.phase = "ENGAGED"
                mem.last_block = None
                mem.cycle_active = self._looks_mid_cycle(record)
                if mem.cycle_active and self.dispatch == "handover" and self.baton is None:
                    self.baton = vehicle

            state.update_vehicle_state(vehicle, autopilot_mode=MODE_AUTO)
            enabled.append(vehicle)

            if not already:
                self._log(vehicle, f"{events.vehicle_name(vehicle)} autopilot engaged ({self.dispatch} dispatch).")
                self._trace(vehicle, "ENGAGED")

        return {"enabled": enabled, "refused": refused}

    @staticmethod
    def _looks_mid_cycle(record):

        return (
            record.get("mission_status") in (
                "MOVING_TO_LOADING", "AT_LOADING_POINT", "LOADING",
                "MOVING_TO_WAREHOUSE", "UNLOADING", "UNLOADED")
            or record.get("location") == state.STOP_2
            or (record.get("location") == state.STOP_3 and (record.get("load_percentage") or 0) > 0)
        )

    def disable(self, vehicles, reason="OPERATOR"):
        """Back to MANUAL. A vehicle in a mission is held where it is (speed 0, brakes)."""

        disabled = []

        for vehicle in vehicles:

            with self._lock:
                was = self.modes.get(vehicle) == MODE_AUTO
                self.modes[vehicle] = MODE_MANUAL
                if self.baton == vehicle:
                    self.baton = None
                self.memory[vehicle] = self._mem(vehicle).fresh()

            if not was:
                continue

            disabled.append(vehicle)

            try:
                record = state.update_vehicle_state(vehicle, autopilot_mode=MODE_MANUAL)
            except ValueError:
                continue

            if not record.get("emergency_stop") and (
                state.in_mission(record) or record.get("status") == state.STATUS_RUNNING
            ):
                state.hold_vehicle(vehicle, "AUTOPILOT_DISENGAGED")

            self._log(vehicle, f"{events.vehicle_name(vehicle)} autopilot disengaged ({reason}); "
                               "the vehicle holds where it is.")
            self._trace(vehicle, "DISENGAGED", reason=reason)
            self._sync(vehicle)

        return disabled

    def on_emergency_stop(self):
        """An emergency stop disengages the autopilot for the whole fleet (latched)."""

        for vehicle in self.auto_vehicles():
            self._disengage(vehicle, "EMERGENCY_STOP")

    def _disengage(self, vehicle, reason):

        with self._lock:
            if self.modes.get(vehicle) != MODE_AUTO:
                return
            self.modes[vehicle] = MODE_MANUAL
            if self.baton == vehicle:
                self.baton = None
            self.memory[vehicle] = self._mem(vehicle).fresh("DISENGAGED")

        try:
            state.update_vehicle_state(vehicle, autopilot_mode=MODE_MANUAL)
        except ValueError:
            return

        self._log(vehicle, f"{events.vehicle_name(vehicle)} autopilot disengaged: {reason}. "
                           "Release the stop, then enable the autopilot again.", severity="warning")
        self._trace(vehicle, "DISENGAGED", reason=reason)

    def reset_all(self):
        """Fleet reset: everything back to MANUAL, no obstacles, no blackouts."""

        with self._lock:
            self.modes.clear()
            self.memory.clear()
            self.baton = None
            self.last_departure_s = -math.inf
            self.road_obstacles.clear()
            self.trace.clear()

        with self.bus.lock:
            self.bus.blackout = set()

    def forget(self, vehicle):

        with self._lock:
            self.modes.pop(vehicle, None)
            self.memory.pop(vehicle, None)
            if self.baton == vehicle:
                self.baton = None

    def rename(self, old, new):

        with self._lock:
            if old in self.modes:
                self.modes[new] = self.modes.pop(old)
            if old in self.memory:
                self.memory[new] = self.memory.pop(old)
            if self.baton == old:
                self.baton = new

    # ---- road obstacles (scenarios) ----------------------------------------

    def add_road_obstacle(self, obstacle_id, segment, s, label=None):

        if segment not in self.world.segments:
            raise ValueError(f"Unknown segment {segment}.")

        length = self.world.segments[segment].length

        with self._lock:
            self.road_obstacles[obstacle_id] = {
                "id": obstacle_id, "segment": segment,
                "s": max(0.0, min(float(s), length)), "label": label or "Obstacle on the road",
            }

    def remove_road_obstacle(self, obstacle_id):

        with self._lock:
            return self.road_obstacles.pop(obstacle_id, None) is not None

    # ---- handover hooks (called by the V2V bus) ---------------------------------

    def _handover_refusal(self, vehicle, record, position):
        """Extra reasons an AUTO vehicle refuses to take over a cycle."""

        if self.modes.get(vehicle) != MODE_AUTO:
            return None

        stop_1 = self.world.stop_nodes["stop_1"]

        if position is not None and position.get("node") != stop_1:
            return "NOT_AT_STOP_1"

        if record.get("location") != state.STOP_1:
            return "NOT_AT_STOP_1"

        return None

    def _on_negotiation(self, negotiation):

        if negotiation.status != "completed":
            return

        with self._lock:
            target = negotiation.to_vehicle
            if self.dispatch != "handover" or self.modes.get(target) != MODE_AUTO:
                return
            if self.baton in (None, negotiation.from_vehicle):
                self.baton = target
                self._trace(target, "BATON", from_vehicle=negotiation.from_vehicle)

    # =====================================================================
    # TICK (world engine hook, engine lock held)
    # =====================================================================

    def tick(self, dt):

        registered = fleet.vehicle_ids()

        with self._lock:
            for vehicle in [v for v in self.memory if v not in registered]:
                self.memory.pop(vehicle, None)
                self.modes.pop(vehicle, None)
            if self.baton is not None and self.baton not in registered:
                self.baton = None

        snaps = {}
        records = {}

        for vehicle in registered:
            try:
                records[vehicle] = state.get_vehicle_state(vehicle)
                snaps[vehicle] = self.engine.pose(vehicle)
            except ValueError:
                continue

        for vehicle, record in records.items():
            self._physics(vehicle, record, snaps[vehicle], snaps, dt)

        auto = [v for v in records if self.modes.get(v) == MODE_AUTO]

        if auto:
            self._dispatch_step(auto, snaps)

        for vehicle in auto:

            with self._vehicle_lock(vehicle) as acquired:

                if not acquired:
                    continue          # an operator command is running for it

                if self.modes.get(vehicle) != MODE_AUTO:
                    continue

                try:
                    record = state.get_vehicle_state(vehicle)
                except ValueError:
                    continue

                self._control(vehicle, record, snaps[vehicle], dt)
                self._publish(vehicle)

        for vehicle, record in records.items():
            # A vehicle that is not driving is not being advised.
            if record.get("status") != state.STATUS_RUNNING:
                agent = self.bus.agents.get(vehicle)
                if agent is not None:
                    agent.advised_hold = None

    # ---- physics: battery, signal, radar -------------------------------------------

    def _physics(self, vehicle, record, snap, snaps, dt):

        mem = self._mem(vehicle)

        # Battery. The exact value is kept here (a tick drains only a few
        # thousandths of a percent); the state shows it rounded to 0.01.
        # If something else set the state (a scenario, a test), adopt that.
        published = state.battery_of(record)
        if published is None:
            published = 100.0

        if mem.battery_exact is None or abs(published - mem.battery_published) > 1e-9:
            mem.battery_exact = published

        battery = mem.battery_exact

        moved = max(0.0, snap["speed_mps"]) * dt
        mem.distance_m += moved

        charging = record.get("mission_status") == "CHARGING" and snap["stop"] == state.STOP_CHARGING

        if charging:
            new = min(100.0, battery + settings.battery_charge_pct_per_s * dt)
        else:
            new = battery \
                - moved / 1000.0 * settings.battery_drain_pct_per_km \
                - dt / 60.0 * settings.battery_idle_drain_pct_per_min

        new = max(0.0, new)
        mem.battery_exact = new

        shown = round(new, 2)

        if record.get("battery_level") is None or shown != published:
            state.set_battery(vehicle, shown)

        mem.battery_published = shown

        low = settings.battery_low_pct

        if new < low and not mem.low_battery:
            mem.low_battery = True
            self._log(vehicle, f"{events.vehicle_name(vehicle)} battery low ({new:.0f} %): it finishes "
                               "its current leg and charges instead of starting a new cycle.",
                      severity="warning")
            self._trace(vehicle, "LOW_BATTERY", battery=round(new, 1))
        elif mem.low_battery and new >= low + LOW_BATTERY_HYSTERESIS:
            mem.low_battery = False

        # Signal strength from the nearest neighbour.
        me = snap
        nearest = None
        for other, them in snaps.items():
            if other == vehicle:
                continue
            distance = math.hypot(me["x"] - them["x"], me["y"] - them["y"])
            nearest = distance if nearest is None else min(nearest, distance)

        radio_range = self.bus.config["comm_range_m"]

        if vehicle in self.bus.blackout or nearest is None or nearest > radio_range:
            strength = 0
        else:
            strength = int(round(100 * (1 - nearest / radio_range)))
            strength = max(5, strength)

        if record.get("signal_strength") != strength:
            state.update_vehicle_state(vehicle, signal_strength=strength)

        # Heartbeat fail-safe (set by the V2V agent): record it in the trace.
        comm_hold = bool(record.get("comm_hold"))
        if comm_hold != mem.comm_hold:
            mem.comm_hold = comm_hold
            self._trace(vehicle, "COMM_HOLD" if comm_hold else "COMM_RESTORED")

        # Simulated radar: a road obstacle ahead on the route.
        seen = self._obstacle_ahead(snap)

        if seen is not None and record.get("radar_status") != state.RADAR_OBSTACLE:
            state.set_radar_status(vehicle, state.RADAR_OBSTACLE)
            mem.radar_by_sim = True
            events.log_radar_obstacle(vehicle)
            events.log_brake_applied(vehicle, f"Radar: {seen['label']} {seen['distance_m']:.0f} m ahead")
            self._trace(vehicle, "RADAR_OBSTACLE", obstacle=seen["id"])
            self._sync(vehicle)

        elif seen is None and mem.radar_by_sim:
            mem.radar_by_sim = False
            if record.get("radar_status") == state.RADAR_OBSTACLE:
                state.set_radar_status(vehicle, state.RADAR_CLEAR)
                events.log_radar_clear(vehicle)
                self._trace(vehicle, "RADAR_CLEAR")
                self._sync(vehicle)

    def _obstacle_ahead(self, snap):

        if not self.road_obstacles or not snap["path"]:
            return None

        pose = _pose_of(snap)

        best = None

        for obstacle in list(self.road_obstacles.values()):
            gap = world_module.path_gap(self.world, pose, obstacle["segment"], obstacle["s"])
            if gap is not None and gap <= settings.radar_range_m and (best is None or gap < best["distance_m"]):
                best = {**obstacle, "distance_m": gap}

        return best

    # ---- dispatch (fleet level) -------------------------------------------------------

    def _dispatch_step(self, auto, snaps):

        if self.dispatch != "handover":
            return

        with self._lock:

            if self.baton is not None and self.modes.get(self.baton) != MODE_AUTO:
                self.baton = None

            if self.baton is not None:
                return

            if any(self._mem(v).cycle_active for v in auto):
                return

            for vehicle in auto:              # fleet order: deterministic
                try:
                    record = state.get_vehicle_state(vehicle)
                except ValueError:
                    continue
                if self._ready_at_stop_1(record, snaps.get(vehicle)) and not self._battery_low(record):
                    self.baton = vehicle
                    self._trace(vehicle, "BATON", reason="first ready vehicle")
                    break

    def _ready_at_stop_1(self, record, snap):

        return (
            snap is not None
            and snap["stop"] == state.STOP_1
            and record.get("location") == state.STOP_1
            and record.get("status") != state.STATUS_RUNNING
            and not record.get("emergency_stop")
            and not record.get("fault")
            and not record.get("comm_hold")
            and not state.in_mission(record)
        )

    @staticmethod
    def _battery_low(record):

        battery = state.battery_of(record)
        return battery is not None and battery < settings.battery_low_pct

    def _return_needs_charge(self, record):
        """At Stop 3: charge on the way back if the battery would reach Stop 1 below the limit.

        The charging bay is on a spur off the return leg, so this is
        the cheap moment to charge (from Stop 1 it is a whole loop).
        """

        battery = state.battery_of(record)

        if battery is None:
            return False

        route = self.world.route(self.world.stop_nodes[state.STOP_3], self.world.stop_nodes[state.STOP_1])
        length = sum(self.world.segments[s].length for s in route)
        seconds = length / max(self.cruise_kmh / 3.6, 0.1)
        needed = length / 1000.0 * settings.battery_drain_pct_per_km \
            + seconds / 60.0 * settings.battery_idle_drain_pct_per_min

        return battery - needed < settings.battery_low_pct

    # ---- control (one AUTO vehicle) ------------------------------------------------------

    def _control(self, vehicle, record, snap, dt):

        mem = self._mem(vehicle)

        if record.get("emergency_stop"):
            self._disengage(vehicle, "EMERGENCY_STOP")
            return

        if record.get("fault"):
            mem.phase = "FAULT"
            return

        status = record.get("status")
        mission = record.get("mission_status")

        if status == state.STATUS_RUNNING:

            goal = world_module.MISSION_GOALS.get(mission)

            if goal is None:
                # Running in a mode the autopilot does not drive (e.g.
                # MANUAL_CONTROL): hold first, then continue the mission.
                self._hold(vehicle, "AUTOPILOT_TAKEOVER")
                return

            if snap["stop"] == goal and not snap["path"]:
                self._arrive(vehicle, goal, record)
                return

            mem.phase = f"DRIVE_TO_{goal.upper()}"

            try:
                commanded = float(record.get("speed") or 0)
            except (TypeError, ValueError):
                commanded = 0.0

            if commanded <= 0 and not self._path_blocked(record):
                # The radar reading cut the commanded speed; it is clear again.
                try:
                    state.resume_cruise(vehicle, self.cruise_kmh)
                    self._trace(vehicle, "RESUME")
                    self._sync(vehicle)
                except (state.TransitionBlocked, ValueError):
                    pass
            return

        if self._path_blocked(record):
            mem.phase = "HOLD"
            return

        stop = snap["stop"]

        if snap["segment"] is not None or stop is None:
            self._resume_on_road(vehicle, record, snap)
            return

        if record.get("location") != stop:
            self._needs_operator(vehicle, f"its position ({stop}) does not match its last stop "
                                          f"({record.get('location')})")
            return

        if stop == state.STOP_2:
            self._at_loading_point(vehicle, record, dt)
        elif stop == state.STOP_3:
            self._at_warehouse(vehicle, record, dt)
        elif stop == state.STOP_CHARGING:
            self._at_charging(vehicle, record)
        elif stop == state.STOP_1:
            self._at_origin(vehicle, record, snap)

    @staticmethod
    def _path_blocked(record):

        return (
            record.get("radar_status") == state.RADAR_OBSTACLE
            or bool(record.get("obstacle_detected"))
            or bool(record.get("comm_hold"))
        )

    def _ok(self, vehicle, event, message=None, sync=True, **detail):
        """A transition succeeded: clear the last block, trace, log, sync."""

        mem = self._mem(vehicle)
        mem.last_block = None
        self._trace(vehicle, event, **detail)
        if message:
            self._log(vehicle, message)
        if sync:
            self._sync(vehicle)

    def _blocked(self, vehicle, code, reason):

        mem = self._mem(vehicle)
        mem.phase = f"BLOCKED_{code}"

        if mem.last_block != code:
            mem.last_block = code
            events.log_safety_block(vehicle, f"autopilot: {reason}", code)
            self._trace(vehicle, "BLOCKED", code=code)

    def _needs_operator(self, vehicle, why):

        mem = self._mem(vehicle)
        mem.phase = "NEEDS_OPERATOR"

        if mem.last_block != "NEEDS_OPERATOR":
            mem.last_block = "NEEDS_OPERATOR"
            self._log(vehicle, f"{events.vehicle_name(vehicle)} cannot continue: {why}. "
                               "Reset the cycle or drive it manually.", severity="warning")
            self._trace(vehicle, "NEEDS_OPERATOR")

    def _hold(self, vehicle, reason):

        state.hold_vehicle(vehicle, reason)
        self._mem(vehicle).phase = reason
        self._trace(vehicle, "HOLD", reason=reason)
        self._sync(vehicle)

    # ---- arrivals ---------------------------------------------------------------------------

    def _arrive(self, vehicle, stop, record):

        mem = self._mem(vehicle)
        name = events.vehicle_name(vehicle)
        old = record.get("location")

        try:

            if stop == state.STOP_2:
                holder = self.bus.slot_holder_conflict(vehicle)
                if holder is not None:
                    mem.phase = "WAIT_FOR_SLOT"
                    return
                state.reach_loading_point(vehicle)
                self.bus.claim_slot(vehicle)
                events.log_location_change(vehicle, old, stop)
                events.log_brake_applied(vehicle, "Reached loading point")

            elif stop == state.STOP_3:
                state.arrive_at_warehouse(vehicle)
                events.log_location_change(vehicle, old, stop)
                events.log_warehouse_reached(vehicle)

            elif stop == state.STOP_1:
                state.arrive_at_origin(vehicle)
                events.log_location_change(vehicle, old, stop)

            elif stop == state.STOP_CHARGING:
                state.arrive_at_charging(vehicle)
                mem.charges += 1
                events.log_location_change(vehicle, old, stop)

        except state.TransitionBlocked as exc:
            self._blocked(vehicle, exc.code, str(exc))
            return
        except ValueError as exc:
            self._needs_operator(vehicle, str(exc))
            return

        mem.phase = f"AT_{stop.upper()}"
        self._ok(vehicle, "ARRIVED", stop=stop,
                 message=f"{name} arrived at {stop.replace('_', ' ').title()}." if stop in (
                     state.STOP_1, state.STOP_CHARGING) else None)

    # ---- at the stops -------------------------------------------------------------------------

    def _at_loading_point(self, vehicle, record, dt):

        mem = self._mem(vehicle)
        load = float(record.get("load_percentage") or 0)

        try:

            if load < 100:

                if record.get("status") != state.STATUS_LOADING:
                    validation = safety.validate_loading_command(vehicle)
                    if not validation["allowed"]:
                        self._blocked(vehicle, validation["code"], validation["reason"])
                        return
                    state.start_loading(vehicle)
                    events.log_loading_started(vehicle)
                    mem.sync_bucket = 0
                    mem.phase = "LOADING"
                    self._ok(vehicle, "LOADING_STARTED")
                    return

                new = min(100.0, load + settings.loading_rate_pct_per_s * dt)
                state.update_loading(vehicle, new)
                mem.phase = "LOADING"

                if new >= 100:
                    events.log_loading_complete(vehicle)
                    self._ok(vehicle, "LOADED")
                else:
                    self._progress_sync(vehicle, new)
                return

            validation = safety.validate_warehouse_command(vehicle)

            if not validation["allowed"]:
                self._blocked(vehicle, validation["code"], validation["reason"])
                return

            state.depart_loading_point(vehicle, self.cruise_kmh)
            self._release_slot(vehicle)
            mem.phase = "DRIVE_TO_STOP_3"
            self._ok(vehicle, "DEPARTED", stop=state.STOP_2)

        except state.TransitionBlocked as exc:
            self._blocked(vehicle, exc.code, str(exc))
        except ValueError as exc:
            self._needs_operator(vehicle, str(exc))

    def _release_slot(self, vehicle):

        with self.bus.lock:
            agent = self.bus.agents.get(vehicle)
            dock = self.bus.dock()
            if agent is None or dock is None:
                return
            if agent.slot["granted"] and self.bus.config["rsu_enabled"]:
                agent.send(dock.id, "SLOT_RELEASE", {"stop": dock.manages_stop})
            agent.slot.update(granted=False, queue_position=None)

    def _at_warehouse(self, vehicle, record, dt):

        mem = self._mem(vehicle)
        load = float(record.get("load_percentage") or 0)
        mission = record.get("mission_status")

        try:

            if load > 0:
                new = max(0.0, load - settings.unloading_rate_pct_per_s * dt)
                state.unload_to(vehicle, new)
                mem.phase = "UNLOADING"
                if new <= 0:
                    self._ok(vehicle, "UNLOADED")
                else:
                    self._progress_sync(vehicle, 100 - new)
                return

            if mission != "COMPLETED":
                state.complete_journey(vehicle)
                events.log_journey_completed(vehicle)
                mem.cycle_active = False
                mem.cycles += 1
                mem.handover_done = False
                mem.handover_tried = []
                self._ok(vehicle, "COMPLETED", cycle=mem.cycles)
                return

            if mem.negotiation is not None or (
                self.dispatch == "handover" and self.baton == vehicle and not mem.handover_done
            ):
                if self._handover_step(vehicle):
                    return

            low = self._return_needs_charge(record)
            state.start_return(vehicle, to_charging=low, speed_kmh=self.cruise_kmh)
            mem.phase = "DRIVE_TO_CHARGING" if low else "DRIVE_TO_STOP_1"
            self._ok(
                vehicle, "RETURN_LEG", to=("charging" if low else state.STOP_1),
                message=(
                    f"{events.vehicle_name(vehicle)} battery {state.battery_of(record):.0f} %: "
                    "takes the return leg to the charging bay."
                    if low else None
                ),
            )

        except state.TransitionBlocked as exc:
            self._blocked(vehicle, exc.code, str(exc))
        except ValueError as exc:
            self._needs_operator(vehicle, str(exc))

    def _handover_candidates(self, vehicle, tried):

        order = fleet.vehicle_ids()

        if vehicle not in order:
            return []

        start = order.index(vehicle)
        rotated = order[start + 1:] + order[:start]

        return [v for v in rotated if self.modes.get(v) == MODE_AUTO and v not in tried]

    def _handover_step(self, vehicle):
        """Negotiate the next cycle. True while still negotiating."""

        mem = self._mem(vehicle)
        name = events.vehicle_name(vehicle)
        negotiation = mem.negotiation

        if negotiation is not None:

            if not negotiation.done:
                mem.phase = "HANDOVER"
                return True

            mem.negotiation = None
            target = negotiation.to_vehicle

            if negotiation.status == "completed":
                events.log_vehicle_handover(vehicle, target)
                mem.handover_done = True
                self._ok(vehicle, "HANDOVER_COMPLETED", to=target, sync=False)
                self._sync(target)
                return False

            mem.handover_tried.append(target)
            self._trace(vehicle, "HANDOVER_" + negotiation.status.upper(), to=target, reason=negotiation.reason)
            self._log(vehicle, f"Handover {name} -> {events.vehicle_name(target)} {negotiation.status}"
                               + (f" ({negotiation.reason})" if negotiation.reason else "")
                               + "; trying the next vehicle.", severity="warning")

        candidates = self._handover_candidates(vehicle, mem.handover_tried)

        if not candidates:
            mem.handover_done = True
            if mem.handover_tried:
                self._log(vehicle, f"No vehicle could take over the next cycle; {name} keeps it.",
                          severity="warning")
                self._trace(vehicle, "HANDOVER_NONE")
            return False

        target = candidates[0]
        mem.negotiation = self.bus.start_handover(vehicle, target)
        mem.phase = "HANDOVER"
        self._trace(vehicle, "HANDOVER_REQUEST", to=target)
        return True

    def _at_charging(self, vehicle, record):

        mem = self._mem(vehicle)
        battery = state.battery_of(record) or 0.0

        try:

            if record.get("mission_status") != "CHARGING":
                state.arrive_at_charging(vehicle)        # e.g. re-engaged after a hold
                mem.phase = "CHARGING"
                return

            if battery < settings.battery_charge_target_pct:
                mem.phase = "CHARGING"
                self._progress_sync(vehicle, battery)
                return

            state.start_return(vehicle, to_charging=False, speed_kmh=self.cruise_kmh)
            mem.phase = "DRIVE_TO_STOP_1"
            self._ok(vehicle, "CHARGED", battery=round(battery, 1),
                     message=f"{events.vehicle_name(vehicle)} charged to {battery:.0f} %; returns to Stop 1.")

        except state.TransitionBlocked as exc:
            self._blocked(vehicle, exc.code, str(exc))
        except ValueError as exc:
            self._needs_operator(vehicle, str(exc))

    def _at_origin(self, vehicle, record, snap):

        mem = self._mem(vehicle)
        now = self.engine.sim_seconds

        try:

            if self._battery_low(record):
                with self._lock:
                    if self.baton == vehicle:
                        self.baton = None
                state.depart_to_charging(vehicle, self.cruise_kmh)
                mem.phase = "DRIVE_TO_CHARGING"
                self._ok(vehicle, "TO_CHARGING",
                         message=f"{events.vehicle_name(vehicle)} battery low: drives to the charging bay "
                                 "instead of starting a new cycle.")
                return

            if self.dispatch == "handover":
                if self.baton != vehicle:
                    mem.phase = "WAIT_FOR_HANDOVER"
                    return
            else:
                if now - self.last_departure_s < settings.autopilot_depart_headway_s:
                    mem.phase = "WAIT_HEADWAY"
                    return

            validation = safety.validate_start_command(vehicle)

            if not validation["allowed"]:
                self._blocked(vehicle, validation["code"], validation["reason"])
                return

            new_state = state.start_vehicle(vehicle, self.cruise_kmh)

            events.log_vehicle_started(vehicle, new_state["location"])
            events.log_brake_released(vehicle)

            self.last_departure_s = now
            mem.cycle_active = True
            mem.handover_done = False
            mem.handover_tried = []
            mem.negotiation = None
            mem.phase = "DRIVE_TO_STOP_2"
            self._ok(vehicle, "STARTED")

        except state.TransitionBlocked as exc:
            self._blocked(vehicle, exc.code, str(exc))
        except ValueError as exc:
            self._needs_operator(vehicle, str(exc))

    def _resume_on_road(self, vehicle, record, snap):
        """Held between stops (released, re-engaged): continue the current leg."""

        location = record.get("location")
        load = float(record.get("load_percentage") or 0)

        if location == state.STOP_1:
            mission = "MOVING_TO_LOADING"
        elif location == state.STOP_2 and load >= 100:
            mission = "MOVING_TO_WAREHOUSE"
        elif location == state.STOP_3:
            mission = "MOVING_TO_CHARGING" if self._battery_low(record) else "RETURNING_TO_STOP_1"
        elif location == state.STOP_CHARGING:
            mission = "RETURNING_TO_STOP_1"
        else:
            self._needs_operator(vehicle, "it is stopped on the road with no leg to continue")
            return

        try:
            state.resume_drive(vehicle, mission, self.cruise_kmh)
        except state.TransitionBlocked as exc:
            self._blocked(vehicle, exc.code, str(exc))
            return
        except ValueError as exc:
            self._needs_operator(vehicle, str(exc))
            return

        self._mem(vehicle).phase = "RESUMING"
        self._ok(vehicle, "RESUMED", mission=mission)

    def _progress_sync(self, vehicle, percent):

        mem = self._mem(vehicle)
        bucket = int(percent // PROGRESS_SYNC_STEP)

        if bucket != mem.sync_bucket:
            mem.sync_bucket = bucket
            self._sync(vehicle, radio=False)

    def _publish(self, vehicle):
        """next_action of an AUTO vehicle: the engine's action while driving, else the phase."""

        try:
            record = state.get_vehicle_state(vehicle)
        except ValueError:
            return

        pose = self.engine.poses.get(vehicle)
        mem = self._mem(vehicle)

        if record.get("status") == state.STATUS_RUNNING and pose is not None and pose.path:
            action = pose.next_action
        else:
            action = mem.phase

        changes = {}
        if record.get("next_action") != action and record.get("mission_status") != "HELD":
            changes["next_action"] = action
        if record.get("autopilot_mode") != MODE_AUTO:
            changes["autopilot_mode"] = MODE_AUTO
        if changes:
            state.update_vehicle_state(vehicle, **changes)

    # =====================================================================
    # ADVICE (the world engine asks before moving an AUTO vehicle)
    # =====================================================================

    def advise(self, vehicle_id, pose, record):

        base = self.bus.advise(vehicle_id, pose, record) or {}

        if self.modes.get(vehicle_id) != MODE_AUTO:
            return base

        rules = []

        if base.get("stop_within_m") is not None or base.get("speed_cap_mps") is not None:
            rules.append(Rule(base.get("stop_within_m"), base.get("speed_cap_mps"),
                              base.get("action") or "PLATOON_FOLLOW"))

        with self.bus.lock:

            agent = self.bus.agents.get(vehicle_id)
            now_ms = self.bus.now_ms

            rules += self._hazard_rules(vehicle_id, pose, agent)
            rules += self._gap_rules(vehicle_id, pose, agent, now_ms)
            rules += self._slot_rules(vehicle_id, pose, record, agent)
            rules += self._merge_rules(vehicle_id, pose)

            advice = self._merge(rules, pose)

            if agent is not None:
                stopping = (
                    (advice.get("stop_within_m") is not None and advice["stop_within_m"] < 1.0)
                    or (advice.get("speed_cap_mps") is not None and advice["speed_cap_mps"] < 0.05)
                )
                agent.advised_hold = advice.get("action") if stopping else None

        return advice

    def _merge(self, rules, pose):

        if not rules:
            return {}

        remaining = world_module.remaining_distance(self.world, pose)

        stops = [r.stop_within_m for r in rules if r.stop_within_m is not None]
        caps = [r.speed_cap_mps for r in rules if r.speed_cap_mps is not None]

        stop = min(stops) if stops else None
        cap = min(caps) if caps else None

        # A hold point at (or past) the destination is the destination
        # itself: let the normal arrival logic stop there.
        if stop is not None and stop >= remaining - 0.5:
            stop = None

        binding = [
            r for r in rules
            if (r.stop_within_m is not None and r.stop_within_m < min(BINDING_STOP_M, remaining - 0.5))
            or (r.speed_cap_mps is not None and r.speed_cap_mps < self.cruise_kmh / 3.6 - 0.05)
        ]

        advice = {}
        if stop is not None:
            advice["stop_within_m"] = max(0.0, stop)
        if cap is not None:
            advice["speed_cap_mps"] = max(0.0, cap)
        if binding:
            advice["action"] = max(binding, key=lambda r: r.priority).action

        return advice

    # ---- rules ------------------------------------------------------------------------------------

    def _hazard_rules(self, vehicle, pose, agent):

        mem = self._mem(vehicle)

        if agent is None or not pose.path:
            return []

        best = None

        for hazard in agent.hazards.values():

            if hazard.get("about") == vehicle or hazard.get("origin") == vehicle:
                continue
            if (hazard.get("severity") or 0) < 2:
                continue

            position = hazard.get("position") or {}
            if position.get("x") is None or position.get("y") is None:
                continue

            distance = self._route_distance_to(pose, float(position["x"]), float(position["y"]))
            radius = hazard.get("relevance_radius_m") or 100

            if distance is None or distance > radius:
                continue

            if best is None or distance < best[0]:
                best = (distance, hazard)

        name = events.vehicle_name(vehicle)

        if best is None:
            if mem.hazard is not None:
                mem.hazard = None
                self._log(vehicle, f"{name} resumes: the hazard ahead is cleared.")
                self._trace(vehicle, "HAZARD_CLEARED")
            return []

        distance, hazard = best

        if mem.hazard != hazard["denm_id"]:
            first = mem.hazard is None
            mem.hazard = hazard["denm_id"]
            source = hazard.get("about") or hazard.get("origin")
            if first:
                self._log(vehicle, f"{name} holds for a hazard ahead: {hazard.get('cause')} "
                                   f"({events.vehicle_name(source)}, {distance:.0f} m along the route).",
                          severity="warning")
            self._trace(vehicle, "HAZARD_HOLD", cause=hazard.get("cause"), source=source)

        return [Rule(max(0.0, distance - settings.hazard_standoff_m), None, "HOLD_FOR_HAZARD")]

    def _route_distance_to(self, pose, x, y):
        """Metres along the pose's path to the point (x, y), or None if it is not ahead on it."""

        total = 0.0

        for index, segment_id in enumerate(pose.path):

            segment = self.world.segments[segment_id]
            a = self.world.nodes[segment.start]
            b = self.world.nodes[segment.end]
            geometric = math.hypot(b.x - a.x, b.y - a.y) or 1.0

            along = (x - a.x) * segment.dx + (y - a.y) * segment.dy
            across = abs(-(x - a.x) * segment.dy + (y - a.y) * segment.dx)
            start_s = pose.s if index == 0 else 0.0

            if across <= 2.0 and -0.5 <= along <= geometric + 0.5:
                s = max(0.0, min(segment.length, along / geometric * segment.length))
                if s >= start_s - 0.5:
                    return total + max(0.0, s - start_s)

            total += segment.length - start_s

        return None

    def _ignore_parked(self, pose, node):
        """A vehicle parked at a multi-bay stop that is our destination is not in the way."""

        return (
            node is not None
            and node == pose.destination
            and self.world.stop_of_node(node) in MULTI_BAY_STOPS
        )

    def _gap_rules(self, vehicle, pose, agent, now_ms):

        mem = self._mem(vehicle)

        if not pose.path:
            return []

        gaps = []

        # What the radio says (CAMs, dead-reckoned to now).
        if agent is not None:
            for other, entry in agent.neighbours.items():
                cam = entry.get("last_cam")
                if not cam or entry.get("state") not in ("connected", "degraded", "pending"):
                    continue
                if cam.get("segment") is None and self._ignore_parked(pose, cam.get("node")):
                    continue
                gap = agent.gap_to(other, pose, now_ms)
                if gap is not None:
                    gaps.append((gap, other, float(cam.get("speed_mps") or 0.0)))

        # What the radar sees (ground truth, short range).
        for other, other_pose in self.engine.poses.items():
            if other == vehicle:
                continue
            gap = self._true_gap(pose, other_pose)
            if gap is not None and gap <= settings.radar_range_m:
                gaps.append((gap, other, other_pose.speed_mps))

        if not gaps:
            self._gap_clear(vehicle, agent, now_ms)
            return []

        gap, leader, leader_speed = min(gaps)
        decel = self.world.kinematics["max_decel_mps2"]

        rules = [Rule(
            max(0.0, gap - settings.min_gap_m + leader_speed ** 2 / (2 * decel)),
            None, "FOLLOW_GAP")]

        if gap < settings.critical_gap_m:

            rules.append(Rule(0.0, 0.0, "EMERGENCY_BRAKE_GAP"))

            if not mem.gap_critical:
                mem.gap_critical = True
                name = events.vehicle_name(vehicle)
                self._log(vehicle, f"{name} emergency brake: only {gap:.1f} m to "
                                   f"{events.vehicle_name(leader)} ahead.", severity="critical")
                self._trace(vehicle, "GAP_CRITICAL", leader=leader, gap=round(gap, 1))
            if agent is not None:
                me = {"x": None, "y": None}
                x, y = world_module.position(self.world, pose)
                me.update(x=round(x, 2), y=round(y, 2), segment=pose.segment)
                agent._raise_denm("GAP_CRITICAL", "EMERGENCY_BRAKE", now_ms, me)

        else:
            self._gap_clear(vehicle, agent, now_ms)

            if gap < settings.min_gap_m:
                span = max(settings.min_gap_m - settings.critical_gap_m, 0.1)
                cap = max(0.0, leader_speed * (gap - settings.critical_gap_m) / span)
                rules.append(Rule(None, cap, "SLOW_GAP"))

        return rules

    def _gap_clear(self, vehicle, agent, now_ms):

        mem = self._mem(vehicle)

        if mem.gap_critical:
            mem.gap_critical = False
            if agent is not None:
                agent._cancel_denm("GAP_CRITICAL", now_ms)

    def _true_gap(self, pose, other):
        """Ground-truth metres along `pose`'s path to the other pose, or None."""

        if other.segment is not None:
            return world_module.path_gap(self.world, pose, other.segment, other.s)

        if self._ignore_parked(pose, other.node):
            return None

        for segment_id in pose.path:
            segment = self.world.segments[segment_id]
            if segment.end == other.node:
                return world_module.path_gap(self.world, pose, segment_id, segment.length)

        return None

    def _slot_rules(self, vehicle, pose, record, agent):

        if record.get("mission_status") != "MOVING_TO_LOADING":
            return []

        stop_2 = self.world.stop_nodes["stop_2"]

        if pose.destination != stop_2:
            return []

        dock = self.bus.dock()

        if dock is not None and self.bus.config["rsu_enabled"]:
            granted = agent is not None and agent.slot["granted"]
        else:
            # No roadside unit: fall back to "nobody is at the loading point".
            granted = state.stop_2_occupant(except_vehicle=vehicle) is None

        if granted:
            return []

        remaining = world_module.remaining_distance(self.world, pose)

        return [Rule(max(0.0, remaining - settings.slot_hold_m), None, "WAIT_FOR_SLOT")]

    def _merge_rules(self, vehicle, pose):
        """Two lanes into one (e.g. the charging exit joining the return leg)."""

        mine = self._next_merge(pose)

        if mine is None:
            return []

        my_distance, node, my_in, out = mine

        if my_distance > MERGE_ZONE_M:
            return []

        order = _vehicle_order()

        for other, other_pose in self.engine.poses.items():

            if other == vehicle:
                continue

            theirs = self._next_merge(other_pose)

            if theirs is None:
                continue

            distance, other_node, other_in, other_out = theirs

            if other_node != node or other_out != out or other_in == my_in or distance > MERGE_ZONE_M:
                continue

            first = distance < my_distance - 0.5 or (
                abs(distance - my_distance) <= 0.5 and order.get(other, 0) < order.get(vehicle, 0))

            if first:
                return [Rule(max(0.0, my_distance - MERGE_WAIT_M), None, "YIELD_MERGE")]

        return []

    def _next_merge(self, pose):
        """(distance, node, incoming segment, outgoing segment) of the next merge on the path."""

        total = 0.0

        for index, segment_id in enumerate(pose.path[:-1]):
            segment = self.world.segments[segment_id]
            total += segment.length - (pose.s if index == 0 else 0.0)
            if segment.end in self._merge_nodes:
                return total, segment.end, segment_id, pose.path[index + 1]
            if total > MERGE_ZONE_M:
                return None

        return None

    # =====================================================================
    # WATCHDOG TRIP AND RECOVERY
    # =====================================================================

    def trip(self, code, detail):
        """Latch a fleet emergency stop: the tick loop stalled or raised.

        Runs on the watchdog thread. Takes no engine or bus lock (the
        engine may be the thing that is stuck): only state and events.
        """

        with self._lock:
            if self.failed:
                return
            self.failed = {"code": code, "detail": detail, "at": utc_iso(milliseconds=True)}
            self.modes = {v: MODE_MANUAL for v in self.modes}
            self.baton = None

        try:
            hardware.activate_emergency_stop()
        except Exception:
            pass

        stopped = []

        for vehicle in fleet.vehicle_ids():
            try:
                state.emergency_stop(vehicle)
                state.update_vehicle_state(vehicle, autopilot_mode=MODE_MANUAL)
                stopped.append(vehicle)
                events.log_emergency_stop(vehicle, f"Autopilot watchdog: {code}.")
            except Exception:
                continue

        state.ignore_telemetry_before(stopped)

        events.add_event(
            f"[SAFETY] Autopilot watchdog tripped ({code}): fleet emergency stop latched. "
            "The autopilot stays FAILED until an operator recovers it.",
            event_type="safety", severity="critical",
            details={"watchdog": dict(self.failed)},
        )

        for hook in list(self.on_trip):
            try:
                hook()
            except Exception:
                pass

        for vehicle in stopped:
            self._sync(vehicle, radio=False)

    def recover(self):
        """Clear a watchdog trip once the tick loop is healthy again. The fleet stays stopped."""

        if not self.failed:
            return {"recovered": False, "message": "The autopilot is not in FAILED state."}

        if self.engine.status()["running"]:
            last = self.engine.last_tick_mono
            if last is None or time.monotonic() - last > settings.watchdog_ms / 1000:
                raise AutopilotError(
                    "The tick loop is still not running; the autopilot stays FAILED.",
                    "AUTOPILOT_STILL_STALLED")

        previous = self.failed

        with self._lock:
            self.failed = None

        self.watchdog.acknowledge()

        events.add_event(
            "[SAFETY] Autopilot watchdog reset by the operator. Vehicles stay emergency-stopped "
            "until released.", event_type="safety", severity="warning",
        )

        return {"recovered": True, "previous_failure": previous}

    # =====================================================================
    # STATUS
    # =====================================================================

    def watchdog_state(self):

        if self.failed:
            return "FAILED"
        return "ARMED" if self.armed() else "DISARMED"

    def status(self):

        last = self.engine.last_tick_mono

        vehicles = []

        for vehicle in fleet.vehicle_ids():

            try:
                record = state.get_vehicle_state(vehicle)
                pose = self.engine.pose(vehicle)
            except ValueError:
                continue

            mem = self._mem(vehicle)

            vehicles.append({
                "vehicle_id": vehicle,
                "mode": self.modes.get(vehicle, MODE_MANUAL),
                "phase": mem.phase if self.modes.get(vehicle) == MODE_AUTO else MODE_MANUAL,
                "status": record.get("status"),
                "mission_status": record.get("mission_status"),
                "location": record.get("location"),
                "next_action": record.get("next_action"),
                "destination": pose.get("destination_stop") or pose.get("destination"),
                "distance_to_destination_m": pose.get("distance_to_destination_m"),
                "eta_seconds": pose.get("eta_seconds"),
                "speed_kmh": pose.get("speed_kmh"),
                "load_percentage": record.get("load_percentage"),
                "battery": state.battery_of(record),
                "low_battery": mem.low_battery,
                "signal_strength": record.get("signal_strength"),
                "fault": record.get("fault"),
                "holding_for_hazard": mem.hazard,
                "cycles_completed": mem.cycles,
                "charges": mem.charges,
                "distance_driven_m": round(mem.distance_m, 1),
                "in_cycle": mem.cycle_active,
            })

        return {
            "dispatch": self.dispatch,
            "cycle_holder": self.baton if self.dispatch == "handover" else None,
            "time_scale": self.engine.time_scale,
            "engine_running": self.engine.status()["running"],
            "simulated_seconds": round(self.engine.sim_seconds, 1),
            "watchdog": {
                "state": self.watchdog_state(),
                "watchdog_ms": settings.watchdog_ms,
                "thread_running": self.watchdog.running,
                "last_tick_age_ms": (round((time.monotonic() - last) * 1000, 1) if last is not None else None),
                "failure": dict(self.failed) if self.failed else None,
            },
            "road_obstacles": list(self.road_obstacles.values()),
            "radio_blackout": sorted(self.bus.blackout),
            "vehicles": vehicles,
        }

    def health(self):

        return {
            "state": "FAILED" if self.failed else "online",
            "watchdog": self.watchdog_state(),
            "auto_vehicles": len(self.auto_vehicles()),
            "dispatch": self.dispatch,
            "failure": dict(self.failed) if self.failed else None,
        }


# =========================================================
# WATCHDOG THREAD
# =========================================================

class Watchdog:
    """Checks that the tick loop runs while the autopilot is armed.

    Trips (fleet emergency stop, autopilot FAILED) when no tick has
    finished for WATCHDOG_MS, or when a tick raised. Never restarts
    anything by itself.
    """

    def __init__(self, autopilot):

        self.autopilot = autopilot
        self._thread = None
        self._stop = threading.Event()
        self.seen_errors = autopilot.engine.error_count
        self.armed_since = None

    @property
    def running(self):
        return bool(self._thread and self._thread.is_alive())

    def start(self):

        if self.running or self.autopilot.engine.tick_ms <= 0:
            return False

        self._stop.clear()
        self.acknowledge()
        self._thread = threading.Thread(target=self._run, name="autopilot-watchdog", daemon=True)
        self._thread.start()
        return True

    def stop(self):

        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)
        self._thread = None

    def acknowledge(self):
        """Accept the errors seen so far (after a recovery or while disarmed)."""

        self.seen_errors = self.autopilot.engine.error_count
        self.armed_since = None

    def _run(self):

        interval = max(0.05, settings.watchdog_ms / 1000 / 4)

        while not self._stop.wait(interval):
            try:
                self.check()
            except Exception as exc:
                print(f"[autopilot] watchdog check failed: {exc}")

    def check(self, now=None):
        """One watchdog check. Returns the trip code, or None."""

        autopilot = self.autopilot
        engine = autopilot.engine

        if autopilot.failed:
            return None

        if not autopilot.armed():
            self.acknowledge()
            return None

        now = time.monotonic() if now is None else now

        if self.armed_since is None:
            self.armed_since = now

        if engine.error_count > self.seen_errors:
            autopilot.trip("TICK_FAILED", engine.last_exception or engine.last_error or "the tick raised")
            return "TICK_FAILED"

        reference = max(engine.last_tick_mono or 0.0, self.armed_since)
        silent_ms = (now - reference) * 1000

        if silent_ms > settings.watchdog_ms:
            detail = f"no tick for {silent_ms:.0f} ms"
            if engine.last_error:
                detail += f"; last error: {engine.last_error}"
            autopilot.trip("TICK_STALLED", detail)
            return "TICK_STALLED"

        return None


def _pose_of(snapshot):

    return world_module.Pose(
        vehicle_id=snapshot["vehicle_id"], node=snapshot["node"], segment=snapshot["segment"],
        s=snapshot["distance_along_m"], path=list(snapshot["path"]), speed_mps=snapshot["speed_mps"],
        destination=snapshot["destination"],
    )


# =========================================================
# THE SINGLETON, WIRED INTO THE ENGINE AND THE BUS
# =========================================================

controller = Autopilot(world_module.engine, v2v.bus)

world_module.engine.add_tick_hook(controller.tick)

world_module.engine.advisor = controller.advise

v2v.bus.refusal_hooks.append(controller._handover_refusal)

v2v.bus.handover_hooks.append(controller._on_negotiation)
