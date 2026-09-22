"""The v0.1.0 console (F-001/002/003/004/005/008): a server-rendered,
read-only web UI over the same SQLite database the gateway writes to.

Built on Starlette + Jinja2 rather than adding FastAPI as a new
dependency -- both are already transitive dependencies of fastmcp
(confirmed before writing this), so this adds zero new packages, which
matters given the project's own stated low-infra-footprint positioning
(docs/positioning.md, F-025): a console that needed its own extra
dependency, let alone a Node/JS build step, would undercut that.

SECURITY NOTE, stated plainly rather than left implicit: this console
has NO AUTHENTICATION in this pass. F-040 (TOTP MFA + hardened sessions)
is a separate, not-yet-built feature -- shipping this without flagging
that would be exactly the kind of silent gap this project's own
standing practice is against. Anyone who can reach this process can
read everything in it. Fine for local/dev use; not fine to expose
without F-040 first. See docs/security.md and features.xlsx (F-040).
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse
from starlette.routing import Route
from starlette.templating import Jinja2Templates

from bf_agent_viewer.console import queries

TEMPLATES_DIR = Path(__file__).parent / "templates"


def build_console(conn: sqlite3.Connection, *, organization_id: str | None = None) -> Starlette:
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

    async def dashboard(request: Request) -> HTMLResponse:
        if not queries.has_any_agents(conn):
            # F-001: onboarding. No console-driven registration wizard yet
            # (that would need a write path this console doesn't have) --
            # points at the CLI path that already exists and is tested.
            return templates.TemplateResponse(request, "onboarding.html", {})
        agents = queries.list_agents(conn, organization_id=organization_id)
        recent_events = queries.list_events(conn, limit=25)
        return templates.TemplateResponse(
            request, "dashboard.html", {"agents": agents, "recent_events": recent_events},
        )

    async def agent_detail(request: Request) -> HTMLResponse:
        agent_id = request.path_params["agent_id"]
        agent = queries.get_agent(conn, agent_id)
        if agent is None:
            return templates.TemplateResponse(
                request, "not_found.html", {"kind": "agent", "id": agent_id}, status_code=404,
            )
        events = queries.list_events(conn, agent_id=agent_id, limit=100)
        return templates.TemplateResponse(
            request, "agent_detail.html", {"agent": agent, "events": events},
        )

    async def event_detail(request: Request) -> HTMLResponse:
        event_id = request.path_params["event_id"]
        event = queries.get_event(conn, event_id)
        if event is None:
            return templates.TemplateResponse(
                request, "not_found.html", {"kind": "event", "id": event_id}, status_code=404,
            )
        return templates.TemplateResponse(request, "event_detail.html", {"event": event})

    async def search_view(request: Request) -> HTMLResponse:
        q = request.query_params.get("q", "").strip()
        results = queries.search(conn, q) if q else {"agents": [], "events": []}
        return templates.TemplateResponse(
            request, "search.html", {"q": q, "results": results},
        )

    return Starlette(routes=[
        Route("/", dashboard),
        Route("/agents/{agent_id}", agent_detail),
        Route("/events/{event_id}", event_detail),
        Route("/search", search_view),
    ])
