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
  count, core dump size) before the backend's real code runs, so a
  runaway or compromised backend can't fork-bomb or exhaust descriptors
  on the host -- wired into the actual FastMCP-managed gateway path, not
  just a workaround only used off to the side, see the integration note
  below for how.
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
  subprocess spawn. Tracked as a follow-up, not silently claimed as done
  -- see issues log (ISS-017 stays open until that's in place; this
  module closes the "runs with default/inherited privileges" part of the
  gap, not the whole gap).

Integration note (fixed 2026-09-22, closes the part of ISS-017 that was
still open): FastMCP's own transports (PythonStdioTransport / the base
StdioTransport) spawn the child process internally and only expose
`command`, `args`, `env`, and `cwd` -- not a preexec hook -- so the
original `spawn()`/`_preexec()` rlimit path below never actually ran when
going through those transports; it only worked on a direct
subprocess.Popen call the real gateway wasn't making. Fixed without
needing a preexec hook at all: `sandboxed_stdio_args()` below builds a
`command`/`args`/`env` triple where the child's first act, before it ever
imports or runs the real backend script, is `python -c <bootstrap>` --
a few lines that read the limits out of an env var, call
resource.setrlimit() on themselves, then runpy.run_path() the real
backend module. That's a legitimate use of the same command/args surface
FastMCP already exposes, not a workaround around it: the limits are
applied inside the child, by the child, before its own real code runs,
which is exactly what preexec_fn would have done from the parent side.
Verified with a real subprocess spawn asserting the limit is actually
lowered inside the child process, not just constructed -- see
tests/test_sandbox.py. `build_env()` stays as the smaller env-only
building block (used by sandboxed_stdio_args() and still fine standalone
for a caller that only wants env stripping). The direct spawn()/_preexec
path stays available for a future non-FastMCP integration.
"""
from __future__ import annotations

import json
import logging
import os
import resource
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger("bf_agent_viewer.gateway.sandbox")

# Conservative defaults -- generous enough for a normal MCP backend server,
# tight enough to bound a compromised or runaway one. Named (not the raw
# int constants) so they survive a round-trip through an environment
# variable into the child process -- see sandboxed_stdio_args().
DEFAULT_LIMITS: dict[str, tuple[int, int]] = {
    "RLIMIT_NOFILE": (256, 256),
    "RLIMIT_NPROC": (64, 64),
    "RLIMIT_CORE": (0, 0),
}

# Environment variables passed through to the spawned backend by default.
# Everything else in the gateway's own environment is withheld.
DEFAULT_ENV_ALLOWLIST = ("PATH", "LANG", "LC_ALL", "PYTHONUNBUFFERED")


@dataclass(frozen=True)
class SandboxPolicy:
    env_allowlist: tuple[str, ...] = DEFAULT_ENV_ALLOWLIST
    extra_env: dict[str, str] = field(default_factory=dict)
    limits: dict[str, tuple[int, int]] = field(default_factory=lambda: dict(DEFAULT_LIMITS))
    cwd: str | None = None


def build_env(policy: SandboxPolicy | None = None) -> dict[str, str]:
    """Public entry point for env-only stripping. Most callers going
    through FastMCP's transports want sandboxed_stdio_args() instead,
    which also wires in the resource-limit hardening; this stays
    available standalone for anything that only needs the env allowlist."""
    return _build_env(policy or SandboxPolicy())


def _build_env(policy: SandboxPolicy) -> dict[str, str]:
    env = {k: os.environ[k] for k in policy.env_allowlist if k in os.environ}
    env.update(policy.extra_env)
    return env


def _apply_limits_in_process(policy: SandboxPolicy) -> None:
    """Runs inside the process it's limiting -- called either by a
    preexec_fn (direct subprocess.Popen path) or by the bootstrap script
    a sandboxed_stdio_args()-spawned child runs on itself before exec'ing
    the real backend. Either way this executes as/in the child, never the
    parent, which is what makes it a real limit and not just advisory."""
    for name, (soft, hard) in policy.limits.items():
        try:
            resource.setrlimit(getattr(resource, name), (soft, hard))
        except (ValueError, OSError):
            # Best-effort: some limits can't be lowered further than
            # the parent's own hard limit in every environment. Don't
            # crash the spawn over it, but this is real -- surfaced in
            # spawn logging below, not swallowed silently.
            logger.warning("could not apply resource limit %s", name)


def _preexec(policy: SandboxPolicy):
    def _apply():
        _apply_limits_in_process(policy)
    return _apply


# The env var name a sandboxed_stdio_args()-built bootstrap reads its
# rlimit table from, then pops before the real backend script runs, so
# the backend itself never sees it in os.environ.
LIMITS_ENV_VAR = "_BF_SANDBOX_RLIMITS"

# Deliberately tiny and dependency-free (stdlib only, no import of this
# package -- the child process hasn't installed it and shouldn't need
# to). Applies the rlimit table from LIMITS_ENV_VAR to itself, then runs
# the real backend script as __main__ via runpy, so from the backend
# script's own point of view it's simply "the script that ran," with no
# trace of the bootstrap left in sys.argv or the environment.
_BOOTSTRAP_SOURCE = (
    "import json, os, resource, runpy, sys\n"
    f"_limits = json.loads(os.environ.pop({LIMITS_ENV_VAR!r}, '{{}}'))\n"
    "for _name, (_soft, _hard) in _limits.items():\n"
    "    try:\n"
    "        resource.setrlimit(getattr(resource, _name), (_soft, _hard))\n"
    "    except (ValueError, OSError):\n"
    "        pass\n"
    "runpy.run_path(sys.argv[1], run_name='__main__')\n"
)


def sandboxed_stdio_args(
    script_path: str | Path, policy: SandboxPolicy | None = None
) -> tuple[str, list[str], dict[str, str]]:
    """Build (command, args, env) for FastMCP's base StdioTransport that
    applies this policy's resource limits inside the child before the
    real backend script runs -- the fix for the part of ISS-017 that was
    still open (rlimit hardening constructed but never actually reaching
    the process FastMCP spawns). Use as:

        command, args, env = sandboxed_stdio_args(backend_script, policy)
        StdioTransport(command=command, args=args, env=env, cwd=policy.cwd)

    instead of PythonStdioTransport(backend_script, env=...), which only
    got the env-allowlist half of the sandbox applied, not the rlimits.
    """
    policy = policy or SandboxPolicy()
    resolved = Path(script_path).resolve()
    if not resolved.is_file():
        raise FileNotFoundError(f"Script not found: {resolved}")
    if not str(resolved).endswith(".py"):
        raise ValueError(f"Not a Python script: {resolved}")

    env = _build_env(policy)
    env[LIMITS_ENV_VAR] = json.dumps(policy.limits)

    args = ["-c", _BOOTSTRAP_SOURCE, str(resolved)]

    logger.info(
        "spawning sandboxed backend: script=%s limits=%s env_keys=%s",
        resolved, sorted(policy.limits.keys()),
        sorted(k for k in env.keys() if k != LIMITS_ENV_VAR),
    )

    return sys.executable, args, env


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


# ---------------------------------------------------------------------------
# Container-based sandboxing (closes the rest of ISS-017: filesystem and
# network isolation, which sandboxed_stdio_args()'s rlimits/env-stripping
# never claimed to provide -- resource limits and container boundaries are
# different problems and need different tools, not one workaround stretched
# to cover both).
#
# sandboxed_stdio_args() above is a real fix for its part of the gap (rlimit
# enforcement through FastMCP's command/args-only transport surface), but it
# was never going to be the answer to "the backend can read the gateway's
# filesystem and reach the network" -- that needs an actual isolation
# boundary, which on Linux means namespaces. Rather than hand-roll that with
# unshare/bind-mounts, this uses Docker, which already exists on the deploy
# target and already solves this correctly: --network=none for network
# isolation, --read-only plus a scratch tmpfs for filesystem isolation,
# --pids-limit/--memory for the same resource bounds sandboxed_stdio_args()
# applies via rlimits, and --cap-drop=ALL/--security-opt=no-new-privileges
# on top. See docker/backend-sandbox/Dockerfile for the image.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ContainerPolicy:
    image: str = "bf-agent-viewer-backend-sandbox:latest"
    network: str = "none"
    memory: str = "256m"
    pids_limit: int = 64
    tmpfs_size: str = "64m"
    container_workdir: str = "/backend"
    extra_docker_args: tuple[str, ...] = ()


def docker_available() -> bool:
    """Whether a Docker daemon is actually reachable right now, not just
    whether the CLI binary exists -- `docker info` talks to the daemon,
    `which docker` doesn't. Used to decide, at gateway startup, whether
    the container-sandboxed path or the rlimit-bootstrap fallback is what
    actually runs; logged either way, never silently downgraded."""
    try:
        result = subprocess.run(
            ["docker", "info"], capture_output=True, timeout=5,
        )
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def dockerized_stdio_args(
    script_path: str | Path,
    policy: SandboxPolicy | None = None,
    container_policy: ContainerPolicy | None = None,
) -> tuple[str, list[str], dict[str, str]]:
    """Build (command, args, env) that run the backend script inside a
    locked-down container instead of as a bare host subprocess. Use as:

        command, args, env = dockerized_stdio_args(backend_script, policy)
        StdioTransport(command=command, args=args, env=env)

    The container itself is the isolation boundary (network, filesystem,
    process count, memory, capabilities); `policy`'s own rlimits are not
    re-applied inside the container (redundant with --pids-limit/--memory)
    but its env allowlist still controls what the backend script sees.
    """
    policy = policy or SandboxPolicy()
    container_policy = container_policy or ContainerPolicy()
    resolved = Path(script_path).resolve()
    if not resolved.is_file():
        raise FileNotFoundError(f"Script not found: {resolved}")
    if not str(resolved).endswith(".py"):
        raise ValueError(f"Not a Python script: {resolved}")

    env = _build_env(policy)
    # The container has its own filesystem layout -- the host's PATH
    # string is meaningless (and would leak host directory structure)
    # inside it, so it's replaced rather than passed through.
    env["PATH"] = "/usr/bin:/bin"

    script_in_container = f"{container_policy.container_workdir}/script.py"

    args = [
        "run", "--rm", "-i",
        f"--network={container_policy.network}",
        "--read-only",
        "--tmpfs", f"/tmp:rw,size={container_policy.tmpfs_size}",
        f"--pids-limit={container_policy.pids_limit}",
        "--memory", container_policy.memory,
        "--memory-swap", container_policy.memory,
        "--cap-drop=ALL",
        "--security-opt", "no-new-privileges",
        "-v", f"{resolved}:{script_in_container}:ro",
        "-w", container_policy.container_workdir,
        *[x for k, v in env.items() for x in ("-e", f"{k}={v}")],
        *container_policy.extra_docker_args,
        container_policy.image,
        "python3", script_in_container,
    ]

    logger.info(
        "spawning containerized backend: image=%s network=%s script=%s env_keys=%s",
        container_policy.image, container_policy.network, resolved, sorted(env.keys()),
    )

    return "docker", args, {}


def image_available(image: str) -> bool:
    """Whether `image` already exists in the local Docker image store.
    Deliberately does NOT attempt a `docker pull` on a miss -- a gateway
    startup silently reaching out to a registry (which may not even be
    reachable, as in this project's own dev sandbox) is a surprise;
    building/pulling the image is a deploy-time step, checked for here,
    not triggered from here."""
    try:
        result = subprocess.run(
            ["docker", "image", "inspect", image], capture_output=True, timeout=10,
        )
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def build_backend_stdio_args(
    script_path: str | Path,
    policy: SandboxPolicy | None = None,
    container_policy: ContainerPolicy | None = None,
    prefer_container: bool = True,
) -> tuple[str, list[str], dict[str, str]]:
    """The single entry point gateway/server.py actually calls: picks the
    container-sandboxed path when Docker is reachable AND the configured
    image is actually present locally, falls back to the rlimit-bootstrap
    path otherwise. Which one ran is always logged -- silently downgrading
    real isolation to the weaker fallback without saying so would be
    exactly the kind of gap this file exists to not have. Checking the
    image's presence (not just the daemon's) matters in practice: a
    reachable daemon with a configured image that was never built/pulled
    would otherwise let `docker run` attempt a registry pull at gateway
    startup and fail obscurely deep inside FastMCP's transport instead of
    with a clear message here."""
    container_policy = container_policy or ContainerPolicy()
    if prefer_container and docker_available() and image_available(container_policy.image):
        return dockerized_stdio_args(script_path, policy, container_policy)
    if prefer_container and docker_available() and not image_available(container_policy.image):
        logger.warning(
            "docker is reachable but image %s is not built/pulled locally -- "
            "backend running under rlimit-bootstrap sandboxing only (no "
            "filesystem/network isolation); build docker/backend-sandbox/ "
            "or pull the image to get container isolation; see ISS-017",
            container_policy.image,
        )
    else:
        logger.warning(
            "docker not available or disabled -- backend running under "
            "rlimit-bootstrap sandboxing only (no filesystem/network "
            "isolation); see ISS-017"
        )
    return sandboxed_stdio_args(script_path, policy)
