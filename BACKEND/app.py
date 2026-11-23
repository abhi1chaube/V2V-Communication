# =========================================================
# V2V FLEET CONTROL SYSTEM - MAIN BACKEND (FastAPI)
# =========================================================
#
# Creates the FastAPI application: CORS, error envelope,
# startup/shutdown, API docs, and the routers in api/.
#
#   api/common.py           envelope, request parsing, local-only admin
#   api/sync.py             local state <-> ThingsBoard
#   api/routes_root.py      /, /api/health, hardware, servo
#   api/routes_vehicles.py  vehicle registry, ThingsBoard account
#   api/routes_fleet.py     live fleet, mission commands, sensors
#   api/routes_safety.py    emergency stop/release, reset, safety
#   api/routes_events.py    activity log, communication, J2735
#
# Domain logic lives in services/ and does not depend on the
# web framework.
#
# Run:   python app.py
# Docs:  http://127.0.0.1:5000/apidocs
# =========================================================

import asyncio
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from services import alerts
from services import autopilot
from services import db
from services import events
from services import fleet
from services import live
from services import state
from services import v2v
from services import world
from services.settings import settings

from api import routes_alerts
from api import routes_audit
from api import routes_auth
from api import routes_autopilot
from api import routes_events
from api import routes_fleet
from api import routes_history
from api import routes_metrics
from api import routes_root
from api import routes_safety
from api import routes_stream
from api import routes_vehicles
from api import routes_v2v
from api import routes_world
from api.audit import AuditMiddleware
from api.common import API_VERSION, ApiError, error
from api.idempotency import IdempotencyMiddleware
from api.sync import push_pool, thingsboard_pool


BACKEND_DIR = Path(__file__).resolve().parent


# =========================================================
# STARTUP / SHUTDOWN
# =========================================================

def restore_fleet():
    """Safe restore (ground rule 6 / Phase 5): each vehicle's last known
    position and load come back from services/db.py's state timeline, but
    ALWAYS HELD (speed 0, brakes applied, autopilot MANUAL), with the
    emergency latch restored exactly. Never restored as moving.

    Called from the lifespan, before the world engine starts ticking, so
    the first tick already sees the right poses. A standalone function
    (not inlined in the lifespan) so it can be tested directly.
    """

    restored = db.latest_timeline_per_vehicle()

    for vehicle_id in fleet.vehicle_ids():

        row = restored.get(vehicle_id)

        if row is None:
            continue

        state.restore_from_row(vehicle_id, row)

        if row.get("segment"):
            world.engine.place(vehicle_id, segment=row["segment"],
                              s=row.get("distance_along_m") or 0.0, speed_mps=0.0)
        elif row.get("node"):
            world.engine.place(vehicle_id, node=row["node"], speed_mps=0.0)

    if restored:
        events.add_event(
            f"[SYSTEM] Restored {len(restored)} vehicle(s) from the last run; "
            "all held (MANUAL, brakes applied) pending an operator.",
            event_type="system", severity="warning",
        )

    return restored


@asynccontextmanager
async def lifespan(app: FastAPI):

    # Persistence (services/db.py) first: events.log_backend_started()
    # below, and everything else, may try to record() to it.
    db.configure()
    db.start()

    restore_fleet()

    alerts.restore()

    try:
        events.log_backend_started()
    except Exception:
        pass

    # Live push feed (services/live.py): bind this event loop so its Hub
    # can bridge broadcasts from the engine's tick thread (call_soon_threadsafe).
    live.hub.bind_loop(asyncio.get_running_loop())
    live.wire_autopilot(autopilot.controller)

    # Alerts (services/alerts.py): open/auto-resolve from fleet state
    # and the watchdog, every tick.
    world.engine.add_tick_hook(lambda dt: alerts.evaluate(state, v2v, autopilot))

    # Vehicle motion (services/world.py). WORLD_TICK_MS=0 keeps
    # it off (the tests drive it tick by tick instead). The V2V
    # bus, the autopilot and the scenario runner tick inside it.
    # Every vehicle starts in MANUAL.
    world.engine.start(settings.world_tick_ms)

    # Autopilot watchdog: fleet emergency stop if the tick loop
    # stalls or raises while any vehicle is in AUTO.
    autopilot.controller.watchdog.start()

    yield

    autopilot.controller.watchdog.stop()

    world.engine.stop()

    live.hub.unbind_loop()

    thingsboard_pool.shutdown(wait=False, cancel_futures=True)
    push_pool.shutdown(wait=False, cancel_futures=True)
    db.stop()


