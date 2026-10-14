"""Workflow outcome classification and root-cause analysis."""

from __future__ import annotations

from typing import Any

import pytest

from agentops_slo_platform.domain.spans import SpanFact, SpanKind
from agentops_slo_platform.domain.workflows import (
    FailureClass,
    Outcome,
    RecoveryStrategy,
    classify_failure,
    diagnose,
    recovery_for,
)
from tests.factories import fact

TRACE = "9" * 32


def trace(*children: SpanFact, root_error: bool = False, **root: Any) -> list[SpanFact]:
    root_span = fact(
        kind=SpanKind.AGENT,
        name="claims-triage",
        trace_id=TRACE,
        span_id="0" * 15 + "1",
        parent=None,
        error=root_error,
        workflow="claims-intake",
        **root,
    )
    return [root_span, *children]


def child(span_id: str, *, parent: str = "0" * 15 + "1", **kw: Any) -> SpanFact:
    kw.setdefault("start_ns", int(span_id, 16) * 1000)
    return fact(trace_id=TRACE, span_id=span_id.rjust(16, "0"), parent=parent.rjust(16, "0"), **kw)


def test_success() -> None:
    d = diagnose(trace(child("2"), child("3", kind=SpanKind.MODEL, name="m")), complete=True)
    assert d.outcome is Outcome.SUCCESS
    assert d.failure_class is None
    assert d.recovery == ()
    assert (d.workflow, d.agent, d.span_count, d.tool_calls, d.model_calls) == (
        "claims-intake",
        "claims-triage",
        3,
        1,
        1,
    )


def test_recovered_when_root_succeeds_despite_failures() -> None:
    d = diagnose(trace(child("2", error=True, http_status=503), child("3")), complete=True)
    assert d.outcome is Outcome.RECOVERED
    assert d.observed_failure_classes == (FailureClass.TOOL_ERROR,)
    assert d.error_count == 1


def test_root_cause_is_deepest_failed_span() -> None:
    spans = trace(
        child("2", kind=SpanKind.AGENT, name="sub-agent", error=True),
        child("3", parent="2", kind=SpanKind.MODEL, name="gpt", error=True, http_status=429),
        child("4", parent="2", name="other-tool"),
        root_error=True,
    )
    d = diagnose(spans, complete=True)
    assert d.outcome is Outcome.FAILED
    assert d.failure_class is FailureClass.RATE_LIMITED
    assert d.root_cause_span_id == "3".rjust(16, "0")
    assert d.root_cause_kind is SpanKind.MODEL
    assert d.retryable
    assert d.recovery == (RecoveryStrategy.RETRY_AFTER_HINT, RecoveryStrategy.FAILOVER)


def test_root_failure_without_failed_children_blames_root() -> None:
    d = diagnose(trace(child("2"), root_error=True), complete=True)
    assert d.failure_class is FailureClass.AGENT_ERROR
    assert d.root_cause_span_id == "0" * 15 + "1"


def test_incomplete_and_orphan_traces() -> None:
    orphan = [child("2"), child("3")]
    assert diagnose(orphan, complete=False).outcome is Outcome.INCOMPLETE
    # Abandoned without root, but wait limit expired with complete=True: judged on spans seen.
    judged = diagnose(orphan, complete=True)
    assert judged.outcome is Outcome.SUCCESS
    assert judged.workflow == "crm.lookup"


def test_loop_detection() -> None:
    calls = [child(f"{i + 2:x}", name="search") for i in range(6)]
    d = diagnose(trace(*calls), complete=True, loop_threshold=5)
    assert d.outcome is Outcome.FAILED
    assert d.failure_class is FailureClass.LOOP_DETECTED
    assert d.root_cause_name == "search"
    assert not d.retryable


def test_totals_and_unpriced_calls() -> None:
    spans = trace(
        child("2", kind=SpanKind.MODEL, name="m", input_tokens=100, output_tokens=10, cost_usd=0.5),
        child("3", kind=SpanKind.MODEL, name="m", input_tokens=50, output_tokens=5),
        child("4", kind=SpanKind.MODEL, name="m"),
    )
    d = diagnose(spans, complete=True)
    assert (d.input_tokens, d.output_tokens, d.cost_usd, d.unpriced_model_calls) == (
        150,
        15,
        0.5,
        1,
    )


def test_empty_trace_rejected() -> None:
    with pytest.raises(ValueError, match="empty"):
        diagnose([], complete=True)


@pytest.mark.parametrize(
    ("overrides", "expected", "retryable"),
    [
        ({"finish_reasons": ("content_filter",)}, FailureClass.GUARDRAIL_BLOCKED, False),
        ({"error_type": "GuardrailIntervened"}, FailureClass.GUARDRAIL_BLOCKED, False),
        ({"http_status": 429}, FailureClass.RATE_LIMITED, True),
        ({"error_message": "RESOURCE_EXHAUSTED: quota"}, FailureClass.RATE_LIMITED, True),
        ({"error_type": "ReadTimeout"}, FailureClass.TIMEOUT, True),
        ({"http_status": 504}, FailureClass.TIMEOUT, True),
        ({"http_status": 403}, FailureClass.AUTHORIZATION_DENIED, False),
        ({"error_message": "permission denied for tool"}, FailureClass.AUTHORIZATION_DENIED, False),
        ({"error_type": "TokenBudgetExceeded"}, FailureClass.BUDGET_EXCEEDED, False),
        ({"http_status": 402}, FailureClass.BUDGET_EXCEEDED, False),
        ({"error_type": "MaxIterationsReached"}, FailureClass.LOOP_DETECTED, False),
        ({"kind": SpanKind.TOOL, "http_status": 400}, FailureClass.TOOL_ERROR, False),
        ({"kind": SpanKind.TOOL, "http_status": 502}, FailureClass.TOOL_ERROR, True),
        ({"kind": SpanKind.MODEL}, FailureClass.MODEL_ERROR, True),
        ({"kind": SpanKind.MODEL, "http_status": 400}, FailureClass.MODEL_ERROR, False),
        ({"kind": SpanKind.MEMORY}, FailureClass.MEMORY_ERROR, True),
        ({"kind": SpanKind.AGENT}, FailureClass.AGENT_ERROR, False),
        ({"kind": SpanKind.OTHER, "http_status": 500}, FailureClass.UNKNOWN, True),
        ({"kind": SpanKind.WORKFLOW}, FailureClass.UNKNOWN, False),
    ],
)
def test_failure_classes(
    overrides: dict[str, Any], expected: FailureClass, retryable: bool
) -> None:
    overrides.setdefault("kind", SpanKind.TOOL)
    assert classify_failure(fact(error=True, **overrides)) == (expected, retryable)


def test_every_failure_class_has_a_recovery_strategy() -> None:
    assert all(recovery_for(f) for f in FailureClass)
