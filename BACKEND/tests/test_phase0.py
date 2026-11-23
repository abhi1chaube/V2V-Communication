"""Phase 0 tests: UTC timestamps and the validated settings module.

Every check runs in a fresh interpreter with a throwaway vehicles.json
and placeholder ThingsBoard values, so the real BACKEND/.env and
data/vehicles.json are never used and nothing is printed from them.

Run from BACKEND:   venv\\Scripts\\python -m pytest tests -q
"""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent

UTC_SECONDS = r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$"
UTC_MILLIS = r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$"


@pytest.fixture(scope="module")
def isolated_env():
    work = Path(tempfile.gettempdir()) / "v2v-phase0-tests"
    work.mkdir(exist_ok=True)
    data = work / "vehicles.json"
    data.write_text(json.dumps({"vehicles": [
        {"name": "Vehicle_01", "device_id": "00000000-0000-4000-8000-000000000001",
         "token": "FAKE_TOKEN_0001", "color": "yellow"},
    ]}), encoding="utf-8")
    return {
        **os.environ,
        "FLEET_DATA_FILE": str(data),
        "THINGSBOARD_URL": "http://thingsboard.invalid",
        "THINGSBOARD_USERNAME": "phase0-test",
        "THINGSBOARD_PASSWORD": "phase0-test",
    }


def run_python(code, env):
    return subprocess.run(
        [sys.executable, "-c", f"import sys; sys.path.insert(0, {str(BACKEND)!r})\n{code}"],
        cwd=BACKEND, env=env, capture_output=True, text=True, timeout=60,
    )


# ---- UTC ---------------------------------------------------------------

def test_event_timestamps_are_utc_iso(isolated_env):
    result = run_python(
        "import re, datetime\n"
        "from services import events\n"
        "events.add_event('phase0 check', 'Vehicle_01')\n"
        "stamp = events.get_events(1)[0]['timestamp']\n"
        f"assert re.match({UTC_SECONDS!r}, stamp), stamp\n"
        "parsed = datetime.datetime.strptime(stamp, '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=datetime.timezone.utc)\n"
        "drift = abs((datetime.datetime.now(datetime.timezone.utc) - parsed).total_seconds())\n"
        "assert drift < 5, f'not UTC now: drift {drift}s'\n"
        "print('ok')",
        isolated_env,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_utc_iso_formats(isolated_env):
    result = run_python(
        "import re, datetime\n"
        "from services.timeutil import utc_iso\n"
        f"assert re.match({UTC_SECONDS!r}, utc_iso())\n"
        f"assert re.match({UTC_MILLIS!r}, utc_iso(milliseconds=True))\n"
        "ist = datetime.timezone(datetime.timedelta(hours=5, minutes=30))\n"
        "moment = datetime.datetime(2026, 11, 16, 11, 38, 41, tzinfo=ist)\n"
        "assert utc_iso(moment) == '2026-11-16T06:08:41Z', utc_iso(moment)\n"
        "print('ok')",
        isolated_env,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_simulator_has_no_utcnow():
    source = (BACKEND / "vehicle_simulator.py").read_text(encoding="utf-8")
    assert "utcnow(" not in source


# ---- Settings ----------------------------------------------------------

def test_invalid_settings_stop_startup_with_named_errors(isolated_env):
    env = {**isolated_env, "BACKEND_PORT": "abc", "DEV_RELOAD": "maybe"}
    result = run_python("import services.settings", env)
    assert result.returncode != 0
    assert "BACKEND_PORT" in result.stderr
    assert "DEV_RELOAD" in result.stderr
    assert ".env.example" in result.stderr


def test_empty_values_mean_unset(isolated_env):
    env = {**isolated_env, "FLEET_DATA_FILE": "", "THINGSBOARD_CA_BUNDLE": ""}
    result = run_python(
        "from services.settings import settings as s\n"
        "assert s.fleet_data_file is None\n"
        "assert s.fleet_data_path.name == 'vehicles.json'\n"
        "assert s.thingsboard_tls_verify in (True, False)\n"
        "print('ok')",
        env,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_environment_overrides_and_types(isolated_env):
    env = {
        **isolated_env,
        "backend_port": "5055",               # names are case-insensitive
        "ALLOW_REMOTE_FLEET_ADMIN": "true",
        "THINGSBOARD_CA_BUNDLE": r"C:\certs\corp.pem",
        "THINGSBOARD_URL": "https://tb.example.test/",
    }
    result = run_python(
        "from services.settings import settings as s\n"
        "assert s.backend_port == 5055\n"
        "assert s.allow_remote_fleet_admin is True\n"
        "assert s.thingsboard_tls_verify == r'C:\\certs\\corp.pem'\n"
        "assert s.thingsboard_base_url == 'https://tb.example.test'\n"
        "print('ok')",
        env,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_password_is_not_shown_in_settings_repr(isolated_env):
    env = {**isolated_env, "THINGSBOARD_PASSWORD": "phase0-secret-value"}
    result = run_python(
        "from services.settings import settings as s\n"
        "print(repr(s))",
        env,
    )
    assert result.returncode == 0, result.stderr
    assert "phase0-secret-value" not in result.stdout


def test_no_direct_environment_reads_left():
    """Configuration goes through services/settings.py only."""
    offenders = []
    for path in [BACKEND / "app.py", *sorted((BACKEND / "api").glob("*.py")),
                 *sorted((BACKEND / "services").glob("*.py"))]:
        if path.name == "settings.py":
            continue
        text = path.read_text(encoding="utf-8")
        if "os.getenv(" in text or "load_dotenv(" in text:
            offenders.append(path.name)
    assert offenders == []
