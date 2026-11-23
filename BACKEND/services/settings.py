# =========================================================
# SETTINGS
# =========================================================
#
# Every tunable value of the backend, in one validated place.
#
# Values come from environment variables, then BACKEND/.env,
# then the defaults below (an environment variable always wins
# over .env). Names are case-insensitive. See
# BACKEND/.env.example for the full list.
#
# A bad value (for example BACKEND_PORT=abc) stops the backend
# at startup with a message naming the setting, instead of
# failing later in the middle of a request.
#
# Later upgrade phases add their own settings here (tick rate,
# radio range, packet loss, heartbeat timeout, ...).
# =========================================================

from pathlib import Path
from typing import Literal, Optional

from dotenv import load_dotenv
from pydantic import Field, SecretStr, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict


BACKEND_DIR = Path(__file__).resolve().parent.parent

ENV_FILE = BACKEND_DIR / ".env"

# Load .env into the process environment (never overriding a
# variable that is already set). Settings below are read from
# the environment, and fleet.py still scans it once for legacy
# Vehicle_NN_* keys when vehicles.json does not exist yet.
load_dotenv(ENV_FILE)


class Settings(BaseSettings):

    model_config = SettingsConfigDict(
        case_sensitive=False,
        extra="ignore",
        # "FLEET_DATA_FILE=" (as in .env.example) means "not set",
        # not an empty path.
        env_ignore_empty=True,
    )

    # ---- ThingsBoard ---------------------------------------------
    thingsboard_url: str = Field(
        "https://thingsboard.cloud",
        description="ThingsBoard server URL.",
    )
    thingsboard_username: Optional[str] = Field(
        None,
        description="ThingsBoard tenant user (needs TENANT_ADMIN for device management).",
    )
    thingsboard_password: Optional[SecretStr] = Field(
        None,
        description="ThingsBoard password. Never printed.",
    )
    thingsboard_ca_bundle: str = Field(
        "",
        description="Path to a PEM CA bundle for TLS verification. Overrides the trust store.",
    )
    thingsboard_verify_tls: bool = Field(
        True,
        description="Verify ThingsBoard TLS certificates. Ignored when a CA bundle is set.",
    )
    thingsboard_trust_store: Literal["system", "certifi"] = Field(
        "system",
        description="Certificates used to verify TLS: 'system' = the Windows certificate store "
                    "(includes a corporate root certificate installed by IT), 'certifi' = the "
                    "public CA bundle shipped with requests.",
    )

    # ---- Safety --------------------------------------------------
    qr_always_detected: bool = Field(
        False,
        description="Demo override: treat the AprilTag/QR marker as always detected, so the "
                    "QR interlock never blocks a command.",
    )

    # ---- Commands ------------------------------------------------
    command_lock_timeout_s: float = Field(
        20.0, gt=0,
        description="How long a command waits for another command on the same vehicle "
                    "before failing with 409 COMMAND_IN_PROGRESS.",
    )
    idempotency_ttl_s: int = Field(
        600, ge=0,
        description="How long a response is kept for replay under its Idempotency-Key (0 = off).",
    )

    # ---- World model and simulation -----------------------------
    state_source: Literal["backend", "thingsboard_mirror"] = Field(
        "backend",
        description="Who owns vehicle state. 'backend' (default): the backend's own state is "
                    "the truth and ThingsBoard only receives it. 'thingsboard_mirror': the old "
                    "behaviour, state is read back from ThingsBoard (for an external device or "
                    "the legacy simulator writing to ThingsBoard).",
    )
    world_file: Optional[Path] = Field(
        None,
        description="Route map (nodes, segments, lanes). Default: BACKEND/config/world.json.",
    )
    world_tick_ms: int = Field(
        200, ge=0,
        description="Simulation step of the world engine in ms (vehicle motion). 0 = off.",
    )
    sim_seed: int = Field(
        1234,
        description="Seed for all simulated randomness, so a run can be repeated exactly.",
    )

    # ---- V2V message bus (services/v2v.py) -----------------------
    # Starting values; POST /api/v1/v2v/config changes them at runtime.
    v2v_comm_range_m: float = Field(150.0, gt=0, description="Direct vehicle-to-vehicle radio range.")
    v2v_loss_rate: float = Field(0.02, ge=0, le=1, description="Probability a message is lost on one hop.")
    v2v_latency_min_ms: float = Field(20.0, ge=0, description="Minimum latency of one hop.")
    v2v_latency_max_ms: float = Field(80.0, ge=0, description="Maximum latency of one hop.")
    v2v_rsu_enabled: bool = Field(True, description="Roadside units relay messages and manage the loading slot.")
    v2v_cam_interval_ms: float = Field(500.0, gt=0, description="How often each vehicle broadcasts a CAM heartbeat.")
    v2v_cam_timeout_ms: float = Field(1500.0, gt=0, description="A neighbour in range not heard for this long is LOST.")
    v2v_handover_timeout_ms: float = Field(2000.0, gt=0, description="Wait per handover step before one retry.")
    v2v_target_gap_m: float = Field(25.0, gt=0, description="Gap a platoon follower keeps to the vehicle ahead.")

    # ---- Autopilot (services/autopilot.py) -----------------------
    # The autopilot runs inside the world engine's tick (WORLD_TICK_MS),
    # on the same simulated clock as the motion and the radio.
    autopilot_dispatch: Literal["handover", "concurrent"] = Field(
        "handover",
        description="How AUTO vehicles share the loop. 'handover' (default): one vehicle runs a "
                    "cycle at a time and hands the next cycle over at Stop 3 (V2V negotiation). "
                    "'concurrent': every AUTO vehicle cycles, kept apart by the loading slot, "
                    "shared-lane right-of-way and gap rules.",
    )
    autopilot_cruise_kmh: float = Field(18.0, gt=0, le=40, description="Speed the autopilot drives at.")
    autopilot_depart_headway_s: float = Field(
        10.0, ge=0, description="Concurrent dispatch: minimum time between two departures from Stop 1.")
    min_gap_m: float = Field(10.0, gt=0, description="Below this gap to the vehicle ahead, slow down.")
    critical_gap_m: float = Field(4.0, gt=0, description="Below this gap, emergency brake and raise a DENM.")
    hazard_standoff_m: float = Field(8.0, ge=0, description="Distance kept to a hazard (DENM) ahead on the route.")
    slot_hold_m: float = Field(12.0, ge=0, description="Where a vehicle waits for the loading-slot grant, before Stop 2.")
    radar_range_m: float = Field(20.0, gt=0, description="Simulated radar range (road obstacles, vehicle ahead).")
    loading_rate_pct_per_s: float = Field(10.0, gt=0, description="Autopilot loading speed at Stop 2.")
    unloading_rate_pct_per_s: float = Field(20.0, gt=0, description="Autopilot unloading speed at Stop 3.")
    watchdog_ms: float = Field(
        1000.0, ge=100,
        description="The tick loop must run at least this often while any vehicle is in AUTO; "
                    "otherwise the watchdog latches a fleet emergency stop.",
    )
    time_scale: float = Field(1.0, ge=0.25, le=8.0, description="Simulation speed (demo: 0.25x to 8x).")

    # ---- Battery model ---------------------------------------------
    battery_drain_pct_per_km: float = Field(15.0, ge=0, description="Battery used per km driven.")
    battery_idle_drain_pct_per_min: float = Field(0.5, ge=0, description="Battery used per minute while not charging.")
    battery_charge_pct_per_s: float = Field(2.0, gt=0, description="Charging speed at the charging bay.")
    battery_low_pct: float = Field(20.0, ge=0, le=100, description="Below this: low-battery DENM, go and charge.")
    battery_charge_target_pct: float = Field(95.0, gt=0, le=100, description="Charge until this level.")

    # ---- Live stream (services/live.py, api/routes_stream.py) ----
    sse_resync_s: float = Field(30.0, gt=0, description="Send a full snapshot this often on every SSE connection.")
    sse_queue_size: int = Field(300, gt=0, description="Per-connection SSE buffer. A slow client is disconnected, not backed up forever.")

    # ---- Persistence (services/db.py) -----------------------------
    db_file: Optional[Path] = Field(
        None, description="SQLite database file. Default: BACKEND/data/v2v.db.")
    db_queue_size: int = Field(20000, gt=0, description="Writer queue depth before new writes are dropped and counted.")
    retention_days: int = Field(7, ge=0, description="Rows older than this are deleted (0 = keep forever).")

    # ---- ThingsBoard push throttling (api/sync.py) -----------------
    tb_push_min_interval_s: float = Field(
        1.0, ge=0,
        description="At most one routine push per vehicle in this window; coalesced updates are sent once "
                    "the window ends. A safety-relevant change (emergency, brake, hazard, fault) is never delayed.",
    )

    # ---- Alerts (services/alerts.py) -------------------------------
    vehicle_offline_s: float = Field(10.0, gt=0, description="No heartbeat for this long raises a VEHICLE_OFFLINE alert.")

    # ---- Observability ---------------------------------------------
    log_format: Literal["json", "text"] = Field("json", description="Backend log format on stdout.")

    # ---- Authentication and roles (services/users.py, services/auth.py) --
    # Transition (ground rule / Phase 6): false until the new dashboard has
    # a login screen (Phase 7). While false, every route behaves exactly as
    # before (the existing local-only rule for registry/admin endpoints
    # stays in force); role dependencies are a no-op. While true, every
    # mission/admin endpoint needs a logged-in user with enough role,
    # EXCEPT emergency stop, which never needs one (stopping must always
    # be possible).
    auth_required: bool = Field(False, description="Enforce login and roles. See the note above.")
    auth_secret_key: Optional[str] = Field(
        None,
        description="HMAC key for session tokens. Unset = a random key generated at startup (every "
                    "restart invalidates sessions; API tokens are unaffected, they are looked up in "
                    "SQLite, not signed). Set a fixed value to keep sessions alive across restarts.",
    )
    auth_session_ttl_s: float = Field(3600.0, gt=0, description="Session token lifetime.")
    auth_cookie_name: str = Field("v2v_session", description="Session cookie name.")
    auth_cookie_secure: bool = Field(
        False, description="Mark the session cookie Secure (HTTPS only). Off by default for local http "
                           "development; turn on behind TLS.")
    auth_login_max_attempts: int = Field(5, gt=0, description="Failed logins allowed per window, per source address.")
    auth_login_window_s: float = Field(300.0, gt=0, description="Login rate-limit window.")
    auth_scrypt_n: int = Field(16384, gt=0, description="scrypt cost parameter N (must be a power of 2).")


    # ---- Fleet registry ------------------------------------------
    fleet_data_file: Optional[Path] = Field(
        None,
        description="Vehicle registry file. Default: BACKEND/data/vehicles.json.",
    )

    # ---- HTTP server and access ----------------------------------
    backend_host: str = Field("0.0.0.0", description="Address uvicorn listens on.")
    backend_port: int = Field(5000, ge=1, le=65535, description="Port uvicorn listens on.")
    dev_reload: bool = Field(False, description="Restart the server when a .py file changes.")
    cors_origins: str = Field(
        "",
        description="Comma-separated browser origins allowed to call the API, or * for any. "
                    "Empty = the dashboard from disk and localhost only.",
    )
    allow_remote_fleet_admin: bool = Field(
        False,
        description="Allow vehicle add/edit/delete from other computers.",
    )

    # ---- Derived values ------------------------------------------
    @property
    def thingsboard_base_url(self) -> str:
        return self.thingsboard_url.rstrip("/")

    @property
    def thingsboard_tls_verify(self):
        """The value for requests' verify=: a CA bundle path, or True/False."""
        bundle = self.thingsboard_ca_bundle.strip()
        return bundle if bundle else self.thingsboard_verify_tls

    @property
    def fleet_data_path(self) -> Path:
        return Path(self.fleet_data_file or BACKEND_DIR / "data" / "vehicles.json")


def _load():

    try:
        return Settings()
    except ValidationError as exc:
        problems = "\n".join(
            f"  {'.'.join(str(part) for part in error['loc']).upper()}: {error['msg']}"
            for error in exc.errors()
        )
        raise SystemExit(
            "Invalid backend settings (environment or BACKEND/.env):\n"
            f"{problems}\n"
            "See BACKEND/.env.example for the allowed values."
        ) from None


settings = _load()
