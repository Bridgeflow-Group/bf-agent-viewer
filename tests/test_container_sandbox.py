"""Real-container verification for the filesystem/network-isolation half
of ISS-017/F-044: does dockerized_stdio_args() actually cause network
access and filesystem writes to be blocked *inside* the spawned
container, not just construct a docker run command that looks right?

Uses a locally-built minimal image (BF_TEST_SANDBOX_IMAGE, no pip
install of fastmcp -- these tests only need Python's stdlib) so this
suite runs in environments without registry access; the real deploy
target builds docker/backend-sandbox/Dockerfile instead, which the
Docker daemon and dockerized_stdio_args() treat identically.
"""
import json
import os
import subprocess

import pytest

from bf_agent_viewer.gateway.sandbox import (
    ContainerPolicy,
    SandboxPolicy,
    docker_available,
    dockerized_stdio_args,
)

HERE = os.path.dirname(os.path.abspath(__file__))
PROBE_SCRIPT = os.path.join(HERE, "fixtures", "container_probe.py")

TEST_IMAGE = os.environ.get("BF_TEST_SANDBOX_IMAGE", "bf-local-python:minimal")


def _image_present() -> bool:
    result = subprocess.run(
        ["docker", "image", "inspect", TEST_IMAGE], capture_output=True, timeout=10,
    )
    return result.returncode == 0


pytestmark = [
    pytest.mark.skipif(
        not docker_available(), reason="Docker daemon not reachable in this environment"
    ),
    pytest.mark.skipif(
        not _image_present(),
        reason=f"{TEST_IMAGE} not built locally -- see docker/backend-sandbox/",
    ),
]


def _run_probe(container_policy: ContainerPolicy) -> dict:
    command, args, _env = dockerized_stdio_args(
        PROBE_SCRIPT, SandboxPolicy(), container_policy,
    )
    result = subprocess.run(
        [command, *args], capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, f"probe container failed: {result.stderr}"
    return json.loads(result.stdout)


def test_network_actually_blocked_inside_container():
    observed = _run_probe(ContainerPolicy(image=TEST_IMAGE))
    assert observed["network_blocked"] is True


def test_filesystem_write_actually_blocked_inside_container():
    observed = _run_probe(ContainerPolicy(image=TEST_IMAGE))
    assert observed["fs_write_blocked"] is True


def test_dockerized_stdio_args_rejects_missing_script():
    with pytest.raises(FileNotFoundError):
        dockerized_stdio_args(
            "/no/such/backend.py", SandboxPolicy(), ContainerPolicy(image=TEST_IMAGE),
        )


def test_dockerized_stdio_args_builds_expected_flags():
    command, args, _env = dockerized_stdio_args(
        PROBE_SCRIPT, SandboxPolicy(), ContainerPolicy(image=TEST_IMAGE),
    )
    assert command == "docker"
    assert "--network=none" in args
    assert "--read-only" in args
    assert "--cap-drop=ALL" in args
    assert TEST_IMAGE in args
