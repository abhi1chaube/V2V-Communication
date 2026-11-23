# =========================================================
# FASTAPI AUTH DEPENDENCIES (UPGRADE PHASE 6)
# =========================================================
#
# require_role(minimum) is a NO-OP while AUTH_REQUIRED is false
# (ground rule / Phase 6 transition): every route keeps working
# exactly as it did before Phase 6, including the existing
# local-only rule on registry/admin endpoints, until the new
# dashboard has a login screen (Phase 7) and AUTH_REQUIRED is
# turned on. Once it is on, every route carrying this dependency
# needs a logged-in user with at least that role. Emergency stop
# never carries it: stopping must always be possible.
# =========================================================

from fastapi import Request

from services import auth as auth_service
from services import users
from services.settings import settings

from .common import ApiError


def current_user(request: Request):
    """The logged-in user ({id, username, role, via}), or None.

    Checks the session cookie first (set by POST /api/v1/auth/login),
    then an `Authorization: Bearer <api token>` header (services/users.py,
    for scripts).
    """

    token = request.cookies.get(settings.auth_cookie_name)

    if token:
        data = auth_service.verify_session_token(token)
        if data is not None:
            return {"id": data["sub"], "username": data["username"], "role": data["role"], "via": "session"}

    header = request.headers.get("authorization", "")

    if header.lower().startswith("bearer "):
        user = users.user_for_api_token(header[7:].strip())
        if user is not None:
            return {**user, "via": "api_token"}

    return None


def require_login(request: Request):
    """Dependency: always enforced (not gated by AUTH_REQUIRED) — for the
    identity endpoints themselves (session/token management), which only
    make sense for a caller who is actually logged in."""

    user = current_user(request)

    if user is None:
        raise ApiError("Login required.", 401, "UNAUTHENTICATED")

    return user


def require_role(minimum):
    """Dependency factory for action routes. See the module docstring for
    the AUTH_REQUIRED transition behaviour."""

    def dependency(request: Request):

        if not settings.auth_required:
            return None

        user = current_user(request)

        if user is None:
            raise ApiError("Login required.", 401, "UNAUTHENTICATED")

        if not users.at_least(user["role"], minimum):
            raise ApiError(
                f"This action needs the '{minimum}' role or higher; you are '{user['role']}'.",
                403, "INSUFFICIENT_ROLE",
            )

        request.state.user = user
        return user

    return dependency
