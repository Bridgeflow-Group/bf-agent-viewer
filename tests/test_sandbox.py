"""Real-process verification for the ISS-017/F-044 fix: does
sandboxed_stdio_args() actually cause the resource limits to be in
effect *inside* a spawned child, not just construct a command that
looks right? This spawns a real subprocess with the exact
(command, args, env) the real gateway now uses and reads back what the
child observed about itself.
"""
import json
import os
import subprocess
import sys

import pytest

from bf_agent_viewer.gateway.sandbox import (
    LIMITS_ENV_VAR,
    ContainerPolicy,
    SandboxPolicy,
    build_backend_stdio_args,
    build_env,
    docker_available,
    image_available,
    sandboxed_stdio_args,
)

HERE = os.path.dirname(os.path.abspath(__file__))
PROBE_SCRIPT = os.path.join(HERE, "fixtures", "rlimit_probe.py")

# A distinctly-non-default policy so a passing test can't be accidentally
# matching the host's own ambient limits.
TIGHT_POLICY = SandboxPolicy(
    limits={"RLIMIT_NOFILE": (128, 128), "RLIMIT_NPROC": (32, 32), "RLIMIT_CORE": (0, 0)},
)


def _run_probe(policy: SandboxPolicy) -> dict:
    command, args, env = sandboxed_stdio_args(PROBE_SCRIPT, policy)
    result = subprocess.run(
        [command, *args], capture_output=True, text=True, env=env, timeout=10,
    )
    assert result.returncode == 0, f"probe failed: {result.stderr}"
    return json.loads(result.stdout)


@pytest.mark.skipif(os.name != "posix", reason="rlimits are POSIX-only")
def test_rlimits_actually_applied_inside_spawned_child():
    observed = _run_probe(TIGHT_POLICY)
    for name, (soft, hard) in TIGHT_POLICY.limits.items():
        assert observed["rlimits"][name] == [soft, hard], (
            f"{name} was not actually enforced in the child "
            f"(wanted {[soft, hard]}, saw {observed['rlimits'][name]})"
        )


def test_limits_env_var_not_visible_to_backend_script():
    # The bootstrap must pop LIMITS_ENV_VAR before running the real
    # script, so the backend itself -- and by extension anything an
    # agent could get the backend to leak -- never sees it.
    observed = _run_probe(TIGHT_POLICY)
    assert LIMITS_ENV_VAR not in observed["env_keys"]


def test_env_allowlist_actually_strips_the_child_environment():
    policy = SandboxPolicy(extra_env={})
    os.environ["_BF_TEST_SECRET_SHOULD_NOT_LEAK"] = "shhh"
    try:
        observed = _run_probe(policy)
    finally:
        del os.environ["_BF_TEST_SECRET_SHOULD_NOT_LEAK"]

    assert "_BF_TEST_SECRET_SHOULD_NOT_LEAK" not in observed["env_keys"]
    # Every key the child saw is either one build_env() actually put
    # there, or one CPython's own startup adds on its own regardless of
    # the env passed in (LC_CTYPE, when it coerces the locale -- observed
    # here, not something this sandbox controls). Nothing extra leaked
    # through some other path, e.g. subprocess merging rather than
    # replacing the environment.
    expected = set(build_env(policy).keys())
    cpython_runtime_added = {"LC_CTYPE"}
    unexplained = set(observed["env_keys"]) - expected - cpython_runtime_added
    assert not unexplained, f"unexplained env leaked into child: {unexplained}"


def test_sandboxed_stdio_args_rejects_missing_script():
    with pytest.raises(FileNotFoundError):
        sandboxed_stdio_args("/no/such/backend.py", SandboxPolicy())


def test_sandboxed_stdio_args_rejects_non_python_script(tmp_path):
    not_python = tmp_path / "backend.sh"
    not_python.write_text("#!/bin/sh\necho hi\n")
    with pytest.raises(ValueError):
        sandboxed_stdio_args(str(not_python), SandboxPolicy())


def test_sandboxed_stdio_args_uses_current_interpreter():
    command, args, _env = sandboxed_stdio_args(PROBE_SCRIPT, SandboxPolicy())
    assert command == sys.executable
    assert args[0] == "-c"
    assert args[2].endswith("rlimit_probe.py")


def test_image_available_false_for_nonexistent_image():
    assert image_available("bf-agent-viewer-this-image-does-not-exist:latest") is False


@pytest.mark.skipif(not os.name == "posix", reason="rlimits are POSIX-only")
def test_build_backend_stdio_args_falls_back_when_image_missing():
    # Docker itself may well be reachable in this environment (it is, in
    # the dev sandbox this was built in) -- what must NOT happen is
    # silently attempting to run a configured image that was never
    # built/pulled. Falling back to the rlimit path, observably (command
    # is the interpreter, not "docker"), is the correct behavior.
    command, args, _env = build_backend_stdio_args(
        PROBE_SCRIPT,
        SandboxPolicy(),
        ContainerPolicy(image="bf-agent-viewer-this-image-does-not-exist:latest"),
        prefer_container=True,
    )
    assert command == sys.executable
    assert args[0] == "-c"


def test_build_backend_stdio_args_respects_prefer_container_false():
    command, _args, _env = build_backend_stdio_args(
        PROBE_SCRIPT, SandboxPolicy(), ContainerPolicy(), prefer_container=False,
    )
    assert command == sys.executable


@pytest.mark.skipif(not docker_available(), reason="Docker daemon not reachable")
def test_build_backend_stdio_args_uses_docker_when_image_present():
    test_image = os.environ.get("BF_TEST_SANDBOX_IMAGE", "bf-local-python:minimal")
    if not image_available(test_image):
        pytest.skip(f"{test_image} not built locally")
    command, args, _env = build_backend_stdio_args(
        PROBE_SCRIPT, SandboxPolicy(), ContainerPolicy(image=test_image), prefer_container=True,
    )
    assert command == "docker"
    assert test_image in args
