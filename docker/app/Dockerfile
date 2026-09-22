# Single image for both the `gateway` and `console` services in
# docker-compose.yml (F-018/T-009) -- one package, two entrypoints,
# selected by `command:` per service. Not the sandboxed-backend image
# (that's docker/backend-sandbox/Dockerfile, a different thing entirely:
# this one runs the platform itself, that one is what the gateway spawns
# a downstream MCP server inside).
FROM python:3.11-slim

WORKDIR /app

COPY pyproject.toml README.md ./
COPY bf_agent_viewer ./bf_agent_viewer
COPY examples ./examples

# console extra pulls in starlette/jinja2/uvicorn/pyotp -- installed
# unconditionally here since this one image serves both services rather
# than maintaining two images for the sake of a few MB.
RUN pip install --no-cache-dir ".[console]" \
    && useradd --no-create-home --uid 10002 bfav \
    && mkdir -p /data && chown bfav:bfav /data

USER bfav
VOLUME ["/data"]

# No default CMD -- docker-compose.yml sets `command:` explicitly per
# service (gateway vs. console), same reasoning as
# docker/backend-sandbox/Dockerfile's own "no ENTRYPOINT" note: an
# implicit default here would be one more place behavior could silently
# drift from what's actually configured.
