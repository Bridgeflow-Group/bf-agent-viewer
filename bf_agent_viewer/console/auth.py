"""Console authentication (F-040): password + mandatory TOTP MFA, hardened
sessions.

Real primitives, not a stopgap -- the console shipped in the first v0.1.0
pass with no authentication at all (stated plainly in status.md/app.py
rather than left implicit), and per the project's own standing rule
("don't put in fast fixes in places where you can use a tool"), closing
that gap means actual auth, not a single shared password behind an env
var.

- Passwords: hashlib.scrypt (stdlib -- no new dependency for this part),
  per-user random salt, N=2**14/r=8/p=1 (interactive-login cost
  parameters per the scrypt paper's own recommendation), compared with
  hmac.compare_digest to avoid a timing side-channel.
- MFA: pyotp (RFC 6238 TOTP) -- the standard library for this rather than
  a hand-rolled HMAC-based one-time-password implementation. Added as an
  explicit new entry in the `console` extra (pyproject.toml), since it's
  the whole point of F-040, not incidental.
- Sessions: opaque random tokens (secrets.token_urlsafe), only a SHA-256
  hash of the token is ever persisted -- mirrors how bearer tokens work
  elsewhere in this project (identity/registry.py issue_token) -- fixed
  expiry, httponly + samesite=strict cookies.
- Brute-force resistance: failed password attempts are counted per human;
  five in a row locks that account out for 15 minutes. This is
  intentionally on the *password* check, not the TOTP check -- a correct
  password with five wrong TOTP codes just means "try the code again",
  not "the account is compromised", so it isn't penalized the same way
  (though a much higher raw attempt cap still applies via pending-login
  expiry: a pending login is only good for 5 minutes total).

MFA is not optional here: a freshly provisioned console user has
totp_enabled=0 and the login flow forces enrollment (see enroll page in
app.py) before any session is issued. There is no path that grants a
session with a password alone.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import pyotp

SESSION_TTL = timedelta(hours=12)
PENDING_LOGIN_TTL = timedelta(minutes=5)
MAX_FAILED_ATTEMPTS = 5
LOCKOUT_DURATION = timedelta(minutes=15)

_SCRYPT_N = 2**14
_SCRYPT_R = 8
_SCRYPT_P = 1
_SCRYPT_DKLEN = 32


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def hash_password(password: str, *, salt: bytes | None = None) -> tuple[str, str]:
    """Returns (password_hash_hex, salt_hex)."""
    salt = salt or secrets.token_bytes(16)
    derived = hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P,
        dklen=_SCRYPT_DKLEN,
    )
    return derived.hex(), salt.hex()


def _verify_password(password: str, salt_hex: str, expected_hash_hex: str) -> bool:
    derived_hex, _ = hash_password(password, salt=bytes.fromhex(salt_hex))
    return hmac.compare_digest(derived_hex, expected_hash_hex)


@dataclass(frozen=True)
class ProvisionResult:
    human_id: str
    totp_secret: str
    otpauth_uri: str


def provision_console_user(
    conn: sqlite3.Connection, *, human_id: str, password: str, account_label: str,
    issuer_name: str = "BF Agent Viewer",
) -> ProvisionResult:
    """Create (or replace) console login credentials for an existing human.
    totp_enabled starts at 0 -- see module docstring: this account cannot
    log in to a real session until the web login flow's enrollment step
    verifies a code from an authenticator app against this secret."""
    password_hash, salt = hash_password(password)
    totp_secret = pyotp.random_base32()
    now = _iso(_now())
    conn.execute(
        """INSERT INTO console_credentials
               (human_id, password_hash, password_salt, totp_secret, totp_enabled,
                failed_attempts, locked_until, created_at, updated_at)
           VALUES (?,?,?,?,0,0,NULL,?,?)
           ON CONFLICT(human_id) DO UPDATE SET
               password_hash=excluded.password_hash, password_salt=excluded.password_salt,
               totp_secret=excluded.totp_secret, totp_enabled=0,
               failed_attempts=0, locked_until=NULL, updated_at=excluded.updated_at""",
        (human_id, password_hash, salt, totp_secret, now, now),
    )
    conn.commit()
    uri = pyotp.totp.TOTP(totp_secret).provisioning_uri(name=account_label, issuer_name=issuer_name)
    return ProvisionResult(human_id, totp_secret, uri)


@dataclass(frozen=True)
class LoginResult:
    status: str  # "ok" | "invalid_credentials" | "locked" | "needs_enrollment"
    human_id: str | None = None
    pending_token: str | None = None  # set when status in {"ok", "needs_enrollment"}


def start_login(conn: sqlite3.Connection, *, email: str, password: str) -> LoginResult:
    """Step 1: password check. Never issues a real session -- only ever
    returns a short-lived pending-login token that still requires a valid
    TOTP code (start_enrollment_login for a first-time user, or
    complete_login otherwise) before any console access is granted."""
    row = conn.execute(
        """SELECT h.id, c.password_hash, c.password_salt, c.totp_enabled,
                  c.failed_attempts, c.locked_until
           FROM humans h JOIN console_credentials c ON c.human_id = h.id
           WHERE h.email = ?""",
        (email,),
    ).fetchone()
    if row is None:
        # No such account (or no console credentials provisioned for it).
        # Same generic result as a wrong password -- don't leak which.
        return LoginResult(status="invalid_credentials")

    human_id, password_hash, salt, totp_enabled, failed_attempts, locked_until = row
    if locked_until is not None and _parse_iso(locked_until) > _now():
        return LoginResult(status="locked", human_id=human_id)

    if not _verify_password(password, salt, password_hash):
        failed_attempts += 1
        lock_until_val = None
        if failed_attempts >= MAX_FAILED_ATTEMPTS:
            lock_until_val = _iso(_now() + LOCKOUT_DURATION)
            failed_attempts = 0  # lockout resets the counter for the next window
        conn.execute(
            "UPDATE console_credentials SET failed_attempts=?, locked_until=?, updated_at=? WHERE human_id=?",
            (failed_attempts, lock_until_val, _iso(_now()), human_id),
        )
        conn.commit()
        status = "locked" if lock_until_val else "invalid_credentials"
        return LoginResult(status=status, human_id=human_id)

    # Password correct: reset the failure counter and issue a pending
    # login token good only for entering (or enrolling) a TOTP code.
    conn.execute(
        "UPDATE console_credentials SET failed_attempts=0, locked_until=NULL, updated_at=? WHERE human_id=?",
        (_iso(_now()), human_id),
    )
    pending_token = secrets.token_urlsafe(32)
    conn.execute(
        "INSERT INTO console_pending_logins (token_hash, human_id, purpose, expires_at) VALUES (?,?,?,?)",
        (_hash_token(pending_token), human_id, "login", _iso(_now() + PENDING_LOGIN_TTL)),
    )
    conn.commit()
    status = "ok" if totp_enabled else "needs_enrollment"
    return LoginResult(status=status, human_id=human_id, pending_token=pending_token)


def _consume_pending_login(conn: sqlite3.Connection, pending_token: str) -> str | None:
    """Returns human_id and deletes the pending-login row if the token is
    valid and unexpired; None otherwise. One-shot by design -- a pending
    token is spent on its first use whether the TOTP code was right or
    wrong, so a leaked pending cookie can't be replayed indefinitely."""
    token_hash = _hash_token(pending_token)
    row = conn.execute(
        "SELECT human_id, expires_at FROM console_pending_logins WHERE token_hash = ?",
        (token_hash,),
    ).fetchone()
    conn.execute("DELETE FROM console_pending_logins WHERE token_hash = ?", (token_hash,))
    conn.commit()
    if row is None:
        return None
    human_id, expires_at = row
    if _parse_iso(expires_at) <= _now():
        return None
    return human_id


