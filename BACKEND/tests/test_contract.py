"""API contract test: the FastAPI backend must answer like the Flask backend it
replaced, except for changes made on purpose and listed below.

tests/contract/golden_flask.json was recorded from the Flask app with
tests/contract/runner.py (84 requests, fake vehicles, ThingsBoard faked, no
network). This test replays the same requests against the FastAPI app and
compares status codes and response bodies.

Do not edit the golden file to make this pass. A difference is either a
regression to fix, or an intentional change that must be listed here with its
reason (and covered by its own test).

Run from BACKEND:   venv\\Scripts\\python -m pytest tests -q
"""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
CONTRACT = HERE / "contract"
GOLDEN = CONTRACT / "golden_flask.json"
RESULT = Path(tempfile.gettempdir()) / "v2v-contract-fastapi-result.json"

from api.common import API_VERSION  # noqa: E402  (conftest.py sets sys.path)


# ---------------------------------------------------------------------------
# Intentional changes
# ---------------------------------------------------------------------------

# Keys removed from BOTH bodies, anywhere in them, before comparing.
IGNORED_KEYS = {
    "telemetry_synced": "Phase 1 f: every command response reports whether the "
                        "ThingsBoard push worked (additive field).",
    "telemetry_error": "Phase 1 f: reason when the push failed (additive field).",
    "test_field": "Phase 1 k: leftover placeholder removed from the state record.",
    "qr_status": "Phase 1 a: the AprilTag/QR reading is now honoured and simulated per "
                 "stop (seen at a stop, lost while moving). Covered by test_phase1.py.",
    "Apriltag_status": "Phase 1 a: same as qr_status.",
    "thingsboard_link": "Phase 2: per-vehicle ThingsBoard health (reachable, last push) "
                        "added to fleet and vehicle responses (additive field).",
    "eta_seconds": "Phase 2: new state field filled by the world engine (additive; the "
                   "engine is off during this run, so it stays null).",
    "comm_hold": "Phase 3: new state field, true while every vehicle in V2V range is "
                 "silent (additive; covered by test_phase3.py).",
    "autopilot_mode": "Phase 4: new state field, MANUAL or AUTO (additive; covered by "
                      "test_phase4.py).",
    "fault": "Phase 4: new state field, a simulated breakdown such as BREAKDOWN, null when "
             "healthy (additive; covered by test_phase4.py).",
}


def _version_is_current(status, body):
    return status == 200 and body["data"]["version"] == API_VERSION


def _communication_is_measured(status, body):
    data = body["data"]
    statuses = set(data["vehicle_statuses"].values())
    return (
        status == 200
        and body["success"] is True
        and statuses <= {"connected", "degraded", "lost", "isolated", "unknown"}
        and data["connected"] == ("connected" in statuses)
        and set(data["details"]) == set(data["vehicles"])
    )


BACKEND_OWNS_STATE = (
    "Phase 2: with STATE_SOURCE=backend a ThingsBoard read is a health check only, "
    "so an empty device no longer marks the vehicle with _thingsboard_error "
    "('No usable telemetry available.'). Mirror mode keeps the old answer."
)

