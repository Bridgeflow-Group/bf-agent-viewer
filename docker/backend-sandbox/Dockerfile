# Runtime image for sandboxed MCP backend processes (ISS-017 / F-044).
#
# This is the actual container the real gateway spawns backend scripts
# in when Docker is available, giving them real filesystem and network
# isolation via the container boundary itself -- not something bolted on
# from the host side with rlimits/env-stripping alone. Those (see
# gateway/sandbox.py's sandboxed_stdio_args) stay in place as a
# documented fallback for hosts without a container runtime, but this
# image plus the --network=none / --read-only / --cap-drop=ALL flags in
# gateway/sandbox.py's dockerized_stdio_args() are the real answer to the
# "no filesystem/network isolation yet" half of ISS-017.
FROM python:3.11-slim

RUN pip install --no-cache-dir fastmcp>=4.0.5 mcp>=2.2 \
    && useradd --no-create-home --uid 10001 backend

WORKDIR /backend
USER backend

# No ENTRYPOINT: gateway/sandbox.py's dockerized_stdio_args() always
# supplies the full "python3 /backend/script.py" command explicitly, so
# this image and the locally-built fallback image used in tests/dev
# environments without registry access behave identically.
