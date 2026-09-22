"""CLI -- register an agent identity, issue its token, run the gateway,
run the console. The real, scriptable path underneath both services; the
Docker Compose packaging (F-018, docker/compose/) is a thin wrapper around
these same commands, not a separate deployment mechanism.

Env-var fallbacks (BF_DB, BF_ORG, etc.) exist alongside the CLI flags
specifically for that container use -- a docker-compose.yml sets
`environment:` once per service rather than overriding `command:`, and an
env var still loses to an explicit flag if both are given."""
from __future__ import annotations

import argparse
import os
import sys

from bf_agent_viewer.db import connect
from bf_agent_viewer.identity import issue_token, register_identity


def _env_default(var: str, fallback: str | None = None) -> str | None:
    return os.environ.get(var, fallback)


def cmd_org_create(args: argparse.Namespace) -> None:
    conn = connect(args.db)
    conn.execute(
        "INSERT INTO organizations (id, name) VALUES (?, ?)", (args.id, args.name),
    )
    conn.commit()
    print(f"organization {args.id!r} ({args.name}) created.")


def cmd_human_create(args: argparse.Namespace) -> None:
    conn = connect(args.db)
    row = conn.execute("SELECT id FROM organizations WHERE id = ?", (args.org,)).fetchone()
    if row is None:
        print(f"No organization {args.org!r} found. Create it first with `bf-agent-viewer org create`.")
        raise SystemExit(1)
    conn.execute(
        "INSERT INTO humans (id, organization_id, name, role, email) VALUES (?,?,?,?,?)",
        (args.id, args.org, args.name, args.role, args.email),
    )
    conn.commit()
    print(f"human {args.id!r} ({args.name}) created in organization {args.org!r}.")
    if args.email:
        print(f"Give them console access with: bf-agent-viewer console-user create --db {args.db} --human {args.id}")


def cmd_register(args: argparse.Namespace) -> None:
    conn = connect(args.db)
    identity = register_identity(
        conn,
        organization_id=args.org,
        agent_id=args.agent_id,
        agent_name=args.name,
        owner_human_id=args.owner,
        subject=args.agent_id,
        granted_scope=args.scope,
        parent_identity_id=args.parent_identity,
    )
    token = issue_token(conn, agent_id=identity.agent_id)
    print(f"registered {identity.agent_id} (identity {identity.identity_id})")
    print(f"token: {token}")
    print("Set this as the X-BF-Agent-Token header on the agent's gateway connections.")


def cmd_gateway(args: argparse.Namespace) -> None:
    from bf_agent_viewer.gateway import build_gateway
    from bf_agent_viewer.ratelimit import TokenBucketLimiter

    conn = connect(args.db)
    write_tools = {t.strip() for t in args.write_tools.split(",") if t.strip()}
    limiter = TokenBucketLimiter(rate=args.rate_limit, burst=args.rate_burst)
    gateway, _middleware = build_gateway(
        conn,
        backend_script=args.backend_script,
        organization_id=args.org,
        write_tools=write_tools,
        rate_limiter=limiter,
        prefer_container=not args.no_container,
    )
    if args.no_container:
        print("Backend sandboxing: rlimit-only (--no-container passed) -- filesystem/network isolation from the sandboxed container path is NOT in effect.")
    print(f"Gateway running at http://{args.host}:{args.port}/mcp -- proxying {args.backend_script}, write_tools={sorted(write_tools) or 'none'}")
    gateway.run(transport="http", host=args.host, port=args.port, show_banner=False)


def cmd_console(args: argparse.Namespace) -> None:
    # Imported here, not at module scope: keeps `bf-agent-viewer register`
    # (the scriptable path with no web dependencies at all) working even
    # in a stripped-down environment where the console's deps aren't
    # installed -- this is the only command that needs them.
    import uvicorn

    from bf_agent_viewer.console import build_console

    conn = connect(args.db, check_same_thread=False)
    app = build_console(conn, organization_id=args.org, secure_cookies=args.secure_cookies)
    if not args.secure_cookies:
        print("Session cookies are NOT marked Secure -- fine for localhost/plain HTTP dev use; pass --secure-cookies once this is served behind TLS.")
    print(f"Console running at http://{args.host}:{args.port} -- requires login (F-040: password + TOTP). Create the first login with `bf-agent-viewer console-user create`.")
    uvicorn.run(app, host=args.host, port=args.port)


