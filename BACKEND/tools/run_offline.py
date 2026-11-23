"""Start the backend for development checks, isolated from the real cloud.

- a throwaway vehicles.json with two FAKE vehicles (in the temp folder),
  recreated on every start; the real BACKEND/data/vehicles.json is untouched
- ThingsBoard pointed at an unresolvable host, so nothing is sent to (or read
  from) the real ThingsBoard account; pushes fail fast and are reported as
  telemetry_synced: false, which is expected here
- everything else as normal: world engine ticking, V2V bus, port 5000

Usage (from BACKEND):   venv\\Scripts\\python tools\\run_offline.py [port]
Launch config:          "v2v-backend-offline" in .claude/launch.json
"""

import json
import os
import sys
import tempfile
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent

work = Path(tempfile.gettempdir()) / "v2v-offline"
work.mkdir(exist_ok=True)
data = work / "vehicles.json"
data.write_text(json.dumps({"vehicles": [
    {"name": "Vehicle_01", "device_id": "00000000-0000-4000-8000-000000000001",
     "token": "FAKE_TOKEN_0001", "color": "yellow"},
    {"name": "Vehicle_02", "device_id": "00000000-0000-4000-8000-000000000002",
     "token": "FAKE_TOKEN_0002", "color": "blue"},
]}, indent=2), encoding="utf-8")

os.environ["FLEET_DATA_FILE"] = str(data)
os.environ["THINGSBOARD_URL"] = "http://thingsboard.invalid"     # fails in ~0.1 s
os.environ["THINGSBOARD_USERNAME"] = "offline"
os.environ["THINGSBOARD_PASSWORD"] = "offline"

os.chdir(BACKEND)
sys.path.insert(0, str(BACKEND))

import uvicorn  # noqa: E402

uvicorn.run("app:app", host="127.0.0.1", port=int(sys.argv[1]) if len(sys.argv) > 1 else 5000)
