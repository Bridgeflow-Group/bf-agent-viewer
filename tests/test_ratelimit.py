import time

from bf_agent_viewer.ratelimit import TokenBucketLimiter


def test_allows_up_to_burst_then_denies():
    limiter = TokenBucketLimiter(rate=1.0, burst=3.0)
    assert limiter.allow("agent-1")
    assert limiter.allow("agent-1")
    assert limiter.allow("agent-1")
    assert not limiter.allow("agent-1")


def test_refills_over_time():
    limiter = TokenBucketLimiter(rate=100.0, burst=1.0)
    assert limiter.allow("agent-1")
    assert not limiter.allow("agent-1")
    time.sleep(0.05)  # 100/sec rate should refill well over 1 token in 50ms
    assert limiter.allow("agent-1")


def test_agents_have_independent_buckets():
    limiter = TokenBucketLimiter(rate=1.0, burst=1.0)
    assert limiter.allow("agent-1")
    assert not limiter.allow("agent-1")
    # A different agent's budget must be untouched by agent-1 exhausting its own.
    assert limiter.allow("agent-2")
