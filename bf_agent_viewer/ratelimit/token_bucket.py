"""Per-agent token-bucket rate limiting (F-041).

Design (OQ-021, resolved Sept 21 2026): each agent identity gets its own
bucket -- steady refill rate plus a burst allowance -- so one runaway or
misbehaving agent can only exhaust its own allocation, not degrade
visibility for the rest of the fleet. Calls beyond an agent's allocation
are rejected at the gateway, not silently queued, and the rejection itself
is a signal worth surfacing (pairs with the F-036/F-042 alerting decisions
-- wiring an actual alert on repeated rejection is not yet built; this
module only does the accounting and the allow/deny decision).

This is a first real implementation, not a port -- nothing like this
existed in the prototype.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field


@dataclass
class _Bucket:
    tokens: float
    last_refill: float


@dataclass
class TokenBucketLimiter:
    """rate: steady tokens/sec refill. burst: max tokens a bucket can hold
    (i.e. how far above the steady rate an agent can spike briefly)."""

    rate: float = 5.0
    burst: float = 20.0
    _buckets: dict[str, _Bucket] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def allow(self, agent_id: str, cost: float = 1.0) -> bool:
        """Returns True and consumes `cost` tokens if the agent has enough
        budget; returns False (and consumes nothing) if not. Thread-safe --
        the gateway may serve multiple concurrent connections for
        identities sharing a process."""
        now = time.monotonic()
        with self._lock:
            bucket = self._buckets.get(agent_id)
            if bucket is None:
                bucket = _Bucket(tokens=self.burst, last_refill=now)
                self._buckets[agent_id] = bucket

            elapsed = now - bucket.last_refill
            bucket.tokens = min(self.burst, bucket.tokens + elapsed * self.rate)
            bucket.last_refill = now

            if bucket.tokens >= cost:
                bucket.tokens -= cost
                return True
            return False

    def remaining(self, agent_id: str) -> float:
        with self._lock:
            bucket = self._buckets.get(agent_id)
            return self.burst if bucket is None else bucket.tokens
