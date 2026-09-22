"""A tiny sample MCP server so `docker compose up` has something real to
proxy on first run, without requiring you to bring your own backend
first. Not test infrastructure (tests/fixtures/demo_backend.py is a
separate, intentionally decoupled fixture) -- this one ships in the
image and is what BF_BACKEND_SCRIPT points at by default in
docker-compose.yml.

Swap it for your own MCP server by mounting yours over
/app/examples/demo_backend.py, or by overriding BF_BACKEND_SCRIPT to a
path you mount in instead -- see README.md's Quickstart.
"""
from fastmcp import FastMCP

mcp = FastMCP("bf-agent-viewer-demo-backend")


@mcp.tool
def get_weather(city: str) -> str:
    """A harmless read-only example tool."""
    return f"{city}: 68F, partly cloudy"


@mcp.tool
def send_email(to: str, subject: str) -> str:
    """A harmless example write action, for exercising --write-tools."""
    return f"(demo) would send to {to}: {subject}"


if __name__ == "__main__":
    mcp.run()
