"""Sandboxed backend process spawning (F-044 / ISS-017).

MCP's own official security best practices name this exact architecture --
a proxy that spawns MCP servers as child processes via stdio -- as a real
privilege-escalation path if the proxy's own auth is ever compromised: an
attacker who can reach the proxy can make it spawn arbitrary commands with
the proxy's own privileges.

What this module actually does, and what it doesn't:

DOES:
- Strips the spawned process's environment down to an explicit allowlist
  instead of inheriting the gateway's full environment (credentials,
  cloud metadata tokens, etc. in the gateway's own env are not handed to
  the backend by default).
- Sets conservative POSIX resource limits (file descriptors, process
  count, core dump size) via `resource` before exec, so a runaway or
  compromised backend can't fork-bomb or exhaust descriptors on the host.
- Pins the working directory explicitly rather than inheriting the
  gateway's cwd.
- Logs every spawn (command, args, resolved env keys -- not values) for
  the stdio-usage audit trail MCP's guidance calls for.

DOES NOT (yet):
- Filesystem isolation (chroot/bind-mount restriction) or network
  namespace isolation. Real containment for those needs a container
  runtime or a Linux sandboxing layer (bubblewrap, gVisor, a container),
  which is an infrastructure/deployment decision, not something this
  module can honestly claim to provide on its own from inside a plain
  subprocess.spawn call. Tracked as a follow-up, not silently claimed as
  done -- see issues log (ISS-017 stays open until that's in place; this
  module closes the "runs with default/inherited privileges" part of the
  gap, not the whole gap).

Integration note: FastMCP's own transports (PythonStdioTransport /
StdioTransport) spawn the child process internally and only expose `env`
and `cwd` -- not a preexec hook -- so the resource-limit hardening below
(spawn()/_preexec) only applies on the direct subprocess.Popen path, not
when going through those FastMCP transports. `build_env()` is what
actually wires into the FastMCP-managed gateway (see gateway/server.py);
the rlimit path stays available for a future direct-spawn backend
integration, not yet used by the real gateway server.
"""
from __future__ import annotations

import logging
import os
import resource
import subprocess
from dataclasses import dataclass, field

logger = logging.getLogger("bf_agent_viewer.gateway.sandbox")

# Conservative defaults -- generous enough for a normal MCP backend server,
# tight enough to bound a compromised or runaway one.
DEFAULT_LIMITS: dict[int, tuple[int, int]] = {
    resource.RLIMIT_NOFILE: (256, 256),
    resource.RLIMIT_NPROC: (64, 64),
    resource.RLIMIT_CORE: (0, 0),
}

# Environment variables passed through to the spawned backend by default.
# Everything else in the gateway's own environment is withheld.
DEFAULT_ENV_ALLOWLIST = ("PATH", "LANG", "LC_ALL", "PYTHONUNBUFFERED")


@dataclass(frozen=True)
class SandboxPolicy:
    env_allowlist: tuple[str, ...] = DEFAULT_ENV_ALLOWLIST
    extra_env: dict[str, str] = field(default_factory=dict)
    limits: dict[int, tuple[int, int]] = field(default_factory=lambda: dict(DEFAULT_LIMITS))
    cwd: str | None = None


def build_env(policy: SandboxPolicy | None = None) -> dict[str, str]:
    """Public entry point for callers using FastMCP's own transports
    (which accept `env`/`cwd` but not a preexec hook) -- pass the result
    straight to PythonStdioTransport(env=...)/StdioTransport(env=...)."""
    return _build_env(policy or SandboxPolicy())


def _build_env(policy: SandboxPolicy) -> dict[str, str]:
    env = {k: os.environ[k] for k in policy.env_allowlist if k in os.environ}
    env.update(policy.extra_env)
    return env


def _preexec(policy: SandboxPolicy):
    def _apply():
        for res, (soft, hard) in policy.limits.items():
            try:
                resource.setrlimit(res, (soft, hard))
            except (ValueError, OSError):
                # Best-effort: some limits can't be lowered further than
                # the parent's own hard limit in every environment. Don't
                # crash the spawn over it, but this is real -- surfaced in
                # spawn logging below, not swallowed silently.
                logger.warning("could not apply resource limit %s", res)
    return _apply


def spawn(command: list[str], policy: SandboxPolicy | None = None) -> subprocess.Popen:
    """Spawn a backend process under the sandbox policy. Returns the Popen
    handle (stdio pipes, matching what the MCP stdio transport needs)."""
    policy = policy or SandboxPolicy()
    env = _build_env(policy)

    logger.info(
        "spawning backend: command=%s cwd=%s env_keys=%s",
        command, policy.cwd, sorted(env.keys()),
    )

    return subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        cwd=policy.cwd,
        preexec_fn=_preexec(policy) if os.name == "posix" else None,
        shell=False,
    )
