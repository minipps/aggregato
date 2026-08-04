"""The retry ladder, run lineage, and the degraded threshold.

Pure decision logic: given a classification, the provider's current retry state, and its normal
interval, say what should happen next. Nothing here touches the database — the caller persists
``next_run_at``, ``retry_step``, ``consecutive_failures``, and ``status`` onto ``provider_state``
(data-model.md §7). That split is what makes this module arithmetic rather than fixtures.

Time is injected (testing guidance). No function here reads a wall clock, which is the whole reason
the ladder is testable without sleeping.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID, uuid4

from aggregato.domain.clock import Clock
from aggregato.domain.enums import ErrorClass, ProviderStatus
from aggregato.sync.errors import NEVER_RETRY, schedules_retry

LADDER: tuple[timedelta, ...] = (
    timedelta(minutes=1),
    timedelta(minutes=5),
    timedelta(minutes=15),
    timedelta(hours=1),
)
"""``retry_step`` 0→1→2→3 maps to 1m, 5m, 15m, 1h; beyond that, the provider's normal interval."""

DEGRADED_AFTER_FAILURES = len(LADDER) + 1
"""When a provider becomes ``degraded``.

Named rather than inlined because the value is a decision, not a constant: the ladder is four
attempts long, so the failure *after* it is fully spent is the first evidence that this is not a
transient blip. Tying the threshold to ``len(LADDER)`` also means "degraded" and "the ladder is
exhausted" can never drift apart — a degraded provider therefore retries at the normal interval and
never faster, which is exactly .
"""


class NeverRetryScheduledError(AssertionError):
    """A retry time was attached to a class in :data:`NEVER_RETRY`.

    Unreachable by design. It exists so that a future edit to the ladder cannot quietly schedule a
    retry for ``auth``, ``blocked``, or ``structure_changed``: the state is rejected at
    construction, in production as well as in tests.
    """


@dataclass(frozen=True, slots=True)
class RetryDecision:
    """What the scheduler should write after a run finishes.

    Attributes:
        next_run_at: When to run this provider again, or ``None`` for "not on a schedule" — the
            never-retry classes. ``None`` means an operator has to act.
        retry_step: The ladder position to store; 0 after a success.
        consecutive_failures: The failure counter to store; 0 after a success.
        status: The provider status to store.
        error_class: The classification this decision came from; ``None`` after a success.
        lineage_id: The lineage the *next* attempt belongs to, so the UI shows four attempts as one
            failing sync rather than four unrelated failures . ``None`` when there is no
            next attempt, or when the next run starts fresh work.
    """

    next_run_at: datetime | None
    retry_step: int
    consecutive_failures: int
    status: ProviderStatus
    error_class: ErrorClass | None
    lineage_id: UUID | None

    def __post_init__(self) -> None:
        if self.error_class in NEVER_RETRY and self.next_run_at is not None:
            raise NeverRetryScheduledError(
                f"{self.error_class} is in NEVER_RETRY but a retry was scheduled for "
                f"{self.next_run_at}: retrying it risks account lockout, a deeper block, or "
                f"hammering a platform whose payload changed "
            )


def lineage_id_for(retry_of: UUID | None) -> UUID:
    """The ``lineage_id`` a starting run belongs to.

    Args:
        retry_of: The lineage of the run this attempt retries, from the previous
            :attr:`RetryDecision.lineage_id`. ``None`` when this is not a retry.

    Returns:
        ``retry_of`` unchanged for a retry, so every attempt at one logical piece of work groups
        together; a fresh UUID otherwise .
    """
    return retry_of if retry_of is not None else uuid4()


def plan_after_success(*, clock: Clock, normal_interval: timedelta) -> RetryDecision:
    """The decision after a run that succeeded.

    Args:
        clock: Injected time source.
        normal_interval: The provider's declared interval .

    Returns:
        A decision resetting the ladder and the failure counter to 0 (data-model.md §7) and putting
        the provider back to ``idle``, due again one normal interval from now. The lineage is
        dropped: the next run is new work, not another attempt at this one.
    """
    return RetryDecision(
        next_run_at=clock.now() + normal_interval,
        retry_step=0,
        consecutive_failures=0,
        status=ProviderStatus.IDLE,
        error_class=None,
        lineage_id=None,
    )


def plan_after_failure(
    *,
    clock: Clock,
    error_class: ErrorClass,
    retry_step: int,
    consecutive_failures: int,
    normal_interval: timedelta,
    lineage_id: UUID,
    retry_after: timedelta | None = None,
) -> RetryDecision:
    """The decision after a run that failed or came back ``partial``.

    Args:
        clock: Injected time source.
        error_class: From :func:`aggregato.sync.errors.classify`.
        retry_step: The provider's ladder position *before* this failure; indexes :data:`LADDER`.
        consecutive_failures: The provider's failure count *before* this failure.
        normal_interval: The provider's declared interval, used once the ladder is spent.
        lineage_id: The lineage of the run that just failed; carried to the retry .
        retry_after: A platform-supplied delay, e.g. from a ``Retry-After`` header on a
            ``RateLimited``. Honoured over the ladder step when given . The session-wide
            interval stretching that a rate limit also triggers is the caller's business.

    Returns:
        A decision with the incremented counters. For a class in
        :data:`aggregato.sync.errors.NEVER_RETRY` there is no ``next_run_at`` at all and the status
        is ``degraded`` immediately; :class:`NeverRetryScheduledError` guards that structurally.

    Raises:
        NeverRetryScheduledError: Never in practice — see the class docstring.
    """
    failures = consecutive_failures + 1
    degraded = failures >= DEGRADED_AFTER_FAILURES

    if not schedules_retry(error_class):
        # No delay is computed on this path at all, so there is nothing to accidentally leak into
        # next_run_at. The provider waits for an operator, not for a timer.
        return RetryDecision(
            next_run_at=None,
            retry_step=retry_step,
            consecutive_failures=failures,
            status=ProviderStatus.DEGRADED,
            error_class=error_class,
            lineage_id=None,
        )

    delay = LADDER[retry_step] if retry_step < len(LADDER) else normal_interval
    if retry_after is not None:
        # The platform's own number wins over ours, but never lets a degraded provider come back
        # faster than its normal interval ( beats  in that one direction).
        delay = max(retry_after, normal_interval) if degraded else retry_after

    return RetryDecision(
        next_run_at=clock.now() + delay,
        retry_step=retry_step + 1,
        consecutive_failures=failures,
        status=ProviderStatus.DEGRADED if degraded else ProviderStatus.IDLE,
        error_class=error_class,
        lineage_id=lineage_id,
    )
