"""Workflow (trace) success and failure classification.

A workflow run is one trace. Once the trace is complete, the classifier
decides the outcome and, for failures, the root cause:

- ``success``: the root span succeeded and no span failed.
- ``recovered``: spans failed but the root succeeded (retries, fallbacks).
- ``failed``: the root span failed, or a runaway loop was detected.
- ``incomplete``: no root span arrived before the wait limit.

The root cause is the failed span deepest in the tree (a failed span with no
failed descendants), earliest to end on ties. It is assigned a failure class
from deterministic rules over error type, HTTP status, finish reasons and
span kind, and each class maps to a recovery strategy.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Sequence
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from agentops_slo_platform.domain.spans import SpanFact, SpanKind


class Outcome(StrEnum):
    SUCCESS = "success"
    RECOVERED = "recovered"
    FAILED = "failed"
    INCOMPLETE = "incomplete"


GOOD_OUTCOMES = frozenset({Outcome.SUCCESS, Outcome.RECOVERED})


class FailureClass(StrEnum):
    GUARDRAIL_BLOCKED = "guardrail_blocked"
    RATE_LIMITED = "rate_limited"
    TIMEOUT = "timeout"
    AUTHORIZATION_DENIED = "authorization_denied"
    BUDGET_EXCEEDED = "budget_exceeded"
    LOOP_DETECTED = "loop_detected"
    TOOL_ERROR = "tool_error"
    MODEL_ERROR = "model_error"
    MEMORY_ERROR = "memory_error"
    AGENT_ERROR = "agent_error"
    UNKNOWN = "unknown"


class RecoveryStrategy(StrEnum):
    RETRY_WITH_BACKOFF = "retry_with_backoff"
    RETRY_AFTER_HINT = "retry_honoring_retry_after"
    FAILOVER = "failover_to_alternate_dependency"
    OPEN_CIRCUIT = "open_circuit_on_dependency"
    FIX_INPUT = "fix_input_no_retry"
    HUMAN_REVIEW = "escalate_to_human_review"
    TERMINATE = "terminate_and_cap_iterations"
    RAISE_BUDGET_WITH_APPROVAL = "raise_budget_with_approval"
    CHECK_GRANTS = "check_agent_grants_no_retry"


_STRATEGIES: dict[FailureClass, tuple[RecoveryStrategy, ...]] = {
    FailureClass.GUARDRAIL_BLOCKED: (RecoveryStrategy.HUMAN_REVIEW,),
    FailureClass.RATE_LIMITED: (RecoveryStrategy.RETRY_AFTER_HINT, RecoveryStrategy.FAILOVER),
    FailureClass.TIMEOUT: (RecoveryStrategy.RETRY_WITH_BACKOFF, RecoveryStrategy.OPEN_CIRCUIT),
    FailureClass.AUTHORIZATION_DENIED: (RecoveryStrategy.CHECK_GRANTS,),
    FailureClass.BUDGET_EXCEEDED: (RecoveryStrategy.RAISE_BUDGET_WITH_APPROVAL,),
    FailureClass.LOOP_DETECTED: (RecoveryStrategy.TERMINATE,),
    FailureClass.TOOL_ERROR: (RecoveryStrategy.FIX_INPUT,),
    FailureClass.MODEL_ERROR: (RecoveryStrategy.RETRY_WITH_BACKOFF, RecoveryStrategy.FAILOVER),
    FailureClass.MEMORY_ERROR: (RecoveryStrategy.RETRY_WITH_BACKOFF,),
    FailureClass.AGENT_ERROR: (RecoveryStrategy.HUMAN_REVIEW,),
    FailureClass.UNKNOWN: (RecoveryStrategy.HUMAN_REVIEW,),
}

_GUARDRAIL = re.compile(r"guardrail|content[_ ]?filter|safety|policy[_ ]?violation", re.I)
_RATE = re.compile(r"rate[_ ]?limit|throttl|too[_ ]?many[_ ]?requests|resource[_ ]?exhausted", re.I)
_TIMEOUT = re.compile(r"time[_ ]?out|deadline", re.I)
_AUTHZ = re.compile(r"unauthori[sz]ed|forbidden|permission|access[_ ]?denied", re.I)
_BUDGET = re.compile(r"budget|quota|max[_ ]?tokens|insufficient[_ ]?credit", re.I)
_LOOP = re.compile(r"max[_ ]?(iterations|turns|steps)|recursion", re.I)


class Diagnosis(BaseModel):
    model_config = ConfigDict(frozen=True)

    trace_id: str
    workflow: str
    agent: str
    team: str
    outcome: Outcome
    failure_class: FailureClass | None = None
    root_cause_span_id: str | None = None
    root_cause_kind: SpanKind | None = None
    root_cause_name: str | None = None
    retryable: bool = False
    observed_failure_classes: tuple[FailureClass, ...] = ()
    recovery: tuple[RecoveryStrategy, ...] = ()
    span_count: int = Field(ge=0)
    error_count: int = Field(ge=0)
    tool_calls: int = Field(ge=0)
    model_calls: int = Field(ge=0)
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    cost_usd: float = Field(ge=0)
    unpriced_model_calls: int = Field(ge=0)
    started_ns: int
    ended_ns: int


def classify_failure(span: SpanFact) -> tuple[FailureClass, bool]:  # noqa: PLR0911
    """Failure class of a failed span and whether a retry can help."""
    text = " ".join(filter(None, (span.error_type, span.error_message)))
    status = span.http_status
    if "content_filter" in span.finish_reasons or _GUARDRAIL.search(text):
        return FailureClass.GUARDRAIL_BLOCKED, False
    if status == 429 or _RATE.search(text):
        return FailureClass.RATE_LIMITED, True
    if status in (408, 504) or _TIMEOUT.search(text):
        return FailureClass.TIMEOUT, True
    if status in (401, 403) or _AUTHZ.search(text):
        return FailureClass.AUTHORIZATION_DENIED, False
    if status == 402 or _BUDGET.search(text):
        return FailureClass.BUDGET_EXCEEDED, False
    if _LOOP.search(text):
        return FailureClass.LOOP_DETECTED, False
    server_side = status is not None and status >= 500
    match span.kind:
        case SpanKind.TOOL:
            return FailureClass.TOOL_ERROR, server_side
        case SpanKind.MODEL:
            # Provider errors without a client-side status are transient more often than not.
            return FailureClass.MODEL_ERROR, status is None or server_side
        case SpanKind.MEMORY:
            return FailureClass.MEMORY_ERROR, status is None or server_side
        case SpanKind.AGENT:
            return FailureClass.AGENT_ERROR, False
    return FailureClass.UNKNOWN, server_side


def recovery_for(failure: FailureClass) -> tuple[RecoveryStrategy, ...]:
    return _STRATEGIES[failure]


def _root_cause(spans: Sequence[SpanFact]) -> SpanFact | None:
    failed = [s for s in spans if s.is_error]
    if not failed:
        return None
    failed_ids = {s.span_id for s in failed}
    parents = {s.span_id: s.parent_span_id for s in spans}
    has_failed_child = set()
    for span in failed:
        parent = span.parent_span_id
        hops = 0
        while parent is not None and hops < 64:
            if parent in failed_ids:
                has_failed_child.add(parent)
            parent = parents.get(parent)
            hops += 1
    leaves = [s for s in failed if s.span_id not in has_failed_child]
    return min(leaves, key=lambda s: (s.end_ns, s.span_id))


def _loop(spans: Sequence[SpanFact], threshold: int) -> SpanFact | None:
    """A tool called more than ``threshold`` times under the same parent."""
    calls = Counter((s.parent_span_id, s.name) for s in spans if s.kind is SpanKind.TOOL)
    for (parent, name), count in calls.most_common(1):
        if count > threshold:
            return next(
                s
                for s in spans
                if s.kind is SpanKind.TOOL and s.parent_span_id == parent and s.name == name
            )
    return None


def diagnose(spans: Sequence[SpanFact], *, complete: bool, loop_threshold: int = 25) -> Diagnosis:
    """Classify one trace. ``complete`` is False when the wait limit expired without a root."""
    if not spans:
        raise ValueError("cannot diagnose an empty trace")
    ordered = sorted(spans, key=lambda s: (s.start_ns, s.span_id))
    roots = [s for s in ordered if s.is_root]
    root = roots[0] if roots else ordered[0]
    agent = next((s.agent for s in ordered if s.kind is SpanKind.AGENT), root.agent)
    workflow = root.workflow or next((s.workflow for s in ordered if s.workflow), None)
    errors = [s for s in ordered if s.is_error]
    model_spans = [s for s in ordered if s.kind is SpanKind.MODEL]

    observed = tuple(sorted({classify_failure(s)[0] for s in errors}))
    base: dict[str, object] = {
        "trace_id": root.trace_id,
        "workflow": workflow or root.name,
        "agent": agent,
        "team": root.team,
        "observed_failure_classes": observed,
        "span_count": len(ordered),
        "error_count": len(errors),
        "tool_calls": sum(1 for s in ordered if s.kind is SpanKind.TOOL),
        "model_calls": len(model_spans),
        "input_tokens": sum(s.input_tokens for s in ordered),
        "output_tokens": sum(s.output_tokens for s in ordered),
        "cost_usd": round(sum(s.cost_usd or 0.0 for s in ordered), 8),
        "unpriced_model_calls": sum(
            1 for s in model_spans if s.cost_usd is None and s.input_tokens + s.output_tokens
        ),
        "started_ns": min(s.start_ns for s in ordered),
        "ended_ns": max(s.end_ns for s in ordered),
    }

    if not roots and not complete:
        return Diagnosis.model_validate({**base, "outcome": Outcome.INCOMPLETE})

    looping = _loop(ordered, loop_threshold)
    if looping is not None:
        return Diagnosis.model_validate(
            {
                **base,
                "outcome": Outcome.FAILED,
                "failure_class": FailureClass.LOOP_DETECTED,
                "root_cause_span_id": looping.span_id,
                "root_cause_kind": looping.kind,
                "root_cause_name": looping.name,
                "retryable": False,
                "recovery": recovery_for(FailureClass.LOOP_DETECTED),
            }
        )

    if not root.is_error:
        outcome = Outcome.RECOVERED if errors else Outcome.SUCCESS
        return Diagnosis.model_validate({**base, "outcome": outcome})

    cause = _root_cause(ordered) or root
    failure, retryable = classify_failure(cause)
    return Diagnosis.model_validate(
        {
            **base,
            "outcome": Outcome.FAILED,
            "failure_class": failure,
            "root_cause_span_id": cause.span_id,
            "root_cause_kind": cause.kind,
            "root_cause_name": cause.name,
            "retryable": retryable,
            "recovery": recovery_for(failure),
        }
    )