def peek_pending_login(conn: sqlite3.Connection, pending_token: str) -> str | None:
    """Non-destructive look at a pending-login token, for rendering the
    enroll/verify page on GET without spending the one-shot token before
    the user has even submitted a code."""
    if not pending_token:
        return None
    row = conn.execute(
        "SELECT human_id, expires_at FROM console_pending_logins WHERE token_hash = ?",
        (_hash_token(pending_token),),
    ).fetchone()
    if row is None:
        return None
    human_id, expires_at = row
    if _parse_iso(expires_at) <= _now():
        return None
    return human_id


def totp_provisioning_uri(conn: sqlite3.Connection, human_id: str, *, issuer_name: str = "BF Agent Viewer") -> tuple[str, str] | None:
    """Returns (secret, otpauth_uri) for an already-provisioned-but-not-yet-
    enrolled account, for display on the enroll page. Does not create or
    change anything."""
    row = conn.execute(
        "SELECT c.totp_secret, h.email FROM console_credentials c JOIN humans h ON h.id = c.human_id WHERE c.human_id = ?",
        (human_id,),
    ).fetchone()
    if row is None:
        return None
    secret, email = row
    uri = pyotp.totp.TOTP(secret).provisioning_uri(name=email or human_id, issuer_name=issuer_name)
    return secret, uri