# =========================================================
# APPLICATION AND API DOCS
# =========================================================

DESCRIPTION = """
IoT Vehicle-to-Vehicle Fleet Control System.

**Every call acts on the real backend.** Emergency stop and reset affect the
whole fleet; deleting with `delete_device=true` permanently deletes the
ThingsBoard device. Vehicle add/edit/delete work only from the computer running
the backend.

Every response uses one envelope:
`{"success": bool, "message": str, "code"?: str, "data"?: any}`.
Errors carry a machine-readable `code` (for example `INVALID_VEHICLE`).
"""

TAGS = [
    {"name": "Fleet", "description": "Live state of every vehicle and service health."},
    {"name": "Commands", "description": "Mission commands: start, loading point, loading, warehouse, complete/handover, manual drive."},
    {"name": "Sensors", "description": "Simulated sensor inputs: radar, brake, QR/AprilTag."},
    {"name": "Safety", "description": "Emergency stop, release and cycle reset (whole fleet), and safety summaries."},
    {"name": "Vehicles", "description": "Fleet registry (data/vehicles.json). Tokens are never returned, only token_hint."},
    {"name": "ThingsBoard", "description": "ThingsBoard account information and manual sync."},
    {
        "name": "J2735",
        "description": "J2735-styled messages and the activity log. The JSON uses J2735 names "
                       "and units but is not UPER-encoded and is not sent over V2X."
    },
    {
        "name": "World",
        "description": "Route map and continuous vehicle positions (world engine). "
                       "New endpoints live under /api/v1."
    },
    {
        "name": "V2V",
        "description": "Simulated vehicle-to-vehicle radio: messages (CAM, BSM, DENM, handover, "
                       "loading slot, shared lane, platoon), links, inboxes, agents, statistics, "
                       "and runtime radio settings to degrade the link in a demo."
    },
    {
        "name": "Autopilot",
        "description": "The backend drives the mission (AUTO) with the same safety checks as the "
                       "commands: enable/disable per vehicle or fleet, status, decision trace, "
                       "simulation speed, watchdog recovery. Every vehicle starts in MANUAL."
    },
    {
        "name": "Scenarios",
        "description": "Scripted, seeded runs with fault injection (obstacles, radio loss, "
                       "breakdowns, low battery) from BACKEND/scenarios/*.json."
    },
    {
        "name": "Live stream",
        "description": "Server-Sent Events push feed (GET /api/v1/stream): fleet/pose deltas, V2V "
                       "messages, the activity log, alerts, autopilot and scenario traces. A full "
                       "snapshot on connect and every SSE_RESYNC_S as a resync."
    },
    {
        "name": "Operations",
        "description": "Alert lifecycle (open/acknowledged/resolved), the audit trail, Prometheus "
                       "metrics, and history/replay over services/db.py's SQLite store."
    },
    {
        "name": "Authentication",
        "description": "Login/logout, the current user, user management (admin) and revocable API "
                       "tokens for scripts. AUTH_REQUIRED (default false) gates every role-checked "
                       "route elsewhere; off by default until the new dashboard has a login screen."
    },
    {"name": "System", "description": "Banner and simulated hardware endpoints."},
]

app = FastAPI(
    title="V2V Fleet Control API",
    version=API_VERSION,
    description=DESCRIPTION,
    openapi_tags=TAGS,
    docs_url="/apidocs",
    redoc_url="/redoc",
    openapi_url="/openapi.json",
    lifespan=lifespan,
)


# =========================================================
# AUDIT TRAIL (api/audit.py)
# =========================================================
# Added first (innermost, closest to the router, since Starlette
# wraps in reverse add order): a replayed Idempotency-Key response
# never reaches it, so a retried command is not double-audited.

app.add_middleware(AuditMiddleware)


# =========================================================
# IDEMPOTENCY-KEY (api/idempotency.py)
# =========================================================
# Added before CORS, so CORS is the outer layer and also adds
# its headers to replayed responses.

app.add_middleware(
    IdempotencyMiddleware,
    ttl_seconds=settings.idempotency_ttl_s
)


