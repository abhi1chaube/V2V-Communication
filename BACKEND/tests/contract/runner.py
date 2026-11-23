"""Run the API contract cases against one backend implementation.

Usage (from anywhere):
    python runner.py fastapi app <out.json>
    python runner.py flask   <module> <out.json>

golden_flask.json was recorded with the "flask" mode from the
original Flask backend (app_flask_legacy.py) before the move to
FastAPI. tests/test_contract.py replays it against the FastAPI app.

Runs every case in cases.py, in order, in THIS process, with:
  - a throwaway vehicles.json holding two FAKE vehicles
    (the real BACKEND/data/vehicles.json is never read or written),
  - ThingsBoard replaced by in-process fakes,
  - all real HTTP blocked, so a missed fake fails loudly
    instead of calling the cloud.

Volatile values (timestamps, the work-dir path) are normalised so
two runs of the same implementation produce identical output.
"""

import json
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
BACKEND = HERE.parent.parent
# Outside OneDrive: sync locks files and folders in the project
# tree, which makes recreating the work directory fail.
WORK = Path(tempfile.gettempdir()) / "v2v-contract-work"

FIXTURE = {
    "vehicles": [
        {
            "name": "Vehicle_01",
            "device_id": "00000000-0000-4000-8000-000000000001",
            "token": "FAKE_TOKEN_0001",
            "color": "yellow",
        },
        {
            "name": "Vehicle_02",
            "device_id": "00000000-0000-4000-8000-000000000002",
            "token": "FAKE_TOKEN_0002",
            "color": "blue",
        },
    ]
}

VOLATILE_KEYS = {
    "timestamp", "secMark", "last_seen", "_ts", "time",
    "generated_at", "created_at", "updated_at", "last_updated",
    "uptime", "started_at",
}

_DATETIME = re.compile(r"\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}")


# =========================================================
# ENVIRONMENT
# =========================================================

def prepare_environment():
    WORK.mkdir(parents=True, exist_ok=True)
    for leftover in WORK.iterdir():
        if leftover.is_dir():
            shutil.rmtree(leftover, ignore_errors=True)
        else:
            leftover.unlink(missing_ok=True)

    data_file = WORK / "vehicles.json"
    data_file.write_text(json.dumps(FIXTURE, indent=2), encoding="utf-8")

    # Set before any service module is imported. load_dotenv()
    # in the services never overrides variables already set.
    os.environ["FLEET_DATA_FILE"] = str(data_file)
    os.environ["THINGSBOARD_URL"] = "http://thingsboard.invalid"
    os.environ["THINGSBOARD_USERNAME"] = "contract-test"
    os.environ["THINGSBOARD_PASSWORD"] = "contract-test"
    os.environ.pop("CORS_ORIGINS", None)
    os.environ.pop("ALLOW_REMOTE_FLEET_ADMIN", None)
    # Deterministic: no background vehicle motion during the run.
    os.environ["WORLD_TICK_MS"] = "0"
    # STATE_SOURCE is passed through from the caller (both modes
    # are checked by tests/test_contract.py).

    os.chdir(BACKEND)
    if str(BACKEND) not in sys.path:
        sys.path.insert(0, str(BACKEND))


def block_network():
    import fakes

    fakes.block_network()


def install_thingsboard_fakes():
    # Shared with the pytest suite (tests/contract/fakes.py).
    # Default behaviour: no telemetry, every push succeeds,
    # every device exists.
    import fakes

    return fakes.install()


# =========================================================
# CLIENT ADAPTERS
# =========================================================

class FlaskAdapter:
    def __init__(self, module):
        self.app = module.app
        self.client = self.app.test_client()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def call(self, method, path, json_body=None, raw=None, remote=False):
        kwargs = {"method": method}
        if json_body is not None:
            kwargs["json"] = json_body
        if raw is not None:
            kwargs["data"] = raw
            kwargs["content_type"] = "text/plain"
        if remote:
            kwargs["environ_base"] = {"REMOTE_ADDR": "10.1.2.3"}
        response = self.client.open(path, **kwargs)
        body = response.get_json(silent=True)
        return response.status_code, body if body is not None else response.get_data(as_text=True)


class FastAPIAdapter:
    def __init__(self, module):
        from fastapi.testclient import TestClient

        self.local = TestClient(module.app, client=("127.0.0.1", 50000), raise_server_exceptions=False)
        self.remote = TestClient(module.app, client=("10.1.2.3", 50001), raise_server_exceptions=False)

    def __enter__(self):
        # Runs the lifespan (startup event) exactly once.
        self.local.__enter__()
        return self

    def __exit__(self, *exc):
        self.local.__exit__(*exc)
        return False

    def call(self, method, path, json_body=None, raw=None, remote=False):
        client = self.remote if remote else self.local
        kwargs = {}
        if json_body is not None:
            kwargs["json"] = json_body
        if raw is not None:
            kwargs["content"] = raw
            kwargs["headers"] = {"Content-Type": "text/plain"}
        response = client.request(method, path, **kwargs)
        try:
            body = response.json()
        except ValueError:
            body = response.text
        return response.status_code, body


# =========================================================
# NORMALISATION
# =========================================================

def normalise(value, key=None):
    if key in VOLATILE_KEYS and value is not None:
        return "<volatile>"
    if isinstance(value, dict):
        return {k: normalise(v, k) for k, v in sorted(value.items())}
    if isinstance(value, list):
        return [normalise(v) for v in value]
    if isinstance(value, str):
        text = value.replace(str(WORK), "<WORK>").replace(str(WORK).replace("\\", "/"), "<WORK>")
        return _DATETIME.sub("<datetime>", text)
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


# =========================================================
# MAIN
# =========================================================

def run(kind, module_name):
    prepare_environment()
    block_network()
    install_thingsboard_fakes()

    import importlib
    from cases import CASES  # noqa: E402  (HERE is on sys.path as the script dir)

    module = importlib.import_module(module_name)
    adapter_class = FlaskAdapter if kind == "flask" else FastAPIAdapter

    results = {}
    with adapter_class(module) as adapter:
        for name, method, path, options in CASES:
            status, body = adapter.call(
                method,
                path,
                json_body=options.get("json"),
                raw=options.get("raw"),
                remote=options.get("remote", False),
            )
            results[name] = {
                "request": f"{method} {path}",
                "status": status,
                "body": normalise(body),
            }
    return results


def main():
    if len(sys.argv) != 4 or sys.argv[1] not in ("flask", "fastapi"):
        print(__doc__)
        sys.exit(2)

    kind, module_name, out = sys.argv[1], sys.argv[2], Path(sys.argv[3]).resolve()
    sys.path.insert(0, str(HERE))
    results = run(kind, module_name)
    out.write_text(json.dumps(results, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"{len(results)} cases written to {out}")


if __name__ == "__main__":
    main()
