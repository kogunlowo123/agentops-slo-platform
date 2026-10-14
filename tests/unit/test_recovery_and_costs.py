"""Retry-policy derivation, circuit recommendations, action lifecycle and pricing."""

from __future__ import annotations

import pytest
from agentic_core.domain.classification import Decision, RiskLevel
from pydantic import ValidationError

from agentops_slo_platform.domain.costs import PriceBook, PriceEntry
from agentops_slo_platform.domain.recovery import (
    ActionStatus,
    ActionTarget,
    ActionType,
    CircuitState,
    DependencyStats,
    ProposeAction,
    RecoveryAction,
    dependency_stats,
    percentile,
    recommend_circuit,
    recommend_retry_policy,
)
from agentops_slo_platform.domain.spans import SpanKind
from tests.factories import fact


def stats(**overrides: object) -> DependencyStats:
    values: dict[str, object] = {
        "kind": SpanKind.TOOL,
        "name": "crm.lookup",
        "calls": 1000,
        "failures": 0,
        "error_rate": 0.0,
        "retryable_failure_share": None,
        "retried_failures": 0,
        "retry_success_rate": None,
        "p50_ms": 120.0,
        "p95_ms": 400.0,
        "p99_ms": 900.0,
    }
    values.update(overrides)
    return DependencyStats.model_validate(values)


def test_percentile() -> None:
    assert percentile([], 50) is None
    assert percentile([7.0], 99) == 7.0
    assert percentile([1.0, 2.0, 3.0, 4.0], 50) == 2.5
    assert percentile([10.0, 0.0], 100) == 10.0


def test_dependency_stats_measures_conditional_retry_success() -> None:
    calls = []
    for i in range(6):  # six retried failures under distinct parents; four recover
        parent = f"{i:016x}"
        trace = f"{i:032x}"
        calls.append(fact(trace_id=trace, parent=parent, start_ns=0, error=True, http_status=503))
        calls.append(fact(trace_id=trace, parent=parent, start_ns=10**9, error=i >= 4))
    calls.append(fact(start_ns=0, duration_ms=50))
    result = dependency_stats(SpanKind.TOOL, "crm.lookup", calls)
    assert result.calls == 13
    assert result.failures == 8
    assert result.retried_failures == 6
    assert result.retry_success_rate == pytest.approx(4 / 6)
    assert result.retryable_failure_share == pytest.approx(6 / 8)
    assert result.p50_ms == 100.0


def test_dependency_stats_without_data() -> None:
    empty = dependency_stats(SpanKind.MODEL, "m", [])
    assert empty.error_rate == 0.0
    assert empty.retry_success_rate is None
    assert empty.p99_ms is None


def test_retry_policy_without_failures() -> None:
    policy = recommend_retry_policy(stats())
    assert policy.max_attempts == 2
    assert policy.per_attempt_timeout_ms == 1400  # p99 900 ms * 1.5 rounded up to 100 ms
    assert policy.initial_backoff_ms == 60
    assert policy.max_backoff_ms == 1400


def test_retry_policy_skips_retries_for_permanent_failures() -> None:
    policy = recommend_retry_policy(
        stats(failures=50, error_rate=0.05, retryable_failure_share=0.1)
    )
    assert policy.max_attempts == 1
    assert "transient" in policy.rationale


def test_retry_policy_skips_retries_during_correlated_outage() -> None:
    policy = recommend_retry_policy(
        stats(
            failures=500,
            error_rate=0.5,
            retryable_failure_share=1.0,
            retried_failures=400,
            retry_success_rate=0.05,
        )
    )
    assert policy.max_attempts == 1
    assert "circuit" in policy.rationale