def enroll_totp(conn: sqlite3.Connection, *, pending_token: str, code: str) -> str | None:
    """Step 2 for a first-time login: verify `code` against the secret
    already provisioned, and only if it's correct, flip totp_enabled=1
    and issue a real session. Returns a new session token, or None on any
    failure (unknown/expired pending token, or wrong code)."""
    human_id = _consume_pending_login(conn, pending_token)
    if human_id is None:
        return None
    row = conn.execute(
        "SELECT totp_secret FROM console_credentials WHERE human_id = ?", (human_id,)
    ).fetchone()
    if row is None or not pyotp.TOTP(row[0]).verify(code, valid_window=1):
        return None
    conn.execute(
        "UPDATE console_credentials SET totp_enabled=1, updated_at=? WHERE human_id=?",
        (_iso(_now()), human_id),
    )
    conn.commit()
    return create_session(conn, human_id)


def complete_login(conn: sqlite3.Connection, *, pending_token: str, code: str) -> str | None:
    """Step 2 for an already-enrolled user: verify `code`, issue a real
    session on success. Returns a new session token, or None."""
    human_id = _consume_pending_login(conn, pending_token)
    if human_id is None:
        return None
    row = conn.execute(
        "SELECT totp_secret FROM console_credentials WHERE human_id = ?", (human_id,)
    ).fetchone()
    if row is None or not pyotp.TOTP(row[0]).verify(code, valid_window=1):
        return None
    return create_session(conn, human_id)


def create_session(conn: sqlite3.Connection, human_id: str) -> str:
    token = secrets.token_urlsafe(32)
    now = _now()
    conn.execute(
        "INSERT INTO console_sessions (token_hash, human_id, created_at, expires_at, last_seen_at) VALUES (?,?,?,?,?)",
        (_hash_token(token), human_id, _iso(now), _iso(now + SESSION_TTL), _iso(now)),
    )
    conn.commit()
    return token


def resolve_session(conn: sqlite3.Connection, token: str) -> str | None:
    """Returns the human_id for a valid, unexpired session token, updating
    last_seen_at; None if the token is missing, unknown, or expired
    (an expired row is opportunistically cleaned up here rather than left
    for a separate sweep job -- v0.1.0 scope, fine at this table size)."""
    if not token:
        return None
    token_hash = _hash_token(token)
    row = conn.execute(
        "SELECT human_id, expires_at FROM console_sessions WHERE token_hash = ?", (token_hash,)
    ).fetchone()
    if row is None:
        return None
    human_id, expires_at = row
    if _parse_iso(expires_at) <= _now():
        conn.execute("DELETE FROM console_sessions WHERE token_hash = ?", (token_hash,))
        conn.commit()
        return None
    conn.execute(
        "UPDATE console_sessions SET last_seen_at = ? WHERE token_hash = ?",
        (_iso(_now()), token_hash),
    )
    conn.commit()
    return human_id


def destroy_session(conn: sqlite3.Connection, token: str) -> None:
    if not token:
        return
    conn.execute("DELETE FROM console_sessions WHERE token_hash = ?", (_hash_token(token),))
    conn.commit()
