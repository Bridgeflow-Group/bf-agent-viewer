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

    args = parser.parse_args(argv)
    args.func(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
