"""The ladder is arithmetic over an injected clock, so these tests are arithmetic (Constitution II).

Nothing here sleeps. Every "later" is ``clock.advance(...)``.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from aggregato.domain.enums import ErrorClass, ProviderStatus
from aggregato.sync import retry
from aggregato.sync.retry import (
    DEGRADED_AFTER_FAILURES,
    LADDER,
    NeverRetryScheduledError,
    RetryDecision,
    lineage_id_for,
    plan_after_failure,
    plan_after_success,
)
from tests.conftest import FrozenClock

# Deliberately longer than the last ladder rung, so "fell back to the normal interval" and "took the
# 1h rung" cannot be confused for one another.
NORMAL = timedelta(hours=6)


def test_ladder_is_one_five_fifteen_minutes_then_one_hour() -> None:
    assert list(LADDER) == [
        timedelta(minutes=1),
        timedelta(minutes=5),
        timedelta(minutes=15),
        timedelta(hours=1),
    ]


def test_four_transport_failures_walk_the_ladder_in_order(clock: FrozenClock) -> None:
    lineage = lineage_id_for(None)
    step = 0
    failures = 0
    for expected in LADDER:
        started = clock.now()
        decision = plan_after_failure(
            clock=clock,
            error_class=ErrorClass.TRANSPORT,
            retry_step=step,
            consecutive_failures=failures,
            normal_interval=NORMAL,
            lineage_id=lineage,
        )
        assert decision.next_run_at == started + expected
        step, failures = decision.retry_step, decision.consecutive_failures
        clock.advance(expected)  # no sleeping: the retry "happens" by moving the clock

    assert step == len(LADDER)
    assert failures == len(LADDER)


def test_step_four_and_beyond_fall_back_to_the_normal_interval(clock: FrozenClock) -> None:
    for step in (len(LADDER), len(LADDER) + 3):
        decision = plan_after_failure(
            clock=clock,
            error_class=ErrorClass.TRANSPORT,
            retry_step=step,
            consecutive_failures=step,
            normal_interval=NORMAL,
            lineage_id=uuid4(),
        )
        assert decision.next_run_at == clock.now() + NORMAL


def test_success_resets_the_step_and_the_failure_count(clock: FrozenClock) -> None:
    decision = plan_after_success(clock=clock, normal_interval=NORMAL)
    assert decision.retry_step == 0
    assert decision.consecutive_failures == 0
    assert decision.status is ProviderStatus.IDLE
    assert decision.next_run_at == clock.now() + NORMAL


def test_retries_of_one_logical_run_share_a_lineage(clock: FrozenClock) -> None:
    lineage = lineage_id_for(None)
    seen = {lineage}
    step = 0
    for _ in LADDER:
        decision = plan_after_failure(
            clock=clock,
            error_class=ErrorClass.TRANSPORT,
            retry_step=step,
            consecutive_failures=step,
            normal_interval=NORMAL,
            lineage_id=lineage,
        )
        assert decision.lineage_id is not None
        # FR-019: four attempts must read as one failing sync, not four unrelated failures.
        seen.add(lineage_id_for(decision.lineage_id))
        step = decision.retry_step
        clock.advance(timedelta(minutes=1))

    assert seen == {lineage}


def test_a_run_that_is_not_a_retry_gets_a_fresh_lineage() -> None:
    assert lineage_id_for(None) != lineage_id_for(None)


def test_success_drops_the_lineage_so_the_next_run_starts_new_work(clock: FrozenClock) -> None:
    decision = plan_after_success(clock=clock, normal_interval=NORMAL)
    assert decision.lineage_id is None
    assert lineage_id_for(decision.lineage_id) != lineage_id_for(decision.lineage_id)


def test_degraded_threshold_holds_at_the_normal_interval_and_never_faster(
    clock: FrozenClock,
) -> None:
    step = failures = DEGRADED_AFTER_FAILURES - 1
    decision = plan_after_failure(
        clock=clock,
        error_class=ErrorClass.TRANSPORT,
        retry_step=step,
        consecutive_failures=failures,
        normal_interval=NORMAL,
        lineage_id=uuid4(),
    )
    assert decision.consecutive_failures == DEGRADED_AFTER_FAILURES
    assert decision.status is ProviderStatus.DEGRADED
    # FR-021: the ladder stops accelerating once degraded.
    assert decision.next_run_at == clock.now() + NORMAL


def test_degraded_provider_ignores_a_retry_after_that_would_be_faster(clock: FrozenClock) -> None:
    decision = plan_after_failure(
        clock=clock,
        error_class=ErrorClass.RATE_LIMIT,
        retry_step=DEGRADED_AFTER_FAILURES,
        consecutive_failures=DEGRADED_AFTER_FAILURES,
        normal_interval=NORMAL,
        lineage_id=uuid4(),
        retry_after=timedelta(seconds=10),
    )
    assert decision.status is ProviderStatus.DEGRADED
    assert decision.next_run_at == clock.now() + NORMAL


def test_explicit_retry_after_is_honoured_over_the_ladder_step(clock: FrozenClock) -> None:
    decision = plan_after_failure(
        clock=clock,
        error_class=ErrorClass.RATE_LIMIT,
        retry_step=0,
        consecutive_failures=0,
        normal_interval=NORMAL,
        lineage_id=uuid4(),
        retry_after=timedelta(seconds=90),
    )
    # FR-022: the platform's number wins over our 1m first rung, longer or shorter.
    assert decision.next_run_at == clock.now() + timedelta(seconds=90)
    assert decision.status is ProviderStatus.IDLE


@pytest.mark.parametrize(
    "error_class", [ErrorClass.AUTH, ErrorClass.BLOCKED, ErrorClass.STRUCTURE_CHANGED]
)
def test_never_retry_classes_get_no_retry_time_and_degrade_immediately(
    clock: FrozenClock, error_class: ErrorClass
) -> None:
    decision = plan_after_failure(
        clock=clock,
        error_class=error_class,
        retry_step=0,
        consecutive_failures=0,
        normal_interval=NORMAL,
        lineage_id=uuid4(),
        retry_after=timedelta(seconds=5),  # even an explicit delay must not create a retry
    )
    assert decision.next_run_at is None
    assert decision.status is ProviderStatus.DEGRADED


@pytest.mark.parametrize(
    "error_class", [ErrorClass.AUTH, ErrorClass.BLOCKED, ErrorClass.STRUCTURE_CHANGED]
)
def test_a_never_retry_decision_with_a_retry_time_cannot_be_constructed(
    clock: FrozenClock, error_class: ErrorClass
) -> None:
    # Structural, not merely tested: the invalid state is rejected at construction, in production
    # too, so a future edit to the ladder cannot quietly schedule one of these.
    with pytest.raises(NeverRetryScheduledError):
        RetryDecision(
            next_run_at=clock.now() + timedelta(minutes=1),
            retry_step=1,
            consecutive_failures=1,
            status=ProviderStatus.DEGRADED,
            error_class=error_class,
            lineage_id=None,
        )


def test_the_ladder_never_reads_a_wall_clock() -> None:
    # Constitution II: time is injected. Every "later" in this file is clock.advance(), which is
    # only possible while the ladder takes its now() from the Clock it is handed.
    assert "datetime.now" not in Path(retry.__file__).read_text(encoding="utf-8")
