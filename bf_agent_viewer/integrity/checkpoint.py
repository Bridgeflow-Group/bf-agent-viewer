"""External chain-tip anchor for the tamper-evident event log (F-014/T-040,
OQ-012's addendum).

The gap this closes: `events/log.py`'s per-row hash chain proves that
*if* you start from a known-good chain tip, nothing between there and now
was altered -- but the chain tip itself, and every row, live in the one
SQLite file a database-level compromise already controls. A sufficiently
capable attacker with write access to that file could regenerate a whole
fake-but-internally-consistent history from GENESIS forward, and
`verify_chain()` alone would have no way to tell.

The fix OQ-012 actually decided on Sept 19, 2026 (and which this module
implements, after an industry-alignment research pass on 2026-09-24 found
it had been documented as built but never was -- see research-part-2.md):
periodic signed checkpoints, written to a second local file, separate
from the main SQLite database, using OS append-only semantics where
available. Each checkpoint records the event chain's current tip hash and
is signed with an install-time Ed25519 keypair the database itself never
touches -- so reproducing a valid checkpoint for a forged history would
require the private key, not just write access to the SQLite file.
Checkpoints are themselves chained (each one's payload includes the
previous checkpoint's hash), so the checkpoint file has its own
tamper-evidence independent of the database's.

Explicitly not bulletproof against a root-level attacker who can also
read the signing key file or rewrite the checkpoint file wholesale (a
`chattr +a` append-only flag is a deterrent a root user can remove with
`chattr -a`, not a hard barrier) -- matching OQ-012's own stated framing:
"a cheap deterrent against casual tampering, not bulletproof against a
root-level attacker." Real protection against that stronger threat model
means backing the checkpoint file (and ideally the signing key) up
off-box, which is an operational recommendation documented in CLI.md and
security.md, not something this module can enforce by itself.

Deliberately NOT wired into the gateway's per-request path (unlike
credential-registry refresh, F-016/T-024's `_maybe_refresh_token_registry`)
-- a signature + file write on every request would be real, unnecessary
overhead for a guarantee that only needs to be periodic. Matches this
project's existing pattern for F-019's retention pruning: a separate CLI
command (`bf-agent-viewer checkpoint write`), intended to be scheduled
externally (cron, or a container's own scheduler), not run automatically.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import platform
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from bf_agent_viewer.events import last_hash, verify_chain

# Sentinel for "no checkpoint has ever been written yet" -- mirrors
# events/log.py's own "GENESIS" sentinel for the event chain itself, kept
# as a visibly distinct string so the two chains' tip values are never
# confusable with each other in logs or checkpoint-file contents.
GENESIS_CHECKPOINT_HASH = "GENESIS_CHECKPOINT"


def default_checkpoint_path(db_path: str | os.PathLike) -> Path:
    """<db>.checkpoints.jsonl next to the database file by default --
    same directory, so a simple off-box backup of that directory (the
    operational recommendation OQ-012 documents) picks up both without
    needing separate configuration."""
    db_path = Path(db_path)
    return db_path.with_name(db_path.name + ".checkpoints.jsonl")


def default_signing_key_path(db_path: str | os.PathLike) -> Path:
    """<db>.checkpoint-signing-key next to the database file by default.
    Kept alongside the database for the common case, but nothing about
    this module requires that -- an operator who wants the signing key
    held somewhere more protected than the database's own disk can pass
    --signing-key-path explicitly (see CLI.md); the checkpoint file and
    the key are independent paths throughout this module."""
    db_path = Path(db_path)
    return db_path.with_name(db_path.name + ".checkpoint-signing-key")


def _enable_append_only(path: Path) -> bool:
    """Best-effort `chattr +a` (Linux only). Returns whether it actually
    took effect -- never raises. Not every filesystem/host supports this
    (tmpfs, overlayfs, non-Linux, or missing CAP_LINUX_IMMUTABLE inside a
    container), and the checkpoint mechanism's real guarantee (the
    signature) must keep working regardless -- append-only is an extra
    deterrent layered on top, not a dependency. Safe to call on every
    write: `chattr +a` on an already-flagged file is a no-op, and the
    flag itself doesn't block further appends (only truncation/rewrite/
    delete), which is exactly the semantics this file needs."""
    if platform.system() != "Linux":
        return False
    try:
        result = subprocess.run(
            ["chattr", "+a", str(path)], capture_output=True, timeout=5, check=False,
        )
        return result.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def load_or_create_signing_key(key_path: str | os.PathLike) -> Ed25519PrivateKey:
    """The install-time keypair OQ-012 calls for: generated once, on
    first use, and reused for every checkpoint after that -- never
    regenerated automatically, since a key that changes silently would
    make every prior checkpoint's signature unverifiable against the
    "current" public key. Private key material is written raw (32 bytes,
    no passphrase -- this runs unattended from a cron job, so a
    passphrase would just move the secret into a script's environment)
    with 0600 permissions, created exclusively (O_EXCL) so two processes
    racing to initialize the same fresh install can't clobber each
    other's key. The public key is written alongside as `<key_path>.pub`
    (world-readable is fine and useful -- it's what `checkpoint verify`
    needs, and what an operator would copy off-box to verify checkpoints
    independently of the host that signed them)."""
    key_path = Path(key_path)
    if key_path.exists():
        return Ed25519PrivateKey.from_private_bytes(key_path.read_bytes())

    key = Ed25519PrivateKey.generate()
    private_bytes = key.private_bytes_raw()
    fd = os.open(str(key_path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(private_bytes)
    except FileExistsError:
        # Lost the race to another process initializing the same fresh
        # install concurrently -- use whichever key actually landed.
        return Ed25519PrivateKey.from_private_bytes(key_path.read_bytes())

    pub_path = key_path.with_name(key_path.name + ".pub")
    pub_path.write_bytes(key.public_key().public_bytes_raw())
    return key


def _load_public_key(key_path: str | os.PathLike) -> Ed25519PublicKey:
    """Verification only ever needs the public key, not the private one
    -- reads `<key_path>.pub` (falling back to deriving it from the
    private key file if the .pub file is missing, e.g. an older
    checkpoint directory copied without it)."""
    key_path = Path(key_path)
    pub_path = key_path.with_name(key_path.name + ".pub")
    if pub_path.exists():
        return Ed25519PublicKey.from_public_bytes(pub_path.read_bytes())
    if key_path.exists():
        private_key = Ed25519PrivateKey.from_private_bytes(key_path.read_bytes())
        return private_key.public_key()
    raise FileNotFoundError(
        f"no signing key found at {key_path} (or {pub_path}) -- "
        "run `bf-agent-viewer checkpoint write` first to generate one"
    )


def _checkpoint_content_hash(payload: dict) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


@dataclass(frozen=True)
class Checkpoint:
    seq: int
    created_at: str
    chain_tip_hash: str
    event_count: int
    prev_checkpoint_hash: str
    checkpoint_hash: str
    signature: str  # base64-encoded Ed25519 signature over checkpoint_hash

    def _payload(self) -> dict:
        return {
            "seq": self.seq,
            "created_at": self.created_at,
            "chain_tip_hash": self.chain_tip_hash,
            "event_count": self.event_count,
            "prev_checkpoint_hash": self.prev_checkpoint_hash,
        }

    @classmethod
    def _from_line(cls, line: dict) -> "Checkpoint":
        return cls(
            seq=line["seq"], created_at=line["created_at"],
            chain_tip_hash=line["chain_tip_hash"], event_count=line["event_count"],
            prev_checkpoint_hash=line["prev_checkpoint_hash"],
            checkpoint_hash=line["checkpoint_hash"], signature=line["signature"],
        )


def _read_all_checkpoints(checkpoint_path: str | os.PathLike) -> list[Checkpoint]:
    path = Path(checkpoint_path)
    if not path.exists():
        return []
    checkpoints = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            checkpoints.append(Checkpoint._from_line(json.loads(line)))
    return checkpoints


def write_checkpoint(
    conn,
    *,
    checkpoint_path: str | os.PathLike,
    key_path: str | os.PathLike,
) -> Checkpoint:
    """Append one signed checkpoint recording the event chain's current
    tip. Safe to call repeatedly (e.g. from a cron job) -- each call
    appends a new, independently signed entry; nothing is ever rewritten
    in place, which is what makes the append-only file mode meaningful."""
    checkpoint_path = Path(checkpoint_path)
    key = load_or_create_signing_key(key_path)

    existing = _read_all_checkpoints(checkpoint_path)
    prev_checkpoint_hash = existing[-1].checkpoint_hash if existing else GENESIS_CHECKPOINT_HASH
    seq = existing[-1].seq + 1 if existing else 0

    chain_tip_hash = last_hash(conn)
    event_count = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    created_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    payload = {
        "seq": seq,
        "created_at": created_at,
        "chain_tip_hash": chain_tip_hash,
        "event_count": event_count,
        "prev_checkpoint_hash": prev_checkpoint_hash,
    }
    checkpoint_hash = _checkpoint_content_hash(payload)
    signature = base64.b64encode(key.sign(checkpoint_hash.encode())).decode()

    line = {**payload, "checkpoint_hash": checkpoint_hash, "signature": signature}

    is_new_file = not checkpoint_path.exists()
    # Plain append -- compatible with chattr +a's append-only semantics
    # (that flag blocks truncate/overwrite/delete, not append), so this
    # works identically whether or not the flag actually took on this
    # filesystem.
    with open(checkpoint_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(line, sort_keys=True) + "\n")
    if is_new_file:
        os.chmod(checkpoint_path, 0o644)
    _enable_append_only(checkpoint_path)

    return Checkpoint._from_line(line)


def verify_checkpoint_file(
    checkpoint_path: str | os.PathLike,
    key_path: str | os.PathLike,
) -> tuple[bool, str | None]:
    """Verify the checkpoint file's own internal integrity: every entry's
    signature is valid under the install's public key, and each entry's
    prev_checkpoint_hash correctly chains from the one before it (so a
    deleted or reordered checkpoint entry is detectable even though
    that's a weaker guarantee than a root-enforced append-only flag).
    Does NOT check the checkpoints against the live `events` table --
    see verify_against_events for that.

    Returns (ok, error_detail). An empty checkpoint file (nothing written
    yet) is considered valid -- there is simply nothing to check."""
    checkpoints = _read_all_checkpoints(checkpoint_path)
    if not checkpoints:
        return True, None

    public_key = _load_public_key(key_path)
    prev_hash = GENESIS_CHECKPOINT_HASH
    for cp in checkpoints:
        if cp.prev_checkpoint_hash != prev_hash:
            return False, (
                f"checkpoint seq {cp.seq}: prev_checkpoint_hash does not match the "
                "preceding entry -- an entry may have been deleted, reordered, or inserted"
            )
        expected_hash = _checkpoint_content_hash(cp._payload())
        if expected_hash != cp.checkpoint_hash:
            return False, f"checkpoint seq {cp.seq}: recorded checkpoint_hash does not match its own payload"
        try:
            public_key.verify(base64.b64decode(cp.signature), cp.checkpoint_hash.encode())
        except InvalidSignature:
            return False, f"checkpoint seq {cp.seq}: signature is invalid under the install's public key"
        prev_hash = cp.checkpoint_hash
    return True, None


def verify_against_events(
    conn,
    *,
    checkpoint_path: str | os.PathLike,
    key_path: str | os.PathLike,
) -> tuple[bool, str | None]:
    """The real point of this whole module: confirm the live database's
    event chain hasn't been regenerated or altered since the most recent
    signed checkpoint. Three things have to hold:

    1. The checkpoint file itself is internally valid (verify_checkpoint_file).
    2. The event table's own hash chain is internally valid (events.verify_chain).
    3. The latest checkpoint's signed chain_tip_hash actually appears as a
       real row's content_hash in the current `events` table.

    (3) is what an external anchor is actually for: an attacker who
    regenerated a fake-but-internally-consistent event history entirely
    inside the SQLite file (satisfying (2) on its own) has no way to
    produce a *signed* checkpoint matching that fake history's tip,
    because the signing key never touches the database. If the real
    history was altered after the last checkpoint was written, the
    checkpoint's recorded tip hash won't exist anywhere in the tampered
    table -- that mismatch is the detection.

    This only ever catches tampering at or before the latest checkpoint;
    anything altered *after* the last `checkpoint write` and before the
    next one is invisible to this check until the next checkpoint is
    due -- exactly why this needs to run periodically (see the module
    docstring), not just once."""
    file_ok, file_err = verify_checkpoint_file(checkpoint_path, key_path)
    if not file_ok:
        return False, f"checkpoint file itself failed verification: {file_err}"

    checkpoints = _read_all_checkpoints(checkpoint_path)
    if not checkpoints:
        return True, "no checkpoints have been written yet -- nothing to cross-check against the database"

    chain_ok, bad_event_id = verify_chain(conn)
    if not chain_ok:
        return False, f"the event log's own hash chain failed verification at event {bad_event_id}"

    latest = checkpoints[-1]
    if latest.chain_tip_hash == "GENESIS":
        # The checkpoint was written before the very first event was ever
        # logged -- nothing to look up, and trivially not tampered with.
        return True, None

    found = conn.execute(
        "SELECT 1 FROM events WHERE content_hash = ?", (latest.chain_tip_hash,),
    ).fetchone()
    if found is None:
        return False, (
            f"the latest signed checkpoint (seq {latest.seq}, written {latest.created_at}) "
            f"recorded chain tip {latest.chain_tip_hash[:16]}..., which does not appear "
            "anywhere in the current event table -- the database's event history may have "
            "been altered or regenerated since this checkpoint was signed"
        )
    return True, None
