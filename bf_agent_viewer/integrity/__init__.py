"""External chain-tip anchor for the tamper-evident event log (F-014/T-040).

See checkpoint.py's module docstring for the full design rationale.
"""
from bf_agent_viewer.integrity.checkpoint import (
    Checkpoint,
    default_checkpoint_path,
    default_signing_key_path,
    load_or_create_signing_key,
    verify_against_events,
    verify_checkpoint_file,
    write_checkpoint,
)

__all__ = [
    "Checkpoint",
    "default_checkpoint_path",
    "default_signing_key_path",
    "load_or_create_signing_key",
    "verify_against_events",
    "verify_checkpoint_file",
    "write_checkpoint",
]
