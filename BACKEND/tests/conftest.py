"""Test isolation for the whole pytest session.

Runs before any test module is imported, so the backend never sees
the real BACKEND/.env values that matter or data/vehicles.json:
  - a throwaway vehicles.json with two fake vehicles (in the temp folder)
  - a ThingsBoard URL that cannot resolve, placeholder credentials
  - ThingsBoard replaced by tests/contract/fakes.py, real HTTP blocked

Fixtures:
  fake_tb   the FakeThingsBoard controller (change its behaviour per test)
  api       a TestClient for the FastAPI app, from 127.0.0.1, with the
            fleet, state, events and caches reset before each test
  remote    the same app seen from another computer (10.1.2.3)
"""

import json
import os
import sys
import tempfile
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
BACKEND = HERE.parent
WORK = Path(tempfile.gettempdir()) / "v2v-pytest-work"
DATA_FILE = WORK / "vehicles.json"
DB_FILE = WORK / "v2v-test.db"

FIXTURE = {
    "vehicles": [
        {"name": "Vehicle_01", "device_id": "00000000-0000-4000-8000-000000000001",
         "token": "FAKE_TOKEN_0001", "color": "yellow"},
        {"name": "Vehicle_02", "device_id": "00000000-0000-4000-8000-000000000002",
         "token": "FAKE_TOKEN_0002", "color": "blue"},
    ]
}

WORK.mkdir(parents=True, exist_ok=True)
DATA_FILE.write_text(json.dumps(FIXTURE, indent=2), encoding="utf-8")

os.environ.update({
    "FLEET_DATA_FILE": str(DATA_FILE),
    "THINGSBOARD_URL": "http://thingsboard.invalid",
    "THINGSBOARD_USERNAME": "pytest",
    "THINGSBOARD_PASSWORD": "pytest",
    # The world engine's thread stays off; tests call engine.tick(dt).
    "WORLD_TICK_MS": "0",
    # A throwaway SQLite file in %TEMP%, never BACKEND/data/v2v.db.
    "DB_FILE": str(DB_FILE),
    "RETENTION_DAYS": "7",
})
for name in ("CORS_ORIGINS", "ALLOW_REMOTE_FLEET_ADMIN", "QR_ALWAYS_DETECTED",
             "COMMAND_LOCK_TIMEOUT_S", "IDEMPOTENCY_TTL_S", "STATE_SOURCE",
             "WORLD_FILE", "SIM_SEED"):
    os.environ.pop(name, None)

for path in (BACKEND, HERE / "contract"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))


@pytest.fixture(scope="session")
def fake_tb():
    import fakes

    fakes.block_network()
    return fakes.install()


@pytest.fixture(scope="session")
def _clients(fake_tb):
    from fastapi.testclient import TestClient

    import app as backend

    local = TestClient(backend.app, client=("127.0.0.1", 50000), raise_server_exceptions=False)
    remote = TestClient(backend.app, client=("10.1.2.3", 50001), raise_server_exceptions=False)

    # Run the lifespan once for the session (it shuts the
    # ThingsBoard thread pool down on exit).
    with local:
        yield local, remote


def reset_world(fake_tb):
    from services import events, fleet, j2735, simrng, state, v2v, world
    from services.settings import settings
    from api import commands, sync
    from api.idempotency import IdempotencyMiddleware

    DATA_FILE.write_text(json.dumps(FIXTURE, indent=2), encoding="utf-8")
    fleet._loaded_signature = None          # force a re-read of the registry

    with state._lock:
        state.vehicles.clear()
        state._ignore_before.clear()
        state._placements.clear()

    events.clear_events()
    j2735.forget(*{v["name"] for v in FIXTURE["vehicles"]}, "Vehicle_03", "Truck_03")
    sync._missing_counts.clear()
    commands._locks.clear()
    IdempotencyMiddleware.clear_all()
    fake_tb.reset()

    with world.engine._lock:
        world.engine.poses.clear()
        world.engine.ticks = 0
        world.engine.sim_seconds = 0.0
    with sync._links_lock:
        sync._links.clear()

    settings.qr_always_detected = False
    settings.command_lock_timeout_s = 20.0
    settings.state_source = "backend"
    simrng.reset(1234)
    v2v.bus.reset()

    # Phase 4: every vehicle MANUAL, no scenario, healthy watchdog.
    import time
    from services import autopilot, scenarios

    scenarios.runner.active = None
    scenarios.runner.last = None
    scenarios.runner.directory = scenarios.SCENARIO_DIR
    autopilot.controller.reset_all()
    autopilot.controller.failed = None
    autopilot.controller.dispatch = "handover"
    autopilot.controller.scenario_active = False
    autopilot.controller.watchdog.acknowledge()
    world.engine.time_scale = 1.0
    world.engine.last_tick_mono = time.monotonic()
    world.engine.error_count = 0
    world.engine.last_exception = None
    world.engine.last_error = None
    for name, value in (("loading_rate_pct_per_s", 10.0), ("autopilot_depart_headway_s", 10.0)):
        setattr(settings, name, value)

    # Phase 5: a clean database (rows only: schema/WAL setup happens once,
    # at session start, in _clients below — recreating it per test is
    # much slower here), fresh alerts, push throttle and SSE hub.
    from services import alerts, db, live

    db.clear_all_for_tests()
    alerts.reset_for_tests()
    sync.reset_push_throttle()
    live.hub.reset_for_tests()
    live._reset()


@pytest.fixture
def api(_clients, fake_tb):
    reset_world(fake_tb)
    yield _clients[0]
    reset_world(fake_tb)


@pytest.fixture
def remote(_clients, api):
    return _clients[1]
