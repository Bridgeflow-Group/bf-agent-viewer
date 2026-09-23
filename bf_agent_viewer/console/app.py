"""The v0.1.0 console (F-001/002/003/004/005/008), now with real
authentication (F-040): password + mandatory TOTP MFA, hardened sessions.
See bf_agent_viewer/console/auth.py for the mechanism -- this module just
wires it into routes and a login-required guard.

Built on Starlette + Jinja2 rather than adding FastAPI as a new
dependency -- both are already transitive dependencies of fastmcp
(confirmed before writing this), so this adds zero new *web-framework*
packages, which matters given the project's own stated low-infra-footprint
positioning (docs/positioning.md, F-025). auth.py does add one real new
dependency, pyotp -- justified because TOTP is literally what F-040 asks
for, not incidental scope creep.
"""
from __future__ import annotations

import functools
import sqlite3
from pathlib import Path
from typing import Awaitable, Callable

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response
from starlette.routing import Route
from starlette.templating import Jinja2Templates

from bf_agent_viewer.console import auth, queries

TEMPLATES_DIR = Path(__file__).parent / "templates"

SESSION_COOKIE = "bf_console_session"
PENDING_COOKIE = "bf_console_pending"


def build_console(
    conn: sqlite3.Connection, *, organization_id: str | None = None,
    secure_cookies: bool = False,
    online_threshold_seconds: float = queries.DEFAULT_ONLINE_THRESHOLD_SECONDS,
) -> Starlette:
    """secure_cookies=False by default so the console works over plain
    HTTP on localhost during local/dev use (the common case per
    status.md); set True when this is actually served behind TLS."""
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

    def _current_user(request: Request) -> tuple[str, str] | None:
        """Returns (human_id, name) if the session cookie is valid, else None."""
        token = request.cookies.get(SESSION_COOKIE, "")
        human_id = auth.resolve_session(conn, token)
        if human_id is None:
            return None
        row = conn.execute("SELECT name FROM humans WHERE id = ?", (human_id,)).fetchone()
        return human_id, (row[0] if row else human_id)

    def require_auth(
        handler: Callable[[Request, str, str], Awaitable[HTMLResponse]],
    ) -> Callable[[Request], Awaitable[Response]]:
        @functools.wraps(handler)
        async def wrapped(request: Request) -> Response:
            user = _current_user(request)
            if user is None:
                return RedirectResponse("/login", status_code=303)
            return await handler(request, user[0], user[1])
        return wrapped

    # -- Authenticated routes -----------------------------------------

    @require_auth
    async def dashboard(request: Request, human_id: str, user_name: str) -> HTMLResponse:
        if not queries.has_any_agents(conn):
            # F-001: onboarding. No console-driven registration wizard yet
            # (that would need a write path this console doesn't have) --
            # points at the CLI path that already exists and is tested.
            return templates.TemplateResponse(
                request, "onboarding.html", {"current_user_name": user_name},
            )
        agents = queries.list_agents(
            conn, organization_id=organization_id, online_threshold_seconds=online_threshold_seconds,
        )
        recent_events = queries.list_events(conn, limit=25)
        return templates.TemplateResponse(
            request, "dashboard.html",
            {"agents": agents, "recent_events": recent_events, "current_user_name": user_name},
        )

    @require_auth
    async def agent_detail(request: Request, human_id: str, user_name: str) -> HTMLResponse:
        agent_id = request.path_params["agent_id"]
        agent = queries.get_agent(conn, agent_id, online_threshold_seconds=online_threshold_seconds)
        if agent is None:
            return templates.TemplateResponse(
                request, "not_found.html",
                {"kind": "agent", "id": agent_id, "current_user_name": user_name},
                status_code=404,
            )
        events = queries.list_events(conn, agent_id=agent_id, limit=100)
        return templates.TemplateResponse(
            request, "agent_detail.html",
            {"agent": agent, "events": events, "current_user_name": user_name},
        )

    @require_auth
    async def event_detail(request: Request, human_id: str, user_name: str) -> HTMLResponse:
        event_id = request.path_params["event_id"]
        event = queries.get_event(conn, event_id)
        if event is None:
            return templates.TemplateResponse(
                request, "not_found.html",
                {"kind": "event", "id": event_id, "current_user_name": user_name},
                status_code=404,
            )
        return templates.TemplateResponse(
            request, "event_detail.html", {"event": event, "current_user_name": user_name},
        )

    @require_auth
    async def search_view(request: Request, human_id: str, user_name: str) -> HTMLResponse:
        q = request.query_params.get("q", "").strip()
        results = queries.search(conn, q) if q else {"agents": [], "events": []}
        return templates.TemplateResponse(
            request, "search.html", {"q": q, "results": results, "current_user_name": user_name},
        )

    async def logout(request: Request) -> Response:
        auth.destroy_session(conn, request.cookies.get(SESSION_COOKIE, ""))
        resp = RedirectResponse("/login", status_code=303)
        resp.delete_cookie(SESSION_COOKIE)
        return resp

    # -- Unauthenticated (login) routes --------------------------------

    async def login_get(request: Request) -> HTMLResponse:
        if _current_user(request) is not None:
            return RedirectResponse("/", status_code=303)
        return templates.TemplateResponse(request, "login.html", {})

    async def login_post(request: Request) -> Response:
        form = await request.form()
        result = auth.start_login(
            conn, email=str(form.get("email", "")), password=str(form.get("password", "")),
        )
        if result.status == "locked":
            return templates.TemplateResponse(
                request, "login.html",
                {"error": "Too many failed attempts. Try again in 15 minutes."},
                status_code=429,
            )
        if result.status == "invalid_credentials":
            return templates.TemplateResponse(
                request, "login.html", {"error": "Incorrect email or password."}, status_code=401,
            )
        # status in {"ok", "needs_enrollment"}: password was correct.
        next_path = "/login/verify" if result.status == "ok" else "/login/enroll"
        resp = RedirectResponse(next_path, status_code=303)
        resp.set_cookie(
            PENDING_COOKIE, result.pending_token, max_age=int(auth.PENDING_LOGIN_TTL.total_seconds()),
            httponly=True, samesite="strict", secure=secure_cookies,
        )
        return resp

    async def verify_get(request: Request) -> Response:
        pending_token = request.cookies.get(PENDING_COOKIE, "")
        if auth.peek_pending_login(conn, pending_token) is None:
            return RedirectResponse("/login", status_code=303)
        return templates.TemplateResponse(request, "verify.html", {})

    async def verify_post(request: Request) -> Response:
        pending_token = request.cookies.get(PENDING_COOKIE, "")
        form = await request.form()
        session_token = auth.complete_login(conn, pending_token=pending_token, code=str(form.get("code", "")))
        if session_token is None:
            resp = templates.TemplateResponse(
                request, "login.html",
                {"error": "That code didn't work or expired -- log in again to get a fresh one."},
                status_code=401,
            )
            resp.delete_cookie(PENDING_COOKIE)
            return resp
        resp = RedirectResponse("/", status_code=303)
        resp.delete_cookie(PENDING_COOKIE)
        resp.set_cookie(
            SESSION_COOKIE, session_token, max_age=int(auth.SESSION_TTL.total_seconds()),
            httponly=True, samesite="strict", secure=secure_cookies,
        )
        return resp

    async def enroll_get(request: Request) -> Response:
        pending_token = request.cookies.get(PENDING_COOKIE, "")
        human_id = auth.peek_pending_login(conn, pending_token)
        if human_id is None:
            return RedirectResponse("/login", status_code=303)
        info = auth.totp_provisioning_uri(conn, human_id)
        if info is None:
            return RedirectResponse("/login", status_code=303)
        secret, otpauth_uri = info
        return templates.TemplateResponse(
            request, "enroll.html", {"totp_secret": secret, "otpauth_uri": otpauth_uri},
        )

    async def enroll_post(request: Request) -> Response:
        pending_token = request.cookies.get(PENDING_COOKIE, "")
        form = await request.form()
        session_token = auth.enroll_totp(conn, pending_token=pending_token, code=str(form.get("code", "")))
        if session_token is None:
            resp = templates.TemplateResponse(
                request, "login.html",
                {"error": "That code didn't work or expired -- log in again to get a fresh one."},
                status_code=401,
            )
            resp.delete_cookie(PENDING_COOKIE)
            return resp
        resp = RedirectResponse("/", status_code=303)
        resp.delete_cookie(PENDING_COOKIE)
        resp.set_cookie(
            SESSION_COOKIE, session_token, max_age=int(auth.SESSION_TTL.total_seconds()),
            httponly=True, samesite="strict", secure=secure_cookies,
        )
        return resp

    return Starlette(routes=[
        Route("/", dashboard),
        Route("/agents/{agent_id}", agent_detail),
        Route("/events/{event_id}", event_detail),
        Route("/search", search_view),
        Route("/login", login_get, methods=["GET"]),
        Route("/login", login_post, methods=["POST"]),
        Route("/login/verify", verify_get, methods=["GET"]),
        Route("/login/verify", verify_post, methods=["POST"]),
        Route("/login/enroll", enroll_get, methods=["GET"]),
        Route("/login/enroll", enroll_post, methods=["POST"]),
        Route("/logout", logout, methods=["POST"]),
    ])
