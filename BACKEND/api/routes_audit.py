# =========================================================
# AUDIT TRAIL — /api/v1/audit
# =========================================================

from typing import Optional

from fastapi import APIRouter, Depends, Query

from services import db

from .common import error, require_local, success


router = APIRouter(prefix="/api/v1/audit", tags=["Operations"])


@router.get("", summary="Audit trail", dependencies=[Depends(require_local)])
def audit(
    start: Optional[str] = Query(None, alias="from", description="ISO 8601 UTC, inclusive."),
    end: Optional[str] = Query(None, alias="to", description="ISO 8601 UTC, inclusive."),
    actor: Optional[str] = Query(None, description="Source address (no user accounts until Phase 6)."),
    command: Optional[str] = Query(None, description="Exact \"METHOD /path\", e.g. 'POST /api/start'."),
    limit: int = Query(100, ge=1, le=1000),
):
    """Every command and registry change (api/audit.py's middleware, covering
    every POST/PUT/PATCH/DELETE under /api): actor, source IP, command,
    target (the vehicle, where one is named), result code, success,
    timestamp. The actor is the source address until Phase 6 adds users.
    Admin-only in spirit; local only for now, like the other admin-ish
    endpoints before roles exist. 503 DB_NOT_READY if the database has not
    started yet."""

    try:
        items = db.query_audit(start=start, end=end, actor=actor, command=command, limit=limit)
    except db.DBError as exc:
        return error(str(exc), 503, "DB_NOT_READY")

    return success("Audit trail retrieved.", {"count": len(items), "entries": items})
