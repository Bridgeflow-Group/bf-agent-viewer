"""Minimal CLI -- register an agent identity, issue its token, run the
gateway. No console/web UI yet (that's part of v0.1.0's remaining scope);
this is the real, scriptable path underneath it."""
from __future__ import annotations

import argparse
import sys

from bf_agent_viewer.db import connect
from bf_agent_viewer.identity import issue_token, register_identity


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

    p_register = sub.add_parser("register", help="Register a new agent identity and issue its token")
    p_register.add_argument("--db", required=True)
    p_register.add_argument("--org", required=True)
    p_register.add_argument("--agent-id", required=True)
    p_register.add_argument("--name", required=True)
    p_register.add_argument("--owner", required=True, help="human_id of the accountable owner")
    p_register.add_argument("--scope", nargs="+", required=True, help="tool names this identity may call")
    p_register.add_argument("--parent-identity", default=None)
    p_register.set_defaults(func=cmd_register)

    p_console = sub.add_parser("console", help="Run the read-only web console (F-001/002/003/004/005/008), login required (F-040)")
    p_console.add_argument("--db", required=True)
    p_console.add_argument("--org", default=None, help="Restrict the dashboard to one organization_id; omit to show all")
    p_console.add_argument("--host", default="127.0.0.1")
    p_console.add_argument("--port", type=int, default=8942)
    p_console.add_argument("--secure-cookies", action="store_true", help="Mark session cookies Secure; only correct when served behind TLS")
    p_console.set_defaults(func=cmd_console)

    p_console_user = sub.add_parser("console-user", help="Manage console login accounts (F-040)")
    console_user_sub = p_console_user.add_subparsers(dest="console_user_command", required=True)
    p_cu_create = console_user_sub.add_parser("create", help="Create or reset a console login for an existing human, and start TOTP enrollment")
    p_cu_create.add_argument("--db", required=True)
    p_cu_create.add_argument("--human", required=True, help="humans.id of the person this login belongs to")
    p_cu_create.add_argument("--password", default=None, help="Omit to be prompted (recommended -- avoids the password landing in shell history)")
    p_cu_create.set_defaults(func=cmd_console_user_create)

    args = parser.parse_args(argv)
    args.func(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