def cmd_console_user_create(args: argparse.Namespace) -> None:
    import getpass

    from bf_agent_viewer.console import auth

    conn = connect(args.db)
    row = conn.execute("SELECT id, email FROM humans WHERE id = ?", (args.human,)).fetchone()
    if row is None:
        print(f"No human {args.human!r} found in this database. Register the human (or an agent that owns them) first.")
        raise SystemExit(1)
    human_id, email = row
    password = args.password or getpass.getpass("Console password: ")
    if not password:
        print("A password is required.")
        raise SystemExit(1)
    result = auth.provision_console_user(
        conn, human_id=human_id, password=password, account_label=email or human_id,
    )
    print(f"Console login created for {human_id} ({email or 'no email on file'}).")
    print()
    print("This account cannot log in yet -- MFA enrollment is mandatory and happens on first")
    print("login: add this to an authenticator app, then log in at the console and enter the")
    print("code you're shown next to finish setup.")
    print()
    print(f"Secret: {result.totp_secret}")
    print(f"URI:    {result.otpauth_uri}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="bf-agent-viewer")
    sub = parser.add_subparsers(dest="command", required=True)

    p_org = sub.add_parser("org", help="Manage organizations")
    org_sub = p_org.add_subparsers(dest="org_command", required=True)
    p_org_create = org_sub.add_parser("create", help="Create an organization (the first thing a fresh database needs -- humans and agents both reference one)")
    p_org_create.add_argument("--db", default=_env_default("BF_DB"), required=_env_default("BF_DB") is None)
    p_org_create.add_argument("--id", required=True)
    p_org_create.add_argument("--name", required=True)
    p_org_create.set_defaults(func=cmd_org_create)

    p_human = sub.add_parser("human", help="Manage humans (agent owners, console users)")
    human_sub = p_human.add_subparsers(dest="human_command", required=True)
    p_human_create = human_sub.add_parser("create", help="Create a human record -- needed before registering an agent they own, or provisioning their console login")
    p_human_create.add_argument("--db", default=_env_default("BF_DB"), required=_env_default("BF_DB") is None)
    p_human_create.add_argument("--org", default=_env_default("BF_ORG"), required=_env_default("BF_ORG") is None)
    p_human_create.add_argument("--id", required=True)
    p_human_create.add_argument("--name", required=True)
    p_human_create.add_argument("--email", default=None, help="Needed if this human will log into the console (F-040)")
    p_human_create.add_argument("--role", default=None)
    p_human_create.set_defaults(func=cmd_human_create)

    p_register = sub.add_parser("register", help="Register a new agent identity and issue its token")
    p_register.add_argument("--db", default=_env_default("BF_DB"), required=_env_default("BF_DB") is None)
    p_register.add_argument("--org", default=_env_default("BF_ORG"), required=_env_default("BF_ORG") is None)
    p_register.add_argument("--agent-id", required=True)
    p_register.add_argument("--name", required=True)
    p_register.add_argument("--owner", required=True, help="human_id of the accountable owner")
    p_register.add_argument("--scope", nargs="+", required=True, help="tool names this identity may call")
    p_register.add_argument("--parent-identity", default=None)
    p_register.set_defaults(func=cmd_register)

    p_gateway = sub.add_parser("gateway", help="Run the gateway: proxies to a backend MCP server with identity resolution, scope enforcement, rate limiting, tamper-evident logging, and sandboxed backend spawning")
    p_gateway.add_argument("--db", default=_env_default("BF_DB"), required=_env_default("BF_DB") is None)
    p_gateway.add_argument("--org", default=_env_default("BF_ORG"), required=_env_default("BF_ORG") is None)
    p_gateway.add_argument("--backend-script", default=_env_default("BF_BACKEND_SCRIPT"), required=_env_default("BF_BACKEND_SCRIPT") is None, help="Path to the backend MCP server script this gateway proxies to")
    p_gateway.add_argument("--write-tools", default=_env_default("BF_WRITE_TOOLS", ""), help="Comma-separated tool names treated as write actions (BF_WRITE_TOOLS)")
    p_gateway.add_argument("--host", default=_env_default("BF_GATEWAY_HOST", "127.0.0.1"))
    p_gateway.add_argument("--port", type=int, default=int(_env_default("BF_GATEWAY_PORT", "8941")))
    p_gateway.add_argument("--rate-limit", type=float, default=float(_env_default("BF_RATE_LIMIT", "5.0")), help="Steady tokens/sec per agent identity")
    p_gateway.add_argument("--rate-burst", type=float, default=float(_env_default("BF_RATE_BURST", "20.0")))
    p_gateway.add_argument("--no-container", action="store_true", default=_env_default("BF_NO_CONTAINER", "") not in ("", "0", "false", "False"), help="Force the rlimit-only sandbox path even if Docker is available (BF_NO_CONTAINER) -- the compose deployment sets this; see docker-compose.yml for why")
    p_gateway.set_defaults(func=cmd_gateway)

    p_console = sub.add_parser("console", help="Run the read-only web console (F-001/002/003/004/005/008), login required (F-040)")
    p_console.add_argument("--db", default=_env_default("BF_DB"), required=_env_default("BF_DB") is None)
    p_console.add_argument("--org", default=_env_default("BF_ORG"), help="Restrict the dashboard to one organization_id; omit to show all")
    p_console.add_argument("--host", default=_env_default("BF_CONSOLE_HOST", "127.0.0.1"))
    p_console.add_argument("--port", type=int, default=int(_env_default("BF_CONSOLE_PORT", "8942")))
    p_console.add_argument("--secure-cookies", action="store_true", default=_env_default("BF_SECURE_COOKIES", "") not in ("", "0", "false", "False"), help="Mark session cookies Secure; only correct when served behind TLS (BF_SECURE_COOKIES)")
    p_console.set_defaults(func=cmd_console)

    p_console_user = sub.add_parser("console-user", help="Manage console login accounts (F-040)")
    console_user_sub = p_console_user.add_subparsers(dest="console_user_command", required=True)
    p_cu_create = console_user_sub.add_parser("create", help="Create or reset a console login for an existing human, and start TOTP enrollment")
    p_cu_create.add_argument("--db", default=_env_default("BF_DB"), required=_env_default("BF_DB") is None)
    p_cu_create.add_argument("--human", required=True, help="humans.id of the person this login belongs to")
    p_cu_create.add_argument("--password", default=None, help="Omit to be prompted (recommended -- avoids the password landing in shell history)")
    p_cu_create.set_defaults(func=cmd_console_user_create)

    args = parser.parse_args(argv)
    args.func(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
