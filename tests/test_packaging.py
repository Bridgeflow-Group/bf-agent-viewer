"""T-013: does `pip install .` -- the exact command docker/app/Dockerfile
runs -- actually produce a package that works, not just one that imports
under pytest from a source checkout?

Real bug this caught (Sept 23, 2026): pyproject.toml's package-data only
listed the console's *.html templates. bf_agent_viewer/db/schema.sql and
migrations.sql are read at runtime via Path(__file__).with_name(...), not
imported as Python -- setuptools does not package non-.py files by
default, so an actual `pip install .` had no schema.sql on disk at all.
`bf-agent-viewer org create` (step one of the README quickstart, and the
Docker Compose gateway service's own bootstrap) crashed with
FileNotFoundError the moment anyone ran it against a fresh database.
pytest run from a source checkout never caught this: the repo's own
schema.sql is always right there on disk next to connection.py regardless
of what package-data declares.

This builds a real wheel with `pip wheel --no-deps --no-build-isolation`
in a fresh, disposable venv (avoids Debian's patched system distutils,
and needs no network -- no dependency resolution happens with --no-deps)
and inspects its actual file listing, the same thing `docker build` would
end up with after its own `pip install ".[console]"` step. Building the
real container image and running it is still blocked in this dev sandbox
specifically because pulling `python:3.11-slim` from Docker Hub is denied
by this environment's egress policy (confirmed: `docker pull` returns 403
Forbidden) -- not something to route around per this environment's own
proxy guidance, and the same limitation already documented for
docker/backend-sandbox/Dockerfile's image. This test is the part of that
verification that doesn't need a container runtime or registry access at
all, and it would have caught the schema.sql bug on its own.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import venv
import zipfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

# setuptools' own `build/` and `*.egg-info` directories, if left over in
# REPO_ROOT from a previous manual/editable install, can make a wheel
# build silently reuse stale collected metadata instead of re-reading
# pyproject.toml's current package-data config -- caught directly while
# writing this test (a leftover egg-info made a broken package-data
# config look fine). Building from a clean copy of the source tree,
# instead of REPO_ROOT itself, is what a real `docker build`'s COPY step
# already does -- a fresh context, no local dev-time build state -- so
# this makes the test match that instead of trusting whatever's sitting
# around in this checkout right now.
_IGNORE = shutil.ignore_patterns(
    "build", "*.egg-info", "__pycache__", "*.pyc", ".git", ".pytest_cache",
)

# Every non-.py file bf_agent_viewer reads at runtime via Path(__file__)
# (see db/connection.py's _SCHEMA_PATH/_MIGRATIONS_PATH and
# console/app.py's TEMPLATES_DIR) -- if setuptools' package-data config
# ever misses one of these, this list is what catches it, not a lucky
# read of the source tree.
EXPECTED_DATA_FILES = [
    "bf_agent_viewer/db/schema.sql",
    "bf_agent_viewer/db/migrations.sql",
    "bf_agent_viewer/console/templates/base.html",
    "bf_agent_viewer/console/templates/dashboard.html",
    "bf_agent_viewer/console/templates/agent_detail.html",
    "bf_agent_viewer/console/templates/event_detail.html",
    "bf_agent_viewer/console/templates/search.html",
    "bf_agent_viewer/console/templates/onboarding.html",
    "bf_agent_viewer/console/templates/login.html",
    "bf_agent_viewer/console/templates/enroll.html",
    "bf_agent_viewer/console/templates/verify.html",
    "bf_agent_viewer/console/templates/not_found.html",
    "bf_agent_viewer/console/templates/export.html",
]


@pytest.fixture(scope="module")
def built_wheel(tmp_path_factory):
    """Build the real wheel once per test module -- venv creation plus a
    real pip subprocess is a few seconds, no reason to pay it per test."""
    venv_dir = tmp_path_factory.mktemp("packaging_venv")
    venv.create(venv_dir, with_pip=True)
    venv_python = venv_dir / "bin" / "python"

    # `--no-build-isolation` below means pip builds using *this* venv's own
    # environment rather than fetching an isolated build backend -- so
    # setuptools.build_meta (pyproject.toml's build-system.build-backend)
    # has to actually be importable here first. venv.create(with_pip=True)
    # does not guarantee that: on newer Python/pip (confirmed against
    # Python 3.12 in real GitHub Actions CI, 2026-09-24 -- the first real
    # run of ci.yml), a fresh venv's ensurepip step installs pip alone, not
    # setuptools/wheel alongside it, unlike this dev sandbox's own default
    # venv behavior, which is why this passed locally and failed in CI.
    # Installing them explicitly makes the fixture's own environment
    # assumption correct instead of accidental.
    install_result = subprocess.run(
        [str(venv_python), "-m", "pip", "install", "--no-cache-dir", "setuptools", "wheel"],
        capture_output=True, text=True, timeout=120,
    )
    assert install_result.returncode == 0, (
        f"could not install setuptools/wheel into the build venv:\n"
        f"{install_result.stdout}\n{install_result.stderr}"
    )

    # A clean copy of the source tree, not REPO_ROOT itself -- see _IGNORE's
    # comment above for why building in place isn't trustworthy.
    build_context = tmp_path_factory.mktemp("packaging_src") / "bf-agent-viewer"
    shutil.copytree(REPO_ROOT, build_context, ignore=_IGNORE)

    out_dir = tmp_path_factory.mktemp("packaging_wheel")
    result = subprocess.run(
        [
            str(venv_python), "-m", "pip", "wheel",
            "--no-deps", "--no-build-isolation", "--no-cache-dir",
            "-w", str(out_dir), str(build_context),
        ],
        capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, (
        f"pip wheel failed (this is the same build step docker/app/Dockerfile's "
        f"`pip install .` runs):\n{result.stdout}\n{result.stderr}"
    )

    wheels = list(out_dir.glob("*.whl"))
    assert len(wheels) == 1, f"expected exactly one wheel, got {wheels}"
    return wheels[0]


def test_wheel_contains_every_runtime_data_file(built_wheel):
    with zipfile.ZipFile(built_wheel) as zf:
        names = set(zf.namelist())

    missing = [f for f in EXPECTED_DATA_FILES if f not in names]
    assert not missing, (
        f"missing from the built wheel (present in the source tree, but not "
        f"packaged -- pyproject.toml's [tool.setuptools.package-data] needs a "
        f"matching entry): {missing}"
    )


def test_installed_package_can_actually_connect_to_a_fresh_database(built_wheel, tmp_path):
    """The concrete failure the missing schema.sql caused: install the
    real wheel into a clean venv with nothing else on the path, and run
    the exact first command a fresh install's README quickstart runs."""
    install_dir = tmp_path / "installed_venv"
    venv.create(install_dir, with_pip=True)
    install_python = install_dir / "bin" / "python"

    result = subprocess.run(
        [str(install_python), "-m", "pip", "install", "--no-deps", "--no-cache-dir", str(built_wheel)],
        capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, f"install failed:\n{result.stdout}\n{result.stderr}"

    db_path = tmp_path / "smoke.db"
    result = subprocess.run(
        [
            str(install_python), "-c",
            "from bf_agent_viewer.db import connect; "
            f"conn = connect({str(db_path)!r}); "
            "conn.execute(\"INSERT INTO organizations (id, name) VALUES ('org-1', 'Acme')\"); "
            "conn.commit(); "
            "print(conn.execute('SELECT id, name FROM organizations').fetchone())",
        ],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, (
        f"connect()/schema bootstrap failed against the installed package "
        f"(this is exactly the FileNotFoundError the missing package-data caused):\n"
        f"{result.stdout}\n{result.stderr}"
    )
    assert "('org-1', 'Acme')" in result.stdout
