# =========================================================
# AUTHENTICATION AND USER MANAGEMENT — /api/v1/auth
# =========================================================
#
# Login/logout/me and API tokens work regardless of AUTH_REQUIRED
# (they are identity endpoints, not permission gates: creating an
# API token needs an identity to attach it to either way). User
# management (create/list/update/delete) is local only, same
# precedent as the vehicle registry, PLUS an admin role once
# AUTH_REQUIRED is on — see api/auth.py's module docstring.
# =========================================================

from typing import Optional

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from services import auth as auth_service
from services import users
from services.settings import settings

from .auth import current_user, require_login, require_role
from .common import documented_body, error, json_body, parse_bool, require_local, success


router = APIRouter(prefix="/api/v1/auth", tags=["Authentication"])


class LoginBody(BaseModel):
    username: str = Field(examples=["admin"])
    password: str


class CreateUserBody(BaseModel):
    username: str = Field(examples=["alice"])
    password: str
    role: str = Field("viewer", description="viewer, operator or admin.")


class UpdateUserBody(BaseModel):
    role: Optional[str] = Field(None, description="viewer, operator or admin.")
    disabled: Optional[bool] = None


class TokenBody(BaseModel):
    label: Optional[str] = Field(None, examples=["ci-script"])


def _public(user):
    return {"id": user["id"], "username": user["username"], "role": user["role"]}


# =========================================================
# LOGIN / LOGOUT / ME
# =========================================================

@router.post("/login", summary="Log in", openapi_extra=documented_body(LoginBody))
def login(request: Request, data: dict = Depends(json_body)):
    """Sets an HttpOnly session cookie on success (also read by the SSE
    stream, which cannot send a custom header). 401 INVALID_CREDENTIALS;
    429 LOGIN_RATE_LIMITED after too many failed attempts from this
    address."""

    address = request.client.host if request.client else "unknown"

    if auth_service.login_rate_limited(address):
        return error(
            f"Too many failed logins from this address. Try again in under "
            f"{settings.auth_login_window_s:g} seconds.",
            429, "LOGIN_RATE_LIMITED",
        )

    user = users.authenticate(data.get("username"), data.get("password"))

    if user is None:
        auth_service.record_failed_login(address)
        return error("Invalid username or password.", 401, "INVALID_CREDENTIALS")

    token = auth_service.issue_session_token(user)

    response = success("Logged in.", {"user": _public(user)})
    response.set_cookie(
        settings.auth_cookie_name, token, max_age=int(settings.auth_session_ttl_s),
        httponly=True, samesite="lax", secure=settings.auth_cookie_secure, path="/",
    )
    return response


@router.post("/logout", summary="Log out")
def logout():

    response = success("Logged out.")
    response.delete_cookie(settings.auth_cookie_name, path="/")
    return response


@router.get("/me", summary="The current user", dependencies=[Depends(require_login)])
def me(request: Request):

    return success("Current user.", _public(current_user(request)))


# =========================================================
# USER MANAGEMENT (local only, + admin role once AUTH_REQUIRED is on)
# =========================================================

@router.post(
    "/users", summary="Create a user", status_code=201,
    dependencies=[Depends(require_local), Depends(require_role("admin"))],
    openapi_extra=documented_body(CreateUserBody),
)
def create_user(data: dict = Depends(json_body)):
    """400 INVALID_USERNAME/INVALID_PASSWORD/INVALID_ROLE; 409 USERNAME_TAKEN.
    To create the very FIRST admin (before any user, and so before login is
    even possible), use the CLI instead:
    `venv\\Scripts\\python.exe -m services.users create-admin`."""

    try:
        user = users.create_user(data.get("username"), data.get("password"), data.get("role", "viewer"))
    except users.UserError as exc:
        return error(str(exc), exc.status, exc.code)

    return success(f"User {user['username']} created.", user, status=201)


@router.get("/users", summary="List users", dependencies=[Depends(require_local), Depends(require_role("admin"))])
def list_users():

    return success("Users retrieved.", {"users": users.list_users()})


@router.put(
    "/users/{username}", summary="Change a user's role or enabled state",
    dependencies=[Depends(require_local), Depends(require_role("admin"))],
    openapi_extra=documented_body(UpdateUserBody, required=False),
)
def update_user(username: str, data: dict = Depends(json_body)):
    """404 USER_NOT_FOUND; 400 INVALID_ROLE."""

    try:
        user = None
        if "role" in data:
            user = users.set_role(username, data["role"])
        if "disabled" in data:
            user = users.set_disabled(username, parse_bool(data["disabled"]))
    except users.UserError as exc:
        return error(str(exc), exc.status, exc.code)

    if user is None:
        return error("Send role and/or disabled.", 400, "NOTHING_TO_UPDATE")

    return success(f"User {username} updated.", user)


@router.delete(
    "/users/{username}", summary="Delete a user",
    dependencies=[Depends(require_local), Depends(require_role("admin"))],
)
def delete_user(username: str):
    """404 USER_NOT_FOUND. Also revokes every API token that user held."""

    try:
        users.delete_user(username)
    except users.UserError as exc:
        return error(str(exc), exc.status, exc.code)

    return success(f"User {username} deleted.")


# =========================================================
# API TOKENS (self-service: a logged-in user manages their own)
# =========================================================

@router.post(
    "/tokens", summary="Create an API token (for scripts)", status_code=201,
    dependencies=[Depends(require_login)], openapi_extra=documented_body(TokenBody, required=False),
)
def create_token(request: Request, data: dict = Depends(json_body)):
    """The plaintext token is shown exactly once, here, in `data.token`.
    Send it as `Authorization: Bearer <token>` afterwards; it is never
    retrievable again (only its hash is stored)."""

    user = current_user(request)
    record, token = users.create_api_token(user["id"], label=data.get("label"))

    return success("API token created. Store it now: it will not be shown again.",
                   {**record, "token": token}, status=201)


@router.get("/tokens", summary="List your API tokens", dependencies=[Depends(require_login)])
def list_tokens(request: Request):

    user = current_user(request)

    return success("API tokens retrieved.", {"tokens": users.list_api_tokens(user_id=user["id"])})


@router.delete("/tokens/{token_id}", summary="Revoke an API token", dependencies=[Depends(require_login)])
def revoke_token(token_id: int, request: Request):
    """404 TOKEN_NOT_FOUND (also if it exists but belongs to someone else)."""

    user = current_user(request)
    mine = {t["id"] for t in users.list_api_tokens(user_id=user["id"])}

    if token_id not in mine:
        return error(f"No API token with id {token_id}.", 404, "TOKEN_NOT_FOUND")

    return success("API token revoked.", users.revoke_api_token(token_id))
