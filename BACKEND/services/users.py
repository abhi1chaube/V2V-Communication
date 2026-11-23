# =========================================================
# USERS (UPGRADE PHASE 6)
# =========================================================
#
# Stored in services/db.py's SQLite file, in `users` (salted
# password hashes, hashlib.scrypt from the standard library: no
# new dependency) and `api_tokens` (opaque bearer tokens for
# scripts, revocable; only a SHA-256 hash is ever stored).
#
# Low-frequency, needs-fresh-reads operations (login, token
# checks, user management), so this talks to SQLite directly
# with its own short-lived connections (NOT through db.py's
# async write queue, which is for high-frequency telemetry that
# can tolerate eventual persistence). A module lock serialises
# writers from this process; SQLite's own busy-timeout handles
# the rest against the db.py writer thread's connection.
#
# No default password and no default user: create the first
# admin from a terminal with
#   venv\Scripts\python.exe -m services.users create-admin
# which prompts for the username and password (hidden).
#
# Roles (least to most privileged): viewer, operator, admin.
# =========================================================

import argparse
import getpass
import hashlib
import hmac
import secrets
import sqlite3
import threading

from . import db
from .settings import settings
from .timeutil import utc_iso


ROLES = ("viewer", "operator", "admin")

ROLE_RANK = {role: index for index, role in enumerate(ROLES)}

USERNAME_MIN, USERNAME_MAX = 3, 40
PASSWORD_MIN = 8

_lock = threading.Lock()


class UserError(ValueError):

    def __init__(self, message, code, status=400):
        super().__init__(message)
        self.code = code
        self.status = status


# =========================================================
# PASSWORD HASHING (hashlib.scrypt, stdlib only)
# =========================================================

def hash_password(password):

    n = settings.auth_scrypt_n
    salt = secrets.token_bytes(16)
    derived = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=n, r=8, p=1, dklen=32)

    return f"scrypt${n}$8$1${salt.hex()}${derived.hex()}"


def verify_password(password, stored):

    try:
        algo, n, r, p, salt_hex, hash_hex = stored.split("$")
        if algo != "scrypt":
            return False
        derived = hashlib.scrypt(
            password.encode("utf-8"), salt=bytes.fromhex(salt_hex),
            n=int(n), r=int(r), p=int(p), dklen=len(hash_hex) // 2,
        )
        return hmac.compare_digest(derived.hex(), hash_hex)
    except (ValueError, TypeError):
        return False


# =========================================================
# CONNECTION
# =========================================================

def _connect():

    path = db.path()

    if path is None:
        raise UserError("The database is not configured yet.", "DB_NOT_READY", 503)

    conn = sqlite3.connect(str(path), timeout=5)
    conn.row_factory = sqlite3.Row
    return conn


# =========================================================
# VALIDATION
# =========================================================

def _validate_username(username):

    username = str(username or "").strip()

    if not (USERNAME_MIN <= len(username) <= USERNAME_MAX) or not all(
        c.isalnum() or c in "_-." for c in username
    ):
        raise UserError(
            f"username must be {USERNAME_MIN}-{USERNAME_MAX} letters, digits, _, - or ., {USERNAME_MIN} or more.",
            "INVALID_USERNAME",
        )

    return username


def _validate_password(password):

    if not isinstance(password, str) or len(password) < PASSWORD_MIN:
        raise UserError(f"password must be at least {PASSWORD_MIN} characters.", "INVALID_PASSWORD")


def _validate_role(role):

    role = str(role or "").strip().lower()

    if role not in ROLES:
        raise UserError(f"role must be one of: {', '.join(ROLES)}.", "INVALID_ROLE")

    return role


def at_least(role, minimum):
    """True if `role` has `minimum` or more privilege."""

    return ROLE_RANK.get(role, -1) >= ROLE_RANK.get(minimum, 99)


# =========================================================
# USER MANAGEMENT
# =========================================================

