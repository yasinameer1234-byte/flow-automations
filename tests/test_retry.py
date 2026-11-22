import pytest

from flow.core.retry import PermanentError, RetryExhausted, RetryPolicy, TransientError, with_retry


def test_retries_transient_then_succeeds():
    calls = {"n": 0}
    delays = []

    def fn():
        calls["n"] += 1
        if calls["n"] < 3:
            raise TransientError("flaky")
        return "ok"

    policy = RetryPolicy(attempts=4, base_delay=1, jitter=0, sleep=delays.append)
    assert with_retry(fn, operation="t", policy=policy) == "ok"
    assert calls["n"] == 3
    assert delays == [1, 2]  # exponential backoff


def test_permanent_error_is_not_retried():
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        raise PermanentError("bad input")

    with pytest.raises(PermanentError):
        with_retry(fn, operation="t", policy=RetryPolicy(sleep=lambda _: None))
    assert calls["n"] == 1


def test_exhaustion_raises_with_context():
    def fn():
        raise TransientError("still down")

    with pytest.raises(RetryExhausted) as ei:
        with_retry(fn, operation="send", policy=RetryPolicy(attempts=2, sleep=lambda _: None))
    assert ei.value.attempts == 2 and "still down" in str(ei.value)


def test_delay_is_capped():
    p = RetryPolicy(base_delay=10, max_delay=15, jitter=0)
    assert p.delay_for(10) == 15