def test_retry_policy_attempts_reach_target() -> None:
    # 5% transient first-attempt failures, retries fail 20% of the time:
    # 0.05 -> 0.01 -> 0.002 -> 0.0004 (<= 0.001 target) => 4 attempts.
    policy = recommend_retry_policy(
        stats(
            failures=50,
            error_rate=0.05,
            retryable_failure_share=1.0,
            retried_failures=50,
            retry_success_rate=0.8,
        )
    )
    assert policy.max_attempts == 4
    # Without a measured retry success rate the first-attempt rate is reused.
    unmeasured = recommend_retry_policy(
        stats(failures=50, error_rate=0.05, retryable_failure_share=1.0, calls=10, p99_ms=None)
    )
    assert unmeasured.max_attempts == 3
    assert unmeasured.per_attempt_timeout_ms is None


def test_circuit_states() -> None:
    failing = [fact(error=True) for _ in range(25)]
    healthy = [fact() for _ in range(25)]
    opened = recommend_circuit(failing, failing, short_minutes=5, long_minutes=30)
    assert opened.state is CircuitState.OPEN
    half = recommend_circuit(healthy[:3], failing, short_minutes=5, long_minutes=30)
    assert half.state is CircuitState.HALF_OPEN
    closed = recommend_circuit(healthy, healthy, short_minutes=5, long_minutes=30)
    assert closed.state is CircuitState.CLOSED
    assert closed.short_error_rate == 0.0
    quiet = recommend_circuit([], [], short_minutes=5, long_minutes=30)
    assert quiet.state is CircuitState.CLOSED
    assert quiet.short_error_rate is None


def test_action_expiry() -> None:
    action = RecoveryAction(
        id="a" * 32,
        action=ActionType.OPEN_CIRCUIT,
        target=ActionTarget(kind=SpanKind.TOOL, name="crm.lookup"),
        parameters={},
        reason="tool failing",
        risk=RiskLevel.HIGH,
        status=ActionStatus.PENDING_APPROVAL,
        decision=Decision.REQUIRE_APPROVAL,
        matched_rules=(),
        policy_version="1",
        policy_digest="sha256:x",
        proposed_by="alice",
        proposed_ns=0,
        expires_ns=100,
        ttl_minutes=1,
    )
    assert action.effective_status(99) is ActionStatus.PENDING_APPROVAL
    assert action.effective_status(100) is ActionStatus.EXPIRED
    rejected = action.model_copy(update={"status": ActionStatus.REJECTED})
    assert rejected.effective_status(10**12) is ActionStatus.REJECTED


def test_propose_action_bounds_parameters() -> None:
    with pytest.raises(ValidationError, match="at most 20"):
        ProposeAction(
            action=ActionType.OPEN_CIRCUIT,
            target=ActionTarget(kind=SpanKind.TOOL, name="t"),
            parameters={str(i): i for i in range(21)},
            reason="too many",
        )


def test_price_entry_cost_with_cache_reads() -> None:
    price = PriceEntry(
        model="gpt-4.1",
        input_per_mtok=2.0,
        output_per_mtok=8.0,
        cached_input_per_mtok=0.5,
        effective_from_ns=0,
    )
    # 1M input of which 400k cached, 100k output.
    assert price.cost(1_000_000, 100_000, 400_000) == pytest.approx(1.2 + 0.2 + 0.8)
    uncached = price.model_copy(update={"cached_input_per_mtok": None})
    assert uncached.cost(1_000_000, 0, 400_000) == pytest.approx(2.0)
    # Cached tokens can never exceed input tokens.
    assert price.cost(100, 0, 10_000) == pytest.approx(100 * 0.5 / 1_000_000)


def test_price_book_is_effective_dated() -> None:
    old = PriceEntry(model="m", input_per_mtok=1, output_per_mtok=1, effective_from_ns=100)
    new = PriceEntry(model="m", input_per_mtok=2, output_per_mtok=2, effective_from_ns=200)
    book = PriceBook([new, old])
    assert book.price("m", 50) is None
    assert book.price("m", 150) == old
    assert book.price("m", 200) == new
    assert book.price("other", 500) is None
    assert book.price(None, 500) is None
    assert book.entries() == [old, new]
