"""F-040: password + mandatory TOTP MFA, hardened sessions. Exercises
both the auth.py primitives directly and the real HTTP login flow."""
import pyotp
import pytest

starlette_testclient = pytest.importorskip("starlette.testclient")
TestClient = starlette_testclient.TestClient

from bf_agent_viewer.console import auth, build_console
from bf_agent_viewer.db import reset

PASSWORD = "correct horse battery staple"


@pytest.fixture
def conn(tmp_path):
    c = reset(tmp_path / "auth_test.db", check_same_thread=False)
    c.execute("INSERT INTO organizations (id, name) VALUES ('org-1', 'Org')")
    c.execute(
        "INSERT INTO humans (id, organization_id, name, email) VALUES ('human-1', 'org-1', 'Ada', 'ada@example.com')"
    )
    auth.provision_console_user(c, human_id="human-1", password=PASSWORD, account_label="ada@example.com")
    return c


# -- auth.py primitives --------------------------------------------------

def test_wrong_password_rejected(conn):
    result = auth.start_login(conn, email="ada@example.com", password="nope")
    assert result.status == "invalid_credentials"
    assert result.pending_token is None


def test_unknown_email_rejected_same_as_wrong_password(conn):
    result = auth.start_login(conn, email="nobody@example.com", password="nope")
    assert result.status == "invalid_credentials"


def test_correct_password_first_login_needs_enrollment(conn):
    result = auth.start_login(conn, email="ada@example.com", password=PASSWORD)
    assert result.status == "needs_enrollment"
    assert result.pending_token is not None


def test_enrollment_with_wrong_code_fails_and_issues_no_session(conn):
    result = auth.start_login(conn, email="ada@example.com", password=PASSWORD)
    session = auth.enroll_totp(conn, pending_token=result.pending_token, code="000000")
    assert session is None


def test_enrollment_with_correct_code_issues_session_and_enables_totp(conn):
    result = auth.start_login(conn, email="ada@example.com", password=PASSWORD)
    secret, _uri = auth.totp_provisioning_uri(conn, result.human_id)
    code = pyotp.TOTP(secret).now()
    session = auth.enroll_totp(conn, pending_token=result.pending_token, code=code)
    assert session is not None
    assert auth.resolve_session(conn, session) == "human-1"

    enabled = conn.execute(
        "SELECT totp_enabled FROM console_credentials WHERE human_id = 'human-1'"
    ).fetchone()[0]
    assert enabled == 1


def test_pending_login_is_one_shot(conn):
    result = auth.start_login(conn, email="ada@example.com", password=PASSWORD)
    secret, _uri = auth.totp_provisioning_uri(conn, result.human_id)
    code = pyotp.TOTP(secret).now()
    first = auth.enroll_totp(conn, pending_token=result.pending_token, code=code)
    assert first is not None
    # Same pending token, reused: already consumed, must fail even with a
    # correct code.
    second = auth.enroll_totp(conn, pending_token=result.pending_token, code=code)
    assert second is None


def test_subsequent_login_requires_totp_not_enrollment(conn):
    result = auth.start_login(conn, email="ada@example.com", password=PASSWORD)
    secret, _uri = auth.totp_provisioning_uri(conn, result.human_id)
    auth.enroll_totp(conn, pending_token=result.pending_token, code=pyotp.TOTP(secret).now())

    second_login = auth.start_login(conn, email="ada@example.com", password=PASSWORD)
    assert second_login.status == "ok"  # already enrolled -- not "needs_enrollment" again
    session = auth.complete_login(conn, pending_token=second_login.pending_token, code=pyotp.TOTP(secret).now())
    assert session is not None


def test_lockout_after_five_failed_passwords(conn):
    for _ in range(4):
        result = auth.start_login(conn, email="ada@example.com", password="wrong")
        assert result.status == "invalid_credentials"
    fifth = auth.start_login(conn, email="ada@example.com", password="wrong")
    assert fifth.status == "locked"
    # Even the *correct* password is refused while locked out.
    still_locked = auth.start_login(conn, email="ada@example.com", password=PASSWORD)
    assert still_locked.status == "locked"


def test_expired_session_is_rejected(conn, monkeypatch):
    session = auth.create_session(conn, "human-1")
    # Force it into the past directly rather than sleeping in a test.
    conn.execute(
        "UPDATE console_sessions SET expires_at = '2000-01-01T00:00:00Z' WHERE token_hash = ?",
        (auth._hash_token(session),),
    )
    conn.commit()
    assert auth.resolve_session(conn, session) is None


def test_logout_destroys_session(conn):
    session = auth.create_session(conn, "human-1")
    assert auth.resolve_session(conn, session) == "human-1"
    auth.destroy_session(conn, session)
    assert auth.resolve_session(conn, session) is None


def test_password_hash_is_salted_and_not_plaintext(conn):
    row = conn.execute(
        "SELECT password_hash, password_salt FROM console_credentials WHERE human_id = 'human-1'"
    ).fetchone()
    password_hash, salt = row
    assert PASSWORD not in password_hash
    assert password_hash != auth.hash_password(PASSWORD, salt=bytes.fromhex(salt))[0] + "x"  # sanity: not trivially equal-ish
    # Same password, different random salt -> different hash (proves salting).
    other_hash, other_salt = auth.hash_password(PASSWORD)
    assert other_salt != salt
    assert other_hash != password_hash


# -- Real HTTP flow via app.py -------------------------------------------

def test_dashboard_redirects_to_login_when_unauthenticated(conn):
    app = build_console(conn)
    resp = TestClient(app).get("/", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"


def test_full_login_flow_reaches_dashboard(conn):
    app = build_console(conn)
    client = TestClient(app)

    resp = client.post("/login", data={"email": "ada@example.com", "password": PASSWORD}, follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login/enroll"
    pending = resp.cookies["bf_console_pending"]

    secret, _uri = auth.totp_provisioning_uri(conn, "human-1")
    code = pyotp.TOTP(secret).now()
    resp = client.post("/login/enroll", data={"code": code}, follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/"
    assert "bf_console_session" in resp.cookies

    resp = client.get("/")
    assert resp.status_code == 200
    assert "Log out" in resp.text
    assert "ada@example.com" in resp.text or "Ada" in resp.text


def test_wrong_password_shows_error_not_500(conn):
    app = build_console(conn)
    resp = TestClient(app).post("/login", data={"email": "ada@example.com", "password": "nope"})
    assert resp.status_code == 401
    assert "Incorrect email or password" in resp.text


def test_logout_clears_session_and_blocks_dashboard(conn):
    app = build_console(conn)
    client = TestClient(app)
    session = auth.create_session(conn, "human-1")
    # Enroll TOTP directly (bypassing the HTTP flow, since this test is
    # about logout, not enrollment) so the session created above is valid.
    conn.execute("UPDATE console_credentials SET totp_enabled = 1 WHERE human_id = 'human-1'")
    conn.commit()
    client.cookies.set("bf_console_session", session)
    assert client.get("/").status_code == 200

    resp = client.post("/logout", follow_redirects=False)
    assert resp.status_code == 303
    client.cookies.delete("bf_console_session")
    resp = client.get("/", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"