def create_user(username, password, role):

    username = _validate_username(username)
    _validate_password(password)
    role = _validate_role(role)

    with _lock, _connect() as conn:

        existing = conn.execute("SELECT 1 FROM users WHERE username = ?", (username,)).fetchone()

        if existing:
            raise UserError(f"A user named '{username}' already exists.", "USERNAME_TAKEN", 409)

        conn.execute(
            "INSERT INTO users (username, password_hash, role, created_at, disabled) "
            "VALUES (?, ?, ?, ?, 0)",
            (username, hash_password(password), role, utc_iso(milliseconds=True)),
        )
        conn.commit()

        return _row_to_public(conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone())


def _row_to_public(row):

    if row is None:
        return None

    return {
        "id": row["id"], "username": row["username"], "role": row["role"],
        "created_at": row["created_at"], "disabled": bool(row["disabled"]),
    }


def get_user(username):

    with _connect() as conn:
        row = conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()

    return _row_to_public(row)


def get_user_by_id(user_id):

    with _connect() as conn:
        row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()

    return _row_to_public(row)


def list_users():

    with _connect() as conn:
        rows = conn.execute("SELECT * FROM users ORDER BY id").fetchall()

    return [_row_to_public(r) for r in rows]


def any_user_exists():

    with _connect() as conn:
        return conn.execute("SELECT 1 FROM users LIMIT 1").fetchone() is not None


def set_role(username, role):

    role = _validate_role(role)

    with _lock, _connect() as conn:

        if not conn.execute("SELECT 1 FROM users WHERE username = ?", (username,)).fetchone():
            raise UserError(f"No user named '{username}'.", "USER_NOT_FOUND", 404)

        conn.execute("UPDATE users SET role = ? WHERE username = ?", (role, username))
        conn.commit()

        return _row_to_public(conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone())


def set_disabled(username, disabled):

    with _lock, _connect() as conn:

        if not conn.execute("SELECT 1 FROM users WHERE username = ?", (username,)).fetchone():
            raise UserError(f"No user named '{username}'.", "USER_NOT_FOUND", 404)

        conn.execute("UPDATE users SET disabled = ? WHERE username = ?", (1 if disabled else 0, username))
        conn.commit()

        return _row_to_public(conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone())


def delete_user(username):

    with _lock, _connect() as conn:

        if not conn.execute("SELECT 1 FROM users WHERE username = ?", (username,)).fetchone():
            raise UserError(f"No user named '{username}'.", "USER_NOT_FOUND", 404)

        row = conn.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone()
        conn.execute("DELETE FROM users WHERE username = ?", (username,))
        conn.execute("DELETE FROM api_tokens WHERE user_id = ?", (row["id"],))
        conn.commit()


# =========================================================
# LOGIN
# =========================================================

def authenticate(username, password):
    """The user's public record if username/password match and the
    account is not disabled, else None. Never raises for a bad login."""

    with _connect() as conn:
        row = conn.execute("SELECT * FROM users WHERE username = ?", (str(username or "").strip(),)).fetchone()

    if row is None or row["disabled"]:
        return None

    if not verify_password(password or "", row["password_hash"]):
        return None

    return _row_to_public(row)


# =========================================================
# API TOKENS (for scripts: Authorization: Bearer <token>, revocable)
# =========================================================

