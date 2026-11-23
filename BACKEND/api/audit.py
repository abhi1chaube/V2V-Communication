# =========================================================
# AUDIT TRAIL (UPGRADE PHASE 5)
# =========================================================
#
# Every command and registry change (POST/PUT/PATCH/DELETE under
# /api) is recorded: actor, source IP, command (method + path),
# target (the vehicle, where one is named), result code, success,
# timestamp. One ASGI middleware covers every current and future
# route instead of instrumenting each one by hand.
#
# The actor is the source address until Phase 6 adds users
# (ground rule: no authentication yet). Never stores a token,
# password, or other secret: request bodies are redacted first.
#
# Reads go through services/db.py (GET /api/v1/audit, local only
# for now, same as the other admin-ish endpoints before Phase 6
# adds roles).
# =========================================================

import json

from starlette.requests import Request as StarletteRequest

from services import db
from services import fleet
from services.settings import settings
from services.timeutil import utc_iso

from .auth import current_user


AUDITED_METHODS = {"POST", "PUT", "PATCH", "DELETE"}

# Never persisted, even redacted: these endpoints are too noisy or
# too sensitive to be useful in an audit trail.
EXCLUDED_PATHS = {"/api/events/clear"}

SECRET_KEYS = {"token", "password", "thingsboard_password", "access_token", "secret"}


def redact(value):
    """Deep-copy `value` with secret-looking keys masked."""

    if isinstance(value, dict):
        return {
            key: ("***" if key.lower() in SECRET_KEYS and val else redact(val))
            for key, val in value.items()
        }
    if isinstance(value, list):
        return [redact(item) for item in value]
    return value


class AuditMiddleware:

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):

        if scope["type"] != "http" or scope["method"] not in AUDITED_METHODS:
            return await self.app(scope, receive, send)

        path = scope.get("path", "")

        if path in EXCLUDED_PATHS:
            return await self.app(scope, receive, send)

        chunks = []

        async def capture_receive():
            message = await receive()
            if message["type"] == "http.request":
                chunks.append(message.get("body", b""))
            return message

        captured = {"status": None, "body": []}

        async def capture_send(message):
            if message["type"] == "http.response.start":
                captured["status"] = message["status"]
            elif message["type"] == "http.response.body":
                captured["body"].append(message.get("body", b""))
            await send(message)

        await self.app(scope, capture_receive, capture_send)

        try:
            self._record(scope, b"".join(chunks), captured)
        except Exception as exc:          # the audit trail must never break a request
            print(f"[audit] record failed: {exc}")

    def _record(self, scope, request_body, captured):

        client = scope.get("client")
        source_ip = client[0] if client else None

        # Phase 6: the actual user once AUTH_REQUIRED is on (session cookie
        # or API token); the source address otherwise (also the fallback
        # before any auth exists at all).
        actor = source_ip or "unknown"

        if settings.auth_required:
            user = current_user(StarletteRequest(scope))
            if user is not None:
                actor = user["username"]

        body = {}
        if request_body:
            try:
                parsed = json.loads(request_body)
                if isinstance(parsed, dict):
                    body = parsed
            except ValueError:
                pass

        target = None
        if body.get("vehicle"):
            target = fleet.resolve(body["vehicle"]) or str(body["vehicle"])
        else:
            for part in reversed(scope.get("path", "").split("/")):
                if part and fleet.resolve(part):
                    target = fleet.resolve(part)
                    break

        response_json = {}
        raw = b"".join(captured["body"])
        if raw:
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, dict):
                    response_json = parsed
            except ValueError:
                pass

        status = captured["status"] or 0

        db.record(
            "audit_log",
            timestamp_utc=utc_iso(milliseconds=True),
            actor=actor,
            source_ip=source_ip,
            command=f"{scope['method']} {scope.get('path', '')}",
            target=target,
            result_code=response_json.get("code"),
            success=1 if response_json.get("success", status < 400) else 0,
            details_json=json.dumps({"status": status, "body": redact(body)}, default=str),
        )
