"""Retry schedule and backoff: full jitter over 1s/4s/16s/64s/256s (02 §E).

02 §E fixes the ladder and the count — five attempts, then dead-letter — and this
module is the only place the arithmetic lives, because two copies of a backoff
schedule is two schedules that eventually disagree about when the fifth attempt
happens.

**Full jitter**, not "backoff with a bit of noise": the delays are
``random.uniform(0, base * 4**attempt)`` so a fleet of workers that failed at the
same instant does not come back at the same instant and take the receiver down
again. The distribution matters for the test too: the ceiling is deterministic and
the floor is zero, which is what "full" means.

The other rule here matters more than the numbers: a 4xx is not retried. A
permanent refusal retried five times is a hammer pointed at someone's validation
error (01 §G rejection trigger).
"""

from __future__ import annotations

import random
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Final

# 02 §E: 1s/4s/16s/64s/256s, five attempts, then dead-letter.
BASE_DELAY_SECONDS: Final = 1.0
BACKOFF_MULTIPLIER: Final = 4.0
MAX_ATTEMPTS: Final = 5
RETRY_CEILINGS_SECONDS: Final = (1.0, 4.0, 16.0, 64.0, 256.0)


class NonRetryableDeliveryError(RuntimeError):
    """The consumer refused for a reason a retry cannot fix (a 4xx).

    Raised by a sink for a permanent failure so the outbox can dead-letter
    immediately instead of spending five attempts and eleven minutes on a payload
    the consumer has already rejected on validation grounds.
    """


@dataclass(frozen=True, slots=True)
class RetryPlan:
    """One step of the schedule: how long to wait, and whether to try again."""

    attempt: int
    delay_seconds: float
    exhausted: bool


def ceiling_seconds(attempt: int) -> float:
    """The full-jitter ceiling for a given 1-based attempt number.

    Exposed separately from :func:`plan_delay` so a test can assert the ladder
    itself — 1/4/16/64/256 — independently of the randomness drawn on top of it.
    """
    index = min(max(attempt - 1, 0), len(RETRY_CEILINGS_SECONDS) - 1)
    return RETRY_CEILINGS_SECONDS[index]


def plan_delay(attempt: int, rng: random.Random | None = None) -> RetryPlan:
    """Full-jitter delay after ``attempt`` failures, or exhaustion at attempt 5.

    ``rng`` is injectable so a test can reproduce a schedule exactly; production
    passes nothing and gets ``random``.
    """
    if attempt >= MAX_ATTEMPTS:
        return RetryPlan(attempt=attempt, delay_seconds=0.0, exhausted=True)
    generator = rng or random
    return RetryPlan(
        attempt=attempt,
        delay_seconds=round(generator.uniform(0.0, ceiling_seconds(attempt)), 4),
        exhausted=False,
    )


def should_retry(status_code: int, attempts: int) -> bool:
    """Whether a response justifies another attempt.

    4xx means "your payload is wrong", which a retry cannot repair; 5xx and
    transport failures mean "try later", which is exactly what the ladder is for.
    """
    if 400 <= status_code < 500:
        return False
    return attempts < MAX_ATTEMPTS


def attempt_with_retry(
    call: Callable[[], Any],
    *,
    on_retry: Callable[[RetryPlan, BaseException], None] | None = None,
    retryable: Callable[[BaseException], bool] = lambda exc: not isinstance(
        exc, NonRetryableDeliveryError
    ),
    max_attempts: int = MAX_ATTEMPTS,
    rng: random.Random | None = None,
    sleeper: Callable[[float], None] = lambda _seconds: None,
) -> tuple[int, Any]:
    """Run ``call`` under the schedule and return (attempts_used, result).

    The default ``sleeper`` does not sleep: the outbox worker calls
    :func:`plan_delay` itself and stores ``next_attempt_at`` in the row instead of
    blocking a queue slot for four minutes. Sleeping inside a worker would turn one
    dead endpoint into a stalled queue for every case behind it (03 §K).
    """
    last_error: BaseException | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            return attempt, call()
        except Exception as exc:
            # Deliberately broad: every exception here is either retried or
            # re-raised, and neither branch swallows the cause (03 §A rule 1). The
            # one thing that must not be swallowed is a permanent refusal, which is
            # why NonRetryableDeliveryError is re-raised untouched.
            if not retryable(exc):
                raise
            last_error = exc
            plan = plan_delay(attempt, rng)
            if on_retry is not None:
                on_retry(plan, exc)
            if plan.exhausted:
                break
            sleeper(plan.delay_seconds)
    assert last_error is not None
    raise last_error


def describe_schedule() -> Sequence[str]:
    """The ladder as text, for the README and the degraded-response payload."""
    return tuple(
        f"attempt {attempt}: up to {ceiling_seconds(attempt):g}s full jitter"
        for attempt in range(1, MAX_ATTEMPTS + 1)
    )


__all__ = [
    "BACKOFF_MULTIPLIER",
    "BASE_DELAY_SECONDS",
    "MAX_ATTEMPTS",
    "RETRY_CEILINGS_SECONDS",
    "NonRetryableDeliveryError",
    "RetryPlan",
    "attempt_with_retry",
    "ceiling_seconds",
    "describe_schedule",
    "plan_delay",
    "should_retry",
]