def _token_hash(token):

    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def create_api_token(user_id, label=None):
    """Returns (public record, the plaintext token — shown exactly once)."""

    token = secrets.token_urlsafe(32)

    with _lock, _connect() as conn:

        conn.execute(
            "INSERT INTO api_tokens (token_hash, user_id, label, created_at) VALUES (?, ?, ?, ?)",
            (_token_hash(token), user_id, label, utc_iso(milliseconds=True)),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM api_tokens WHERE token_hash = ?", (_token_hash(token),)).fetchone()

    return _token_row_to_public(row), token


def _token_row_to_public(row):

    if row is None:
        return None

    return {
        "id": row["id"], "user_id": row["user_id"], "label": row["label"],
        "created_at": row["created_at"], "revoked_at": row["revoked_at"],
    }


def list_api_tokens(user_id=None):

    with _connect() as conn:
        if user_id is None:
            rows = conn.execute("SELECT * FROM api_tokens ORDER BY id").fetchall()
        else:
            rows = conn.execute("SELECT * FROM api_tokens WHERE user_id = ? ORDER BY id", (user_id,)).fetchall()

    return [_token_row_to_public(r) for r in rows]


def revoke_api_token(token_id):

    with _lock, _connect() as conn:

        row = conn.execute("SELECT * FROM api_tokens WHERE id = ?", (token_id,)).fetchone()

        if row is None:
            raise UserError(f"No API token with id {token_id}.", "TOKEN_NOT_FOUND", 404)

        if row["revoked_at"] is None:
            conn.execute("UPDATE api_tokens SET revoked_at = ? WHERE id = ?",
                        (utc_iso(milliseconds=True), token_id))
            conn.commit()

        return _token_row_to_public(conn.execute("SELECT * FROM api_tokens WHERE id = ?", (token_id,)).fetchone())


def user_for_api_token(token):
    """The public user record for a valid, non-revoked API token, or None."""

    with _connect() as conn:
        row = conn.execute(
            "SELECT u.* FROM api_tokens t JOIN users u ON u.id = t.user_id "
            "WHERE t.token_hash = ? AND t.revoked_at IS NULL",
            (_token_hash(token),),
        ).fetchone()

    if row is None or row["disabled"]:
        return None

    return _row_to_public(row)


# =========================================================
# CLI: python -m services.users <command>
# =========================================================

def _cli():

    parser = argparse.ArgumentParser(description="V2V Fleet Control: user administration.")
    sub = parser.add_subparsers(dest="command", required=True)

    create = sub.add_parser("create-admin", help="Create the first admin account.")
    create.add_argument("--username")
    create.add_argument("--password")

    listing = sub.add_parser("list-users", help="List every user.")

    role_cmd = sub.add_parser("set-role", help="Change a user's role.")
    role_cmd.add_argument("username")
    role_cmd.add_argument("role", choices=ROLES)

    disable_cmd = sub.add_parser("disable-user", help="Disable a user (keeps the account, blocks login).")
    disable_cmd.add_argument("username")

    enable_cmd = sub.add_parser("enable-user", help="Re-enable a disabled user.")
    enable_cmd.add_argument("username")

    args = parser.parse_args()

    db.configure()

    if args.command == "create-admin":
        username = args.username or input("Username: ").strip()
        password = args.password or getpass.getpass("Password: ")
        confirm = password if args.password else getpass.getpass("Confirm password: ")
        if password != confirm:
            print("Passwords do not match.")
            raise SystemExit(1)
        try:
            user = create_user(username, password, "admin")
        except UserError as exc:
            print(f"Error: {exc}")
            raise SystemExit(1)
        print(f"Created admin '{user['username']}'.")

    elif args.command == "list-users":
        for user in list_users():
            print(f"{user['id']:>4}  {user['username']:<24} {user['role']:<10} "
                 f"{'disabled' if user['disabled'] else 'enabled'}  {user['created_at']}")

    elif args.command == "set-role":
        try:
            user = set_role(args.username, args.role)
        except UserError as exc:
            print(f"Error: {exc}")
            raise SystemExit(1)
        print(f"{user['username']} is now {user['role']}.")

    elif args.command in ("disable-user", "enable-user"):
        try:
            user = set_disabled(args.username, args.command == "disable-user")
        except UserError as exc:
            print(f"Error: {exc}")
            raise SystemExit(1)
        print(f"{user['username']} is now {'disabled' if user['disabled'] else 'enabled'}.")


if __name__ == "__main__":
    _cli()
