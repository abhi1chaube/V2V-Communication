# =========================================================
# OBSERVABILITY: /metrics (Prometheus text format)
# =========================================================
#
# No envelope (not an /api/ route): Prometheus scrapes plain
# text. include_in_schema=False keeps it off the OpenAPI/Swagger
# document, same spirit as /apispec.json in app.py.
# =========================================================

from fastapi import APIRouter
from fastapi.responses import PlainTextResponse

from services import autopilot
from services import db
from services import live
from services import v2v
from services import world


router = APIRouter()


def _line(buf, name, value, help_text=None, metric_type="gauge", labels=""):

    if help_text:
        buf.append(f"# HELP {name} {help_text}")
        buf.append(f"# TYPE {name} {metric_type}")

    buf.append(f"{name}{labels} {value}")


@router.get("/metrics", include_in_schema=False)
def metrics():

    buf = []

    w = world.engine.status()
    _line(buf, "v2v_world_tick_ms", w["tick_ms"], "Configured world engine tick length.")
    _line(buf, "v2v_world_ticks_total", w["ticks"], "Ticks run since start.", "counter")
    _line(buf, "v2v_world_time_scale", w["time_scale"], "Current simulation speed multiplier.")
    _line(buf, "v2v_world_running", 1 if w["running"] else 0, "1 if the tick loop thread is running.")

    ap = autopilot.controller.status()
    _line(buf, "v2v_autopilot_watchdog_failed", 1 if ap["watchdog"]["state"] == "FAILED" else 0,
         "1 if the autopilot watchdog has tripped.")
    _line(buf, "v2v_autopilot_auto_vehicles", sum(1 for v in ap["vehicles"] if v["mode"] == "AUTO"),
         "Vehicles currently in AUTO.")

    stats = v2v.bus.statistics()
    _line(buf, "v2v_messages_sent_total", stats.get("sent", 0), "V2V messages sent.", "counter")
    _line(buf, "v2v_messages_delivered_total", stats.get("delivered", 0), "V2V messages delivered.", "counter")
    _line(buf, "v2v_messages_lost_total", stats.get("lost", 0), "V2V messages lost (simulated loss).", "counter")
    _line(buf, "v2v_messages_duplicate_total", stats.get("duplicate", 0), "Duplicate V2V messages rejected.", "counter")
    _line(buf, "v2v_messages_stale_total", stats.get("stale", 0), "Stale (expired TTL) V2V messages rejected.", "counter")
    _line(buf, "v2v_packet_loss_rate", stats.get("config", {}).get("loss_rate", 0), "Configured V2V packet loss rate.")

    for msg_type, count in stats.get("by_type", {}).items():
        buf.append(f'v2v_messages_by_type_total{{type="{msg_type}"}} {count}')

    _line(buf, "v2v_sse_clients", live.hub.client_count(), "Connected SSE clients.")

    dbstats = db.statistics()
    _line(buf, "v2v_db_writer_queue_depth", dbstats["queued"], "Rows waiting to be written.")
    _line(buf, "v2v_db_writer_written_total", dbstats["written"], "Rows written.", "counter")
    _line(buf, "v2v_db_writer_dropped_total", dbstats["dropped"], "Rows dropped (queue was full).", "counter")
    _line(buf, "v2v_db_writer_running", 1 if dbstats["running"] else 0, "1 if the writer thread is running.")

    from api.sync import push_statistics
    push = push_statistics()
    _line(buf, "v2v_tb_push_sent_total", push.get("pushed", 0), "ThingsBoard pushes actually sent.", "counter")
    _line(buf, "v2v_tb_push_coalesced_total", push.get("coalesced", 0),
         "ThingsBoard pushes delayed by throttling (still sent later).", "counter")
    _line(buf, "v2v_tb_push_failed_total", push.get("failed", 0), "ThingsBoard pushes that failed.", "counter")

    return PlainTextResponse("\n".join(buf) + "\n")