# =========================================================
# SECURITY: CORS
# =========================================================
#
# Only the dashboard may call this API from a browser: opened
# from disk (Origin "null") or served from this computer. Set
# CORS_ORIGINS to a comma-separated list to allow other
# origins, or CORS_ORIGINS=* for "any website".
# =========================================================

_cors_setting = settings.cors_origins.strip()

_cors_common = {
    "allow_methods": ["*"],
    "allow_headers": ["*"],
    # Phase 6: the session cookie needs this (Starlette reflects the
    # request's actual Origin instead of "*" whenever credentials are
    # allowed, so this is safe to combine with allow_origins=["*"]).
    "allow_credentials": True,
}

if _cors_setting == "*":

    app.add_middleware(CORSMiddleware, allow_origins=["*"], **_cors_common)

else:

    _origins = [o.strip() for o in _cors_setting.split(",") if o.strip()]

    if _origins:
        app.add_middleware(CORSMiddleware, allow_origins=_origins, **_cors_common)
    else:
        app.add_middleware(
            CORSMiddleware,
            # index.html opened from disk: browsers send "null",
            # or "file://" when file access is enabled.
            allow_origins=["null", "file://"],
            allow_origin_regex=r"^https?://(localhost|127\.0\.0\.1|\[::1\])(:\d+)?$",
            **_cors_common
        )


# =========================================================
# ERROR ENVELOPE
# =========================================================

@app.exception_handler(ApiError)
async def api_error_handler(request: Request, exc: ApiError):

    return exc.to_response()


@app.exception_handler(StarletteHTTPException)
async def http_error_handler(request: Request, exc: StarletteHTTPException):

    if exc.status_code == 404:
        return JSONResponse(
            {
                "success": False,
                "message": "API route not found.",
                "path": request.url.path
            },
            status_code=404
        )

    response = error(
        str(exc.detail),
        exc.status_code,
        f"HTTP_{exc.status_code}"
    )

    if exc.headers:
        response.headers.update(exc.headers)

    return response


@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request, exc: RequestValidationError):

    # 400, not FastAPI's default 422, and in the usual envelope.
    return error(
        "Invalid request.",
        400,
        "VALIDATION_ERROR",
        {"errors": jsonable_encoder(exc.errors())}
    )


@app.exception_handler(Exception)
async def internal_error_handler(request: Request, exc: Exception):

    return JSONResponse(
        {
            "success": False,
            "message": "Internal server error."
        },
        status_code=500
    )


# =========================================================
# ROUTES
# =========================================================

app.include_router(routes_root.router)
app.include_router(routes_fleet.router)
app.include_router(routes_safety.router)
app.include_router(routes_vehicles.router)
app.include_router(routes_events.router)
app.include_router(routes_world.router)
app.include_router(routes_v2v.router)
app.include_router(routes_autopilot.router)
app.include_router(routes_stream.router)
app.include_router(routes_alerts.router)
app.include_router(routes_audit.router)
app.include_router(routes_history.router)
app.include_router(routes_metrics.router)
app.include_router(routes_auth.router)


@app.get("/apispec.json", include_in_schema=False)
def legacy_apispec():
    """The OpenAPI document under the path the old Swagger UI used."""

    return app.openapi()


# =========================================================
# MAIN
# =========================================================

if __name__ == "__main__":

    import uvicorn

    host = settings.backend_host
    port = settings.backend_port
    reload = settings.dev_reload

    print()
    print("=" * 60)
    print("V2V FLEET CONTROL SYSTEM (FastAPI)")
    print("=" * 60)
    print(f"API docs: http://127.0.0.1:{port}/apidocs")
    print(f"Health:   http://127.0.0.1:{port}/api/health")
    print(f"Vehicles: http://127.0.0.1:{port}/api/vehicles")
    print(f"Fleet: {fleet.fleet_size()} vehicle(s) in {fleet.DATA_FILE}")
    print(f"Auto-reload: {'on' if reload else 'off (set DEV_RELOAD=true to enable)'}")
    print("=" * 60)
    print()

    # The import string and working directory let the
    # auto-reloader re-import this module.
    os.chdir(BACKEND_DIR)

    uvicorn.run(
        "app:app",
        host=host,
        port=port,
        reload=reload,
        reload_dirs=[str(BACKEND_DIR)] if reload else None,
    )