# Case name -> how the new answer differs and why.
#   status / code : expected new status and error code (body not compared)
#   check         : callable(status, body) -> bool (body not compared)
#   drop          : body paths removed from both before comparing the rest
#                   ("*" = every item of a list)
#   modes         : only in these STATE_SOURCE modes (default: all)
CASE_OVERRIDES = {
    "fleet": {
        "reason": BACKEND_OWNS_STATE,
        "drop": [("data", "vehicles", "*", "_thingsboard_error")],
        "modes": {"backend"},
    },
    "fleet_three": {
        "reason": BACKEND_OWNS_STATE,
        "drop": [("data", "vehicles", "*", "_thingsboard_error")],
        "modes": {"backend"},
    },
    "health": {
        "reason": "Phase 2: /api/health reports the world engine's status; Phase 4 adds the "
                  "autopilot and watchdog state; Phase 5 adds db/stream/alerts status (all additive).",
        "drop": [("world",), ("autopilot",), ("db",), ("stream",), ("alerts",)],
    },
    "root": {
        "reason": "The version is defined once (api.common.API_VERSION). Flask reported "
                  "2.0.0 here but 2.1.0 in its Swagger spec.",
        "check": _version_is_current,
        "drop": [("data", "version")],
    },
    "servo_out_of_range": {
        "reason": "Phase 1 h: an out-of-range angle is a 400 INVALID_ANGLE, not a crash (500).",
        "status": 400,
        "code": "INVALID_ANGLE",
    },
    "communication": {
        "reason": "Phase 1 g / Phase 3: status is measured on the V2V radio (CAM "
                  "heartbeats, range, loss) instead of always saying 'connected'.",
        "check": _communication_is_measured,
    },
    "cycle_complete": {
        "reason": "Phase 3: the handover is negotiated over the V2V bus; data.handover "
                  "reports the outcome and messages (additive). The rest is unchanged.",
        "drop": [("data", "handover")],
    },
}


# ---------------------------------------------------------------------------

MODES = ("backend", "thingsboard_mirror")


@pytest.fixture(scope="module", params=MODES)
def results(request):
    # Separate process: the backend's services keep module-level state, so the
    # run must start from a clean interpreter. Run once per STATE_SOURCE mode.
    mode = request.param
    out = RESULT.with_name(f"{RESULT.stem}-{mode}.json")
    completed = subprocess.run(
        [sys.executable, str(CONTRACT / "runner.py"), "fastapi", "app", str(out)],
        capture_output=True,
        text=True,
        timeout=180,
        env={**os.environ, "STATE_SOURCE": mode},
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    return mode, json.loads(out.read_text(encoding="utf-8"))


GOLDEN_CASES = json.loads(GOLDEN.read_text(encoding="utf-8"))


def _strip(value):
    if isinstance(value, dict):
        return {k: _strip(v) for k, v in value.items() if k not in IGNORED_KEYS}
    if isinstance(value, list):
        return [_strip(v) for v in value]
    return value


def _drop(node, path):
    if not path:
        return
    key, rest = path[0], path[1:]
    if key == "*":
        if isinstance(node, list):
            for item in node:
                _drop(item, rest)
        return
    if not isinstance(node, dict):
        return
    if rest:
        _drop(node.get(key), rest)
    else:
        node.pop(key, None)


@pytest.mark.parametrize("name", list(GOLDEN_CASES))
def test_matches_flask(name, results):
    mode, cases = results
    expected = GOLDEN_CASES[name]
    actual = cases.get(name)

    assert actual is not None, f"case {name} did not run"
    assert actual["request"] == expected["request"]

    override = CASE_OVERRIDES.get(name, {})

    if mode not in override.get("modes", MODES):
        override = {}

    if "status" in override:
        assert actual["status"] == override["status"], actual
        assert actual["body"].get("code") == override["code"], actual
        return

    if "check" in override:
        assert override["check"](actual["status"], actual["body"]), (
            f"{expected['request']}: intentional change no longer holds: {override['reason']}\n"
            f"actual: {json.dumps(actual['body'], indent=2)[:2000]}"
        )
        if "drop" not in override:
            return

    assert actual["status"] == expected["status"], (
        f"{expected['request']}: status {actual['status']} != {expected['status']}\n"
        f"body: {json.dumps(actual['body'], indent=2)[:2000]}"
    )

    want = _strip(expected["body"])
    got = _strip(actual["body"])

    for path in override.get("drop", []):
        _drop(want, path)
        _drop(got, path)

    assert got == want, (
        f"{expected['request']}: body differs\n"
        f"expected: {json.dumps(want, indent=2)[:3000]}\n"
        f"actual:   {json.dumps(got, indent=2)[:3000]}"
    )


def test_every_override_names_a_real_case():
    assert set(CASE_OVERRIDES) <= set(GOLDEN_CASES)
