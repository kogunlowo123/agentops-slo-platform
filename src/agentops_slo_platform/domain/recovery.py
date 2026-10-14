"""Recovery analytics and recovery actions.

Retry policies are derived from observed behaviour of a dependency (a tool,
model or memory store), not guessed:

- The *conditional* retry failure probability is measured: of the failed
  calls that were retried under the same parent span, how many retries also
  failed. Failures are often correlated (an outage), so this is usually much
  higher than the unconditional error rate, and it is what decides whether
  retrying helps at all.
- ``max_attempts`` is the smallest k with
  ``p_first * p_retry ** (k - 1) <= target_failure`` (capped), i.e. the
  fewest attempts that bring the residual failure rate to target.
- Timeouts come from the p99 latency of successful calls with headroom;
  backoff starts at half the median latency with full jitter.

Circuit breaking compares a short and a long window: a dependency failing
now beyond the threshold should be opened; one that failed recently but has
recovered (or has stopped receiving traffic) should be probed (half-open).
Recommendations are advisory. Applying one is a recovery *action* that goes
through the recovery policy, and high-risk actions need human approval.
"""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Sequence
from enum import StrEnum
from itertools import pairwise
from typing import Any

from agentic_core.domain.classification import Decision, RiskLevel
from pydantic import BaseModel, ConfigDict, Field, field_validator

from agentops_slo_platform.domain.spans import SpanFact, SpanKind
from agentops_slo_platform.domain.workflows import classify_failure

MAX_ATTEMPTS_CAP = 5
MIN_SAMPLE_FOR_TIMEOUT = 20
MIN_RETRIES_FOR_ESTIMATE = 5


class CircuitState(StrEnum):
    CLOSED = "closed"
    HALF_OPEN = "half_open"
    OPEN = "open"


def percentile(values: Sequence[float], q: float) -> float | None:
    """Linear-interpolated percentile (q in [0, 100]); None for no data."""
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    rank = (q / 100) * (len(ordered) - 1)
    low, high = math.floor(rank), math.ceil(rank)
    return ordered[low] + (ordered[high] - ordered[low]) * (rank - low)


class DependencyStats(BaseModel):
    model_config = ConfigDict(frozen=True)

    kind: SpanKind
    name: str
    calls: int
    failures: int
    error_rate: float
    retryable_failure_share: float | None
    retried_failures: int
    retry_success_rate: float | None
    p50_ms: float | None
    p95_ms: float | None
    p99_ms: float | None


class RetryPolicy(BaseModel):
    model_config = ConfigDict(frozen=True)

    max_attempts: int = Field(ge=1, le=MAX_ATTEMPTS_CAP)
    initial_backoff_ms: int = Field(ge=0)
    backoff_multiplier: float = Field(ge=1)
    max_backoff_ms: int = Field(ge=0)
    jitter: str = "full"
    per_attempt_timeout_ms: int | None = Field(default=None, ge=1)
    rationale: str


class CircuitRecommendation(BaseModel):
    model_config = ConfigDict(frozen=True)

    state: CircuitState
    short_window_minutes: int
    long_window_minutes: int
    short_calls: int
    short_error_rate: float | None
    long_calls: int
    long_error_rate: float | None
    open_threshold: float
    min_calls: int
    reason: str


class RecoveryRecommendation(BaseModel):
    model_config = ConfigDict(frozen=True)

    stats: DependencyStats
    retry_policy: RetryPolicy
    circuit: CircuitRecommendation


def dependency_stats(kind: SpanKind, name: str, calls: Sequence[SpanFact]) -> DependencyStats:
    failures = [c for c in calls if c.is_error]
    retryable = sum(1 for c in failures if classify_failure(c)[1])

    groups: dict[tuple[str, str | None], list[SpanFact]] = defaultdict(list)
    for call in calls:
        groups[(call.trace_id, call.parent_span_id)].append(call)
    retried = recovered = 0
    for group in groups.values():
        group.sort(key=lambda c: (c.start_ns, c.span_id))
        for previous, following in pairwise(group):
            if previous.is_error:
                retried += 1
                recovered += 0 if following.is_error else 1

    latencies = [c.duration_ms for c in calls if not c.is_error]
    return DependencyStats(
        kind=kind,
        name=name,
        calls=len(calls),
        failures=len(failures),
        error_rate=len(failures) / len(calls) if calls else 0.0,
        retryable_failure_share=retryable / len(failures) if failures else None,
        retried_failures=retried,
        retry_success_rate=recovered / retried if retried >= MIN_RETRIES_FOR_ESTIMATE else None,
        p50_ms=percentile(latencies, 50),
        p95_ms=percentile(latencies, 95),
        p99_ms=percentile(latencies, 99),
    )


def _round_up(value: float, step: int) -> int:
    return int(math.ceil(value / step) * step)


