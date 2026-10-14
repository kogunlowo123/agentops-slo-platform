"""Span classification and attribute handling per the GenAI semantic conventions."""

from __future__ import annotations

from typing import Any

import pytest

from agentops_slo_platform.domain.spans import (
    SpanEvent,
    SpanKind,
    SpanRecord,
    StatusCode,
    ancestry_attribute,
    classify_kind,
    exception_details,
    finish_reasons,
    http_status,
    is_error,
    label_safe,
    retry_attempt,
    subject_name,
    token_counts,
    trace_ids,
)
from tests.factories import fact


def record(
    attributes: dict[str, Any] | None = None,
    *,
    parent: str | None = "a" * 16,
    status: StatusCode = StatusCode.UNSET,
    name: str = "span",
    resource: dict[str, Any] | None = None,
    events: tuple[SpanEvent, ...] = (),
) -> SpanRecord:
    return SpanRecord(
        trace_id="1" * 32,
        span_id="2" * 16,
        parent_span_id=parent,
        name=name,
        start_ns=0,
        end_ns=10,
        status=status,
        attributes=attributes or {},
        resource=resource or {},
        events=events,
    )


@pytest.mark.parametrize(
    ("attributes", "parent", "expected"),
    [
        ({"gen_ai.operation.name": "invoke_agent"}, "a" * 16, SpanKind.AGENT),
        ({"gen_ai.operation.name": "create_agent"}, "a" * 16, SpanKind.AGENT),
        ({"gen_ai.operation.name": "execute_tool"}, "a" * 16, SpanKind.TOOL),
        ({"gen_ai.operation.name": "chat"}, "a" * 16, SpanKind.MODEL),
        ({"gen_ai.operation.name": "embeddings"}, "a" * 16, SpanKind.MODEL),
        ({"gen_ai.operation.name": "search_memory"}, "a" * 16, SpanKind.MEMORY),
        ({"db.system.name": "qdrant"}, "a" * 16, SpanKind.MEMORY),
        ({"db.system": "Pinecone"}, "a" * 16, SpanKind.MEMORY),
        ({"db.system.name": "postgresql"}, "a" * 16, SpanKind.OTHER),
        ({"gen_ai.operation.name": "invoke_workflow"}, "a" * 16, SpanKind.WORKFLOW),
        ({}, None, SpanKind.WORKFLOW),
        ({"gen_ai.operation.name": "invoke_agent"}, None, SpanKind.AGENT),
        ({}, "a" * 16, SpanKind.OTHER),
        ({"agentops.span.kind": "memory", "gen_ai.operation.name": "chat"}, None, SpanKind.MEMORY),
        ({"agentops.span.kind": "bogus", "gen_ai.operation.name": "chat"}, None, SpanKind.MODEL),
    ],
)
def test_classify_kind(attributes: dict[str, Any], parent: str | None, expected: SpanKind) -> None:
    assert classify_kind(record(attributes, parent=parent)) is expected


def test_subject_names_per_kind() -> None:
    assert subject_name(record({"gen_ai.tool.name": "crm.lookup"}), SpanKind.TOOL) == "crm.lookup"
    assert subject_name(record(name="fallback"), SpanKind.TOOL) == "fallback"
    model = record({"gen_ai.request.model": "m-req", "gen_ai.response.model": "m-resp"})
    assert subject_name(model, SpanKind.MODEL) == "m-resp"
    assert subject_name(record(), SpanKind.MODEL) == "unknown-model"
    assert subject_name(record({"db.operation.name": "query"}), SpanKind.MEMORY) == "query"
    assert subject_name(record({"gen_ai.agent.id": "a-1"}), SpanKind.AGENT) == "a-1"
    assert subject_name(record({"agentops.workflow.name": "claims"}), SpanKind.WORKFLOW) == "claims"
    assert subject_name(record(name="x"), SpanKind.OTHER) == "x"


def test_resource_attributes_are_a_fallback() -> None:
    span = record({"agentops.team": "span-team"}, resource={"agentops.team": "res-team"})
    assert span.attr("agentops.team") == "span-team"
    assert record(resource={"agentops.team": "res-team"}).attr("agentops.team") == "res-team"
    assert record().attr("missing") is None


def test_label_safe_bounds_and_cleans() -> None:
    assert label_safe(None) == "unattributed"
    assert label_safe("  ") == "unattributed"
    assert label_safe("a\nb\x00c") == "a_b_c"
    assert len(label_safe("x" * 500)) == 120
    assert label_safe("ünïcode") == "_n_code"


def test_token_counts_current_and_legacy() -> None:
    current = record(
        {
            "gen_ai.usage.input_tokens": 100,
            "gen_ai.usage.output_tokens": 20,
            "gen_ai.usage.cache_read.input_tokens": 50,
        }
    )
    assert token_counts(current) == (100, 20, 50)
    legacy = record({"gen_ai.usage.prompt_tokens": "7", "gen_ai.usage.completion_tokens": 3.0})
    assert token_counts(legacy) == (7, 3, 0)
    weird = record({"gen_ai.usage.input_tokens": True, "gen_ai.usage.output_tokens": -5})
    assert token_counts(weird) == (0, 0, 0)
    assert token_counts(record({"gen_ai.usage.input_tokens": 1.5})) == (0, 0, 0)


def test_errors_status_and_exceptions() -> None:
    assert is_error(record(status=StatusCode.ERROR))
    assert not is_error(record({"error.type": "x"}, status=StatusCode.OK))
    assert is_error(record({"error.type": "TimeoutError"}))
    assert not is_error(record())
    exc = SpanEvent(name="exception", attributes={"exception.type": "E", "exception.message": "m"})
    assert exception_details(record(events=(SpanEvent(name="log"), exc))) == ("E", "m")
    assert exception_details(record(events=(SpanEvent(name="exception"),))) == (None, None)
    assert exception_details(record()) == (None, None)


def test_http_retry_and_finish_reasons() -> None:
    assert http_status(record({"http.response.status_code": 429})) == 429
    assert http_status(record()) is None
    assert retry_attempt(record({"agentops.retry.attempt": 2})) == 2
    assert retry_attempt(record({"http.request.resend_count": 1})) == 1
    assert finish_reasons(record({"gen_ai.response.finish_reasons": ["stop"]})) == ("stop",)
    assert finish_reasons(record({"gen_ai.response.finish_reasons": "length"})) == ("length",)
    assert finish_reasons(record()) == ()


def test_ancestry_walk_is_bounded_and_cycle_safe() -> None:
    parents = {"c": "b", "b": "a", "a": None, "x": "y", "y": "x"}
    assert ancestry_attribute("c", parents, {"a": "agent-a"}) == "agent-a"
    assert ancestry_attribute("c", parents, {"c": "own"}) == "own"
    assert ancestry_attribute("x", parents, {}) is None
    assert ancestry_attribute(None, parents, {}) is None
    deep = {str(i): str(i + 1) for i in range(100)}
    assert ancestry_attribute("0", deep, {"99": "far"}, max_depth=10) is None


def test_fact_properties_and_trace_ids() -> None:
    root = fact(parent=None, duration_ms=250)
    assert root.is_root
    assert root.duration_ms == 250
    assert trace_ids([record(), record()]) == ["1" * 32]
    assert record(parent=None).is_root
