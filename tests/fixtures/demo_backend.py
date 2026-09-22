"""Test-only demo backend, standing in for a real customer MCP server."""
from fastmcp import FastMCP

mcp = FastMCP("demo-backend")


@mcp.tool
def get_weather(city: str) -> str:
    return f"{city}: 72F, clear"


@mcp.tool
def read_file(path: str) -> str:
    return f"[contents of {path}]"


@mcp.tool
def send_email(to: str, subject: str) -> str:
    return f"sent to {to}: {subject}"


@mcp.tool
def delete_record(record_id: str) -> str:
    return f"deleted {record_id}"


if __name__ == "__main__":
    mcp.run()
