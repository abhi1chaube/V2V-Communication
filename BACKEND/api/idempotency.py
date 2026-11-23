# =========================================================
# IDEMPOTENCY-KEY SUPPORT
# =========================================================
#
# A client that retries a command (a timeout, a flaky Wi-Fi
# link, a double click) can send the same Idempotency-Key
# header both times. The first request runs; a repeat with the
# same key and the same request gets the stored response again
# (header Idempotent-Replay: true) instead of running the
# command twice.
#
#   same key, different method/path/body -> 409 IDEMPOTENCY_KEY_REUSED
#   same key while the first is running  -> 409 IDEMPOTENCY_IN_PROGRESS
#   key longer than 200 chars / not text -> 400 INVALID_IDEMPOTENCY_KEY
#
# Applies to POST, PUT, PATCH and DELETE. Requests without the
# header are not affected. Responses with status 5xx, and
# 409 COMMAND_IN_PROGRESS, are not stored, so a retry runs
# again. Stored
# responses expire after IDEMPOTENCY_TTL_S (default 600 s);
# at most MAX_ENTRIES are kept. Memory only (lost on restart).
# =========================================================

import hashlib
import json
import time
from collections import OrderedDict
from threading import Lock


MAX_ENTRIES = 1000

MAX_KEY_LENGTH = 200

UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


class _Entry:

    __slots__ = ("fingerprint", "expires", "pending", "status", "headers", "body")

    def __init__(self, fingerprint, expires):
        self.fingerprint = fingerprint
        self.expires = expires
        self.pending = True
        self.status = None
        self.headers = None
        self.body = None


class IdempotencyMiddleware:

    # Every instance, so tests can clear stored responses.
    instances = []

    def __init__(self, app, ttl_seconds=600):

        self.app = app

        self.ttl = ttl_seconds

        self._entries = OrderedDict()

        self._lock = Lock()

        IdempotencyMiddleware.instances.append(self)

    @classmethod
    def clear_all(cls):

        for instance in cls.instances:
            instance.clear()

    # -----------------------------------------------------

    def clear(self):

        with self._lock:
            self._entries.clear()

    def _purge(self, now):

        expired = [key for key, entry in self._entries.items() if entry.expires <= now and not entry.pending]

        for key in expired:
            del self._entries[key]

        while len(self._entries) > MAX_ENTRIES:
            self._entries.popitem(last=False)

    # -----------------------------------------------------

    async def __call__(self, scope, receive, send):

        if (
            self.ttl <= 0
            or scope["type"] != "http"
            or scope["method"] not in UNSAFE_METHODS
        ):
            return await self.app(scope, receive, send)

        key = None

        for name, value in scope.get("headers", []):
            if name == b"idempotency-key":
                key = value
                break

        if key is None:
            return await self.app(scope, receive, send)

        try:
            key = key.decode("ascii").strip()
        except UnicodeDecodeError:
            key = ""

        if not key or len(key) > MAX_KEY_LENGTH or not key.isprintable():
            return await _send_json(send, 400, {
                "success": False,
                "message": f"Idempotency-Key must be 1-{MAX_KEY_LENGTH} printable ASCII characters.",
                "code": "INVALID_IDEMPOTENCY_KEY"
            })

        # Read the whole body so it can be fingerprinted, then
        # replay it to the application.
        chunks = []

        while True:
            message = await receive()
            if message["type"] != "http.request":
                break
            chunks.append(message.get("body", b""))
            if not message.get("more_body", False):
                break

        body = b"".join(chunks)

        fingerprint = hashlib.sha256(
            scope["method"].encode()
            + b" " + scope.get("path", "").encode()
            + b"?" + scope.get("query_string", b"")
            + b"\n" + body
        ).hexdigest()

        now = time.monotonic()

        with self._lock:

            self._purge(now)

            entry = self._entries.get(key)

            if entry is not None and entry.expires <= now and not entry.pending:
                del self._entries[key]
                entry = None

            if entry is None:
                entry = _Entry(fingerprint, now + self.ttl)
                self._entries[key] = entry
                owner = True
            else:
                owner = False

        if not owner:

            if entry.fingerprint != fingerprint:
                return await _send_json(send, 409, {
                    "success": False,
                    "message": "This Idempotency-Key was already used for a different request.",
                    "code": "IDEMPOTENCY_KEY_REUSED"
                })

            if entry.pending:
                return await _send_json(send, 409, {
                    "success": False,
                    "message": "A request with this Idempotency-Key is still running.",
                    "code": "IDEMPOTENCY_IN_PROGRESS"
                })

            await send({
                "type": "http.response.start",
                "status": entry.status,
                "headers": entry.headers + [(b"idempotent-replay", b"true")]
            })

            return await send({"type": "http.response.body", "body": entry.body})

        # First request with this key: run it and record the response.
        replayed = False

        async def replay_receive():
            nonlocal replayed
            if not replayed:
                replayed = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()

        captured = {"status": None, "headers": [], "body": []}

        async def capture_send(message):
            if message["type"] == "http.response.start":
                captured["status"] = message["status"]
                captured["headers"] = [
                    (name, value)
                    for name, value in message.get("headers", [])
                    if name.lower() != b"content-length"
                ]
            elif message["type"] == "http.response.body":
                captured["body"].append(message.get("body", b""))
            await send(message)

        try:
            await self.app(scope, replay_receive, capture_send)
        except BaseException:
            with self._lock:
                self._entries.pop(key, None)
            raise

        body_bytes = b"".join(captured["body"])

        with self._lock:

            if (
                captured["status"] is None
                or captured["status"] >= 500
                # Temporary: the vehicle was busy. A retry must run.
                or (captured["status"] == 409 and b'"COMMAND_IN_PROGRESS"' in body_bytes)
            ):
                self._entries.pop(key, None)
            else:
                entry.status = captured["status"]
                entry.headers = captured["headers"] + [
                    (b"content-length", str(len(body_bytes)).encode())
                ]
                entry.body = body_bytes
                entry.pending = False


async def _send_json(send, status, payload):

    body = json.dumps(payload).encode()

    await send({
        "type": "http.response.start",
        "status": status,
        "headers": [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode())
        ]
    })

    await send({"type": "http.response.body", "body": body})
