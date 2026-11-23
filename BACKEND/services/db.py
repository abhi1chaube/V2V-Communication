# =========================================================
# PERSISTENCE: SQLITE (UPGRADE PHASE 5)
# =========================================================
#
# One file, BACKEND/data/v2v.db (setting DB_FILE). Four tables:
#
#   activity_log     every events.add_event() entry
#   v2v_messages     every message sent on the V2V bus
#   audit_log        every command and registry change (api/audit.py)
#   state_timeline   a pose+state row per vehicle, about once a
#                    second (feeds session replay in Phase 7, and
#                    restores each vehicle's last known position
#                    and load after a restart)
#
# alerts (services/alerts.py) live in their own table, one row
# PER ALERT kept up to date (INSERT OR REPLACE) through its
# lifecycle, so restoring "every alert not yet resolved" after a
# restart is a single query.
#
# WRITES NEVER BLOCK THE CALLER: record() puts a row on a bounded
# queue and returns; a single background thread does the actual
# INSERTs in small batches. A full queue counts and drops the
# write rather than stall whoever called record() (the world
# engine tick, a command, ...). Reads open their own connection
# (SQLite WAL mode: readers never block the writer or each other).
#
# Retention (setting RETENTION_DAYS, default 7, 0 = forever) is
# swept by the writer thread once an hour.
# =========================================================

import queue
import sqlite3
import threading
import time
from datetime import timedelta
from pathlib import Path

from .settings import BACKEND_DIR, settings
from .timeutil import utc_iso, utc_now


DEFAULT_DB_FILE = BACKEND_DIR / "data" / "v2v.db"

CLEANUP_INTERVAL_S = 3600