def recommend_retry_policy(
    stats: DependencyStats, *, target_failure_rate: float = 0.001
) -> RetryPolicy:
    timeout = (
        max(100, _round_up(stats.p99_ms * 1.5, 100))
        if stats.p99_ms is not None and stats.calls >= MIN_SAMPLE_FOR_TIMEOUT
        else None
    )
    initial = max(50, _round_up((stats.p50_ms or 200.0) / 2, 10))
    max_backoff = max(initial, timeout or 10 * initial)

    def policy(attempts: int, rationale: str) -> RetryPolicy:
        return RetryPolicy(
            max_attempts=attempts,
            initial_backoff_ms=initial,
            backoff_multiplier=2.0,
            max_backoff_ms=max_backoff,
            per_attempt_timeout_ms=timeout,
            rationale=rationale,
        )

    if stats.failures == 0:
        return policy(2, "no failures observed; one retry for transient faults")
    share = stats.retryable_failure_share or 0.0
    if share < 0.2:
        return policy(
            1, f"only {share:.0%} of failures are transient; retries would add load, not success"
        )
    if stats.retry_success_rate is not None and stats.retry_success_rate < 0.1:
        return policy(
            1,
            f"retries succeed {stats.retry_success_rate:.0%} of the time; failures are "
            "correlated (outage), so open the circuit instead of retrying",
        )
    p_first = stats.error_rate * share
    p_retry = (
        1 - stats.retry_success_rate if stats.retry_success_rate is not None else p_first
    ) or 1e-9
    attempts = 1
    residual = p_first
    while residual > target_failure_rate and attempts < MAX_ATTEMPTS_CAP:
        attempts += 1
        residual *= p_retry
    return policy(
        attempts,
        f"first-attempt transient failure {p_first:.2%}, retry failure {p_retry:.2%}; "
        f"{attempts} attempts give residual {residual:.4%} (target {target_failure_rate:.2%})",
    )


def recommend_circuit(
    short: Sequence[SpanFact],
    long: Sequence[SpanFact],
    *,
    short_minutes: int,
    long_minutes: int,
    open_threshold: float = 0.5,
    min_calls: int = 20,
) -> CircuitRecommendation:
    def rate(calls: Sequence[SpanFact]) -> float | None:
        return sum(1 for c in calls if c.is_error) / len(calls) if calls else None

    short_rate, long_rate = rate(short), rate(long)
    if len(short) >= min_calls and short_rate is not None and short_rate >= open_threshold:
        state = CircuitState.OPEN
        reason = f"{short_rate:.0%} of {len(short)} calls failed in the last {short_minutes}m"
    elif len(long) >= min_calls and long_rate is not None and long_rate >= open_threshold:
        state = CircuitState.HALF_OPEN
        reason = (
            f"{long_rate:.0%} failed over {long_minutes}m but the last {short_minutes}m is "
            "below threshold or quiet; probe before restoring full traffic"
        )
    else:
        state = CircuitState.CLOSED
        reason = "error rate below threshold or too few calls to judge"
    return CircuitRecommendation(
        state=state,
        short_window_minutes=short_minutes,
        long_window_minutes=long_minutes,
        short_calls=len(short),
        short_error_rate=short_rate,
        long_calls=len(long),
        long_error_rate=long_rate,
        open_threshold=open_threshold,
        min_calls=min_calls,
        reason=reason,
    )


# ------------------------------------------------------------------ actions


class ActionType(StrEnum):
    APPLY_RETRY_POLICY = "retry_policy.apply"
    OPEN_CIRCUIT = "circuit.open"
    CLOSE_CIRCUIT = "circuit.close"
    PAUSE_AGENT = "agent.pause"
    RAISE_BUDGET = "budget.raise"


ACTION_RISK: dict[ActionType, RiskLevel] = {
    ActionType.APPLY_RETRY_POLICY: RiskLevel.LOW,
    ActionType.CLOSE_CIRCUIT: RiskLevel.MODERATE,
    ActionType.OPEN_CIRCUIT: RiskLevel.HIGH,
    ActionType.RAISE_BUDGET: RiskLevel.HIGH,
    ActionType.PAUSE_AGENT: RiskLevel.CRITICAL,
}


class ActionStatus(StrEnum):
    ACTIVE = "active"  # allowed or approved, within its validity window
    PENDING_APPROVAL = "pending_approval"
    DENIED = "denied"  # policy denied
    REJECTED = "rejected"  # a human declined
    EXPIRED = "expired"


class ActionTarget(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: SpanKind
    name: str = Field(min_length=1, max_length=120)


class ProposeAction(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    action: ActionType
    target: ActionTarget
    parameters: dict[str, Any] = Field(default_factory=dict)
    reason: str = Field(min_length=5, max_length=1000)
    ttl_minutes: int = Field(default=60, ge=1, le=7 * 24 * 60)

    @field_validator("parameters")
    @classmethod
    def _bounded(cls, value: dict[str, Any]) -> dict[str, Any]:
        if len(value) > 20:
            raise ValueError("at most 20 parameters")
        return value


class RecoveryAction(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    action: ActionType
    target: ActionTarget
    parameters: dict[str, Any]
    reason: str
    risk: RiskLevel
    status: ActionStatus
    decision: Decision
    matched_rules: tuple[str, ...]
    policy_version: str
    policy_digest: str
    proposed_by: str
    proposed_ns: int
    decided_by: str | None = None
    decided_ns: int | None = None
    expires_ns: int
    ttl_minutes: int

    def effective_status(self, now_ns: int) -> ActionStatus:
        """Active actions lapse at expiry; pending ones lapse if nobody decides in time."""
        live = (ActionStatus.ACTIVE, ActionStatus.PENDING_APPROVAL)
        if self.status in live and now_ns >= self.expires_ns:
            return ActionStatus.EXPIRED
        return self.status
