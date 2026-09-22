"""Test-only fixture, NOT a real MCP backend. Run directly (not imported)
by test_sandbox.py via the exact (command, args, env) sandboxed_stdio_args()
builds, standing in for "the real backend script" in that bootstrap's
runpy.run_path() call. Prints what it can actually observe about its own
process -- the rlimits actually in effect, and which env vars it can see --
as JSON on stdout, so the test can assert on real child-process state
instead of just on how the parent constructed the spawn.
"""
import json
import os
import resource

print(json.dumps({
    "rlimits": {
        name: list(resource.getrlimit(getattr(resource, name)))
        for name in ("RLIMIT_NOFILE", "RLIMIT_NPROC", "RLIMIT_CORE")
    },
    "env_keys": sorted(os.environ.keys()),
}))