# table -> (timestamp column, is it append-only for retention purposes)
TIMESTAMPED_TABLES = {
    "activity_log": "timestamp_utc",
    "v2v_messages": "timestamp_utc",
    "audit_log": "timestamp_utc",
    "state_timeline": "timestamp_utc",
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS activity_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp_utc TEXT NOT NULL,
    vehicle TEXT,
    event_type TEXT,
    severity TEXT,
    message TEXT,
    details_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_activity_log_ts ON activity_log(timestamp_utc);
CREATE INDEX IF NOT EXISTS idx_activity_log_vehicle ON activity_log(vehicle, timestamp_utc);

CREATE TABLE IF NOT EXISTS v2v_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    msg_id TEXT,
    timestamp_utc TEXT NOT NULL,
    msg_type TEXT,
    sender TEXT,
    recipient TEXT,
    priority INTEGER,
    payload_json TEXT,
    deliveries_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_v2v_messages_ts ON v2v_messages(timestamp_utc);
CREATE INDEX IF NOT EXISTS idx_v2v_messages_type ON v2v_messages(msg_type, timestamp_utc);

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp_utc TEXT NOT NULL,
    actor TEXT,
    source_ip TEXT,
    command TEXT,
    target TEXT,
    result_code TEXT,
    success INTEGER,
    details_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_audit_log_ts ON audit_log(timestamp_utc);

CREATE TABLE IF NOT EXISTS state_timeline (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp_utc TEXT NOT NULL,
    vehicle TEXT NOT NULL,
    x REAL, y REAL, heading_deg REAL,
    node TEXT, segment TEXT, distance_along_m REAL, speed_kmh REAL,
    status TEXT, mission_status TEXT, location TEXT,
    load_percentage REAL, battery REAL,
    emergency_stop INTEGER, fault TEXT, autopilot_mode TEXT, next_action TEXT
);
CREATE INDEX IF NOT EXISTS idx_state_timeline_ts ON state_timeline(timestamp_utc);
CREATE INDEX IF NOT EXISTS idx_state_timeline_vehicle ON state_timeline(vehicle, timestamp_utc);

CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL,
    created_at TEXT NOT NULL,
    disabled INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS api_tokens (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    token_hash TEXT NOT NULL UNIQUE,
    user_id INTEGER NOT NULL,
    label TEXT,
    created_at TEXT NOT NULL,
    revoked_at TEXT
);

CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    vehicle TEXT,
    severity TEXT,
    kind TEXT NOT NULL,
    message TEXT,
    state TEXT NOT NULL,
    acknowledged_by TEXT,
    acknowledged_at TEXT,
    resolved_by TEXT,
    resolved_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_alerts_state ON alerts(state);
"""


class DBError(ValueError):
    pass


# =========================================================
# STATE (module-level: one database per process)
# =========================================================

_path = None
_queue = None
_thread = None
_stop = threading.Event()
_stats_lock = threading.Lock()
_stats = {"written": 0, "dropped": 0, "batches": 0, "last_error": None}


def path():
    return _path


def configure(db_path=None, wipe=False):
    """Create the file and tables if needed. Safe to call more than once.

    `wipe=True` (tests only) deletes an existing file first, for a
    completely clean database rather than one that merely has its
    tables re-declared over old rows.
    """

    global _path

    _path = Path(db_path or settings.db_file or DEFAULT_DB_FILE)
    _path.parent.mkdir(parents=True, exist_ok=True)

    if wipe and _path.exists():
        _path.unlink()

    conn = sqlite3.connect(str(_path))
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()

    return _path


def start():
    """Start the writer thread. configure() must have run first."""

    global _thread, _queue

    if _thread is not None and _thread.is_alive():
        return False

    if _path is None:
        configure()

    _queue = queue.Queue(maxsize=settings.db_queue_size)
    _stop.clear()
    _thread = threading.Thread(target=_run, name="db-writer", daemon=True)
    _thread.start()
    return True


def stop(flush_timeout=3.0):

    _stop.set()

    if _queue is not None:
        try:
            _queue.put_nowait(None)          # wake a blocked get()
        except queue.Full:
            pass

    if _thread is not None:
        _thread.join(timeout=flush_timeout)


def reset_for_tests(db_path):
    """A completely fresh database, writer restarted (tests only).

    Slow (WAL pragma + schema DDL on a brand new file routinely cost
    several hundred ms on this machine, almost certainly antivirus
    scanning each newly created file): use once per session, not
    between tests. clear_all_for_tests() is the fast per-test reset.
    """

    stop()
    global _path
    _path = None
    configure(db_path, wipe=True)
    with _stats_lock:
        _stats.update(written=0, dropped=0, batches=0, last_error=None)
    start()


def clear_all_for_tests():
    """Delete every row, keep the writer thread running (tests only).

    No schema DDL and no journal-mode change, so this is fast: just a
    DELETE per table on a file that is already set up. Drains the
    write queue first so nothing from the previous test lands after
    the clear.
    """

    deadline = time.monotonic() + 2.0

    while _queue is not None and not _queue.empty() and time.monotonic() < deadline:
        time.sleep(0.005)

    time.sleep(0.01)          # a dequeued-but-not-yet-committed batch finishes

    conn = sqlite3.connect(str(_path), timeout=5)

    try:
        conn.execute("BEGIN")
        for table in (*TIMESTAMPED_TABLES, "alerts"):
            conn.execute(f"DELETE FROM {table}")
        conn.commit()
    finally:
        conn.close()

    with _stats_lock:
        _stats.update(written=0, dropped=0, batches=0, last_error=None)


# =========================================================
# WRITING (never blocks the caller for more than a queue put)
# =========================================================

def record(table, mode="insert", **fields):
    """Queue one row for `table`. mode: "insert" (append) or "upsert"
    (INSERT OR REPLACE, for alerts: one row per alert id, kept current).

    Silently dropped (and counted) if the writer isn't running or the
    queue is full: persistence is best-effort, never a reason to fail
    a command or stall the tick loop.
    """

    if _queue is None:
        with _stats_lock:
            _stats["dropped"] += 1
        return

    try:
        _queue.put_nowait((table, mode, fields))
    except queue.Full:
        with _stats_lock:
            _stats["dropped"] += 1


def statistics():
    with _stats_lock:
        return {**_stats, "queued": _queue.qsize() if _queue is not None else 0,
                "running": bool(_thread and _thread.is_alive())}


def _run():

    conn = sqlite3.connect(str(_path), check_same_thread=False, timeout=5)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")

    last_cleanup = time.monotonic()

    try:
        while True:

            try:
                item = _queue.get(timeout=0.25)
            except queue.Empty:
                item = "_tick_"

            batch = [] if item in (None, "_tick_") else [item]

            while len(batch) < 500:
                try:
                    nxt = _queue.get_nowait()
                except queue.Empty:
                    break
                if nxt is None:
                    break
                batch.append(nxt)

            if batch:
                _flush(conn, batch)

            if time.monotonic() - last_cleanup >= CLEANUP_INTERVAL_S:
                _cleanup(conn)
                last_cleanup = time.monotonic()

            if _stop.is_set() and _queue.empty():
                break
    finally:
        conn.close()


def _flush(conn, batch):

    try:
        conn.execute("BEGIN")
        for table, mode, fields in batch:
            columns = ", ".join(fields)
            placeholders = ", ".join("?" for _ in fields)
            verb = "INSERT OR REPLACE" if mode == "upsert" else "INSERT"
            conn.execute(f"{verb} INTO {table} ({columns}) VALUES ({placeholders})", list(fields.values()))
        conn.commit()
        with _stats_lock:
            _stats["written"] += len(batch)
            _stats["batches"] += 1
    except Exception as exc:
        conn.rollback()
        with _stats_lock:
            _stats["last_error"] = f"{type(exc).__name__}: {exc}"
        print(f"[db] write failed ({len(batch)} rows): {exc}")


def _cleanup(conn):

    days = settings.retention_days

    if days <= 0:
        return

    cutoff = _iso_days_ago(days)

    try:
        for table, column in TIMESTAMPED_TABLES.items():
            conn.execute(f"DELETE FROM {table} WHERE {column} < ?", (cutoff,))
        conn.execute("DELETE FROM alerts WHERE state = 'resolved' AND resolved_at < ?", (cutoff,))
        conn.commit()
    except Exception as exc:
        conn.rollback()
        print(f"[db] retention cleanup failed: {exc}")


def _iso_days_ago(days):

    return utc_iso(utc_now() - timedelta(days=days), milliseconds=True)


# =========================================================
# READING (a fresh connection per call; WAL = never blocks the writer)
# =========================================================

def _read_connection():

    if _path is None:
        raise DBError("The database is not configured yet.")

    conn = sqlite3.connect(str(_path), timeout=5)
    conn.row_factory = sqlite3.Row
    return conn


def _where(conditions, params, start=None, end=None, time_column="timestamp_utc"):

    if start:
        conditions.append(f"{time_column} >= ?")
        params.append(start)
    if end:
        conditions.append(f"{time_column} <= ?")
        params.append(end)


def query_events(start=None, end=None, vehicle=None, event_type=None, limit=200):

    conditions, params = [], []
    _where(conditions, params, start, end)
    if vehicle:
        conditions.append("vehicle = ?")
        params.append(vehicle)
    if event_type:
        conditions.append("event_type = ?")
        params.append(event_type)

    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""

    conn = _read_connection()
    try:
        rows = conn.execute(
            f"SELECT * FROM activity_log {where} ORDER BY id DESC LIMIT ?",
            [*params, limit],
        ).fetchall()
    finally:
        conn.close()

    return [_event_row(r) for r in rows]


def _event_row(row):

    import json as _json

    return {
        "id": row["id"], "timestamp": row["timestamp_utc"], "vehicle": row["vehicle"],
        "event_type": row["event_type"], "severity": row["severity"], "message": row["message"],
        "details": _json.loads(row["details_json"]) if row["details_json"] else {},
    }


def query_v2v(start=None, end=None, msg_type=None, vehicle=None, limit=200):

    conditions, params = [], []
    _where(conditions, params, start, end)
    if msg_type:
        conditions.append("msg_type = ?")
        params.append(msg_type)
    if vehicle:
        conditions.append("(sender = ? OR recipient = ? OR recipient = '*')")
        params.extend([vehicle, vehicle])

    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""

    conn = _read_connection()
    try:
        rows = conn.execute(
            f"SELECT * FROM v2v_messages {where} ORDER BY id DESC LIMIT ?",
            [*params, limit],
        ).fetchall()
    finally:
        conn.close()

    import json as _json

    return [
        {
            "msg_id": r["msg_id"], "timestamp_utc": r["timestamp_utc"], "msg_type": r["msg_type"],
            "sender": r["sender"], "recipient": r["recipient"], "priority": r["priority"],
            "payload": _json.loads(r["payload_json"]) if r["payload_json"] else {},
            "deliveries": _json.loads(r["deliveries_json"]) if r["deliveries_json"] else [],
        }
        for r in rows
    ]


def query_audit(start=None, end=None, actor=None, command=None, limit=200):

    conditions, params = [], []
    _where(conditions, params, start, end)
    if actor:
        conditions.append("actor = ?")
        params.append(actor)
    if command:
        conditions.append("command = ?")
        params.append(command)

    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""

    conn = _read_connection()
    try:
        rows = conn.execute(
            f"SELECT * FROM audit_log {where} ORDER BY id DESC LIMIT ?",
            [*params, limit],
        ).fetchall()
    finally:
        conn.close()

    import json as _json

    return [
        {
            "id": r["id"], "timestamp_utc": r["timestamp_utc"], "actor": r["actor"],
            "source_ip": r["source_ip"], "command": r["command"], "target": r["target"],
            "result_code": r["result_code"], "success": bool(r["success"]),
            "details": _json.loads(r["details_json"]) if r["details_json"] else {},
        }
        for r in rows
    ]


def query_timeline(start=None, end=None, vehicle=None, limit=20000):

    conditions, params = [], []
    _where(conditions, params, start, end)
    if vehicle:
        conditions.append("vehicle = ?")
        params.append(vehicle)

    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""

    conn = _read_connection()
    try:
        rows = conn.execute(
            f"SELECT * FROM state_timeline {where} ORDER BY id ASC LIMIT ?",
            [*params, limit],
        ).fetchall()
    finally:
        conn.close()

    return [dict(r) for r in rows]


def latest_timeline_per_vehicle():
    """Most recent state_timeline row for every vehicle (for safe restore)."""

    conn = _read_connection()
    try:
        rows = conn.execute(
            "SELECT t.* FROM state_timeline t "
            "JOIN (SELECT vehicle, MAX(id) AS max_id FROM state_timeline GROUP BY vehicle) m "
            "ON t.vehicle = m.vehicle AND t.id = m.max_id"
        ).fetchall()
    finally:
        conn.close()

    return {r["vehicle"]: dict(r) for r in rows}


def query_alerts(state=None, vehicle=None, severity=None, limit=200):

    conditions, params = [], []
    if state:
        conditions.append("state = ?")
        params.append(state)
    if vehicle:
        conditions.append("vehicle = ?")
        params.append(vehicle)
    if severity:
        conditions.append("severity = ?")
        params.append(severity)

    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""

    conn = _read_connection()
    try:
        rows = conn.execute(
            f"SELECT * FROM alerts {where} ORDER BY id DESC LIMIT ?",
            [*params, limit],
        ).fetchall()
    finally:
        conn.close()

    return [dict(r) for r in rows]


def open_alerts():
    """Every alert not resolved (for restoring alerts.py's in-memory state)."""

    return [a for a in query_alerts(limit=100000) if a["state"] != "resolved"]


def max_alert_id():

    conn = _read_connection()
    try:
        row = conn.execute("SELECT MAX(id) AS m FROM alerts").fetchone()
    finally:
        conn.close()

    return (row["m"] or 0) if row else 0
