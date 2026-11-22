"""Retry with exponential backoff and jitter, for calls to external services.

Only errors marked transient are retried. Permanent errors (bad input, auth)
fail fast so we don't hammer a client's API or hide a real bug.
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TypeVar

T = TypeVar("T")


class TransientError(Exception):
    """Safe to retry: timeouts, 429, 5xx, dropped connections."""


class PermanentError(Exception):
    """Do not retry: validation errors, 4xx other than 429, auth failures."""


class RetryExhausted(Exception):
    def __init__(self, operation: str, attempts: int, last_error: Exception) -> None:
        super().__init__(f"{operation} failed after {attempts} attempts: {last_error}")
        self.operation = operation
        self.attempts = attempts
        self.last_error = last_error


@dataclass
class RetryPolicy:
    attempts: int = 4
    base_delay: float = 0.5
    max_delay: float = 30.0
    jitter: float = 0.25
    sleep: Callable[[float], None] = time.sleep

    def delay_for(self, attempt: int) -> float:
        d = min(self.max_delay, self.base_delay * (2 ** (attempt - 1)))
        return max(0.0, d * (1 + random.uniform(-self.jitter, self.jitter)))  # noqa: S311


def with_retry(
    fn: Callable[[], T],
    *,
    operation: str,
    policy: RetryPolicy,
    on_retry: Callable[[int, Exception, float], None] | None = None,
) -> T:
    last: Exception | None = None
    for attempt in range(1, policy.attempts + 1):
        try:
            return fn()
        except PermanentError:
            raise
        except TransientError as e:
            last = e
            if attempt == policy.attempts:
                break
            delay = policy.delay_for(attempt)
            if on_retry:
                on_retry(attempt, e, delay)
            policy.sleep(delay)
    assert last is not None
    raise RetryExhausted(operation, policy.attempts, last)
