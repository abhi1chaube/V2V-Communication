# =========================================================
# SESSION TOKENS AND LOGIN RATE LIMITING (UPGRADE PHASE 6)
# =========================================================
#
# A minimal, self-contained signed token (JWT-shaped: header,
# payload, HMAC-SHA256 signature; no new dependency) carried in
# an HttpOnly cookie. Chosen over a server-side session store
# because GET /api/v1/stream (Server-Sent Events, Phase 5) is
# read by the browser's EventSource, which cannot set a custom
# Authorization header — a cookie is sent automatically, so one
# mechanism covers both ordinary requests and the live stream.
#
# The signing key is AUTH_SECRET_KEY if set, else a random key
# generated once per process: every restart then invalidates
# sessions (an operator logs in again), which is an acceptable
# trade for not having to manage a secret file. API tokens
# (services/users.py) are a different mechanism (opaque, looked
# up in SQLite, revocable) for scripts.
#
# Framework-free: FastAPI-specific plumbing (reading the request,
# raising the HTTP error) lives in api/auth.py.
# =========================================================

import base64
import hashlib
import hmac
import json
import secrets
import threading
import time
from collections import deque

from .settings import settings


# A fallback key for when AUTH_SECRET_KEY is not set, generated once
# per process (module import), not persisted.
_random_secret = secrets.token_bytes(32)


def _secret_key():

    fixed = settings.auth_secret_key

    return fixed.encode("utf-8") if fixed else _random_secret


def _b64encode(data):

    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64decode(text):

    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


# =========================================================
# SESSION TOKENS
# =========================================================

def issue_session_token(user, ttl_s=None):
    """A signed token carrying the user's id, username, role and expiry."""

    ttl = settings.auth_session_ttl_s if ttl_s is None else ttl_s

    header = _b64encode(json.dumps({"alg": "HS256", "typ": "JWT"}, separators=(",", ":")).encode("utf-8"))
    payload = _b64encode(json.dumps({
        "sub": user["id"], "username": user["username"], "role": user["role"],
        "exp": int(time.time() + ttl),
    }, separators=(",", ":")).encode("utf-8"))

    signing_input = f"{header}.{payload}".encode("ascii")
    signature = _b64encode(hmac.new(_secret_key(), signing_input, hashlib.sha256).digest())

    return f"{header}.{payload}.{signature}"


def verify_session_token(token):
    """The token's payload dict if the signature is valid and it has not
    expired, else None. Never raises on a malformed token."""

    if not token or token.count(".") != 2:
        return None

    header, payload, signature = token.split(".")

    expected = _b64encode(hmac.new(_secret_key(), f"{header}.{payload}".encode("ascii"), hashlib.sha256).digest())

    if not hmac.compare_digest(signature, expected):
        return None

    try:
        data = json.loads(_b64decode(payload))
    except (ValueError, UnicodeDecodeError):
        return None

    if not isinstance(data, dict) or data.get("exp", 0) < time.time():
        return None

    return data


# =========================================================
# LOGIN RATE LIMITING (per source address, failed attempts only)
# =========================================================

_attempts = {}
_attempts_lock = threading.Lock()


def login_rate_limited(address):

    now = time.monotonic()

    with _attempts_lock:
        window = _attempts.setdefault(address, deque())
        while window and now - window[0] > settings.auth_login_window_s:
            window.popleft()
        return len(window) >= settings.auth_login_max_attempts


def record_failed_login(address):

    with _attempts_lock:
        _attempts.setdefault(address, deque()).append(time.monotonic())


def reset_rate_limit_for_tests():

    with _attempts_lock:
        _attempts.clear()
