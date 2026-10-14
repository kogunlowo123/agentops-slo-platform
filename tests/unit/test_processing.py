"""Span processing (attribution, pricing, scrubbing, dedupe) and analytics use cases."""

from __future__ import annotations

import pytest

from agentops_slo_platform.application.analytics import GroupBy
from agentops_slo_platform.domain.costs import CostGrouping, PriceEntry
from agentops_slo_platform.domain.errors import NotFoundError, PlatformError
from agentops_slo_platform.domain.spans import SpanKind, SpanRecord
from agentops_slo_platform.domain.workflows import FailureClass, Outcome
from agentops_slo_platform.ingestion import otlp
from agentops_slo_platform.runtime.container import Container
from tests.conftest import NOW_NS, FakeClock
from tests.factories import OtlpSpan, agent_span, model_attrs, protobuf_body, tool_attrs

CALLER = "spiffe://agents.corp.test/agent/observability/otel-collector"


def decode(spans: list[OtlpSpan], **kw: object) -> list[SpanRecord]:
    records, rejected = otlp.records(otlp.parse_request(protobuf_body(spans, **kw), otlp.PROTOBUF))
    assert not rejected
    return records


async def ingest(container: Container, spans: list[OtlpSpan], **kw: object) -> int:
    result = await container.processor.process(decode(spans, **kw), caller=CALLER, now_ns=NOW_NS)
    return result.accepted


async def price(container: Container, model: str = "gpt-4.1", at: int = 0) -> None:
    await container.prices.put(
        PriceEntry(model=model, input_per_mtok=2.0, output_per_mtok=8.0, effective_from_ns=at),
        actor="finops@corp.test",
        now_ns=NOW_NS,
    )
    container.price_cache.invalidate()


def claims_trace(start: int = NOW_NS - 60 * 10**9) -> list[OtlpSpan]:
    root = agent_span(
        "claims-triage run",
        start,
        duration_ms=4000,
        attributes={"agentops.workflow.name": "claims-intake"},
    )
    tool = root.child("crm", attributes=tool_attrs("crm.lookup"), duration_ms=250)
    llm = root.child("chat", attributes=model_attrs(), duration_ms=1200)
    return [root, tool, llm]


async def test_attribution_pricing_and_workflow_touch(container: Container) -> None:
    await price(container)
    spans = claims_trace()
    assert (
        await ingest(container, spans, resource={"service.name": "x", "agentops.team": "claims"})
        == 3
    )
    stored = await container.span_store.trace(spans[0].trace_id)
    model = next(s for s in stored if s.kind is SpanKind.MODEL)
    assert model.agent == "claims-triage"  # inherited from the invoke_agent span
    assert model.team == "claims"
    assert model.cost_usd == pytest.approx((1000 * 2.0 + 200 * 8.0) / 1_000_000)
    assert model.workflow == "claims-intake"
    assert model.ingested_by == CALLER
    finalized, diagnosis = await container.workflow_store.get(spans[0].trace_id)
    assert not finalized
    assert diagnosis is None


async def test_parent_attribution_across_batches(container: Container) -> None:
    root, tool, llm = claims_trace()
    await ingest(container, [root], resource={"service.name": "x", "agentops.team": "claims"})
    await ingest(container, [tool, llm], resource={"service.name": "x"})
    stored = {s.span_id: s for s in await container.span_store.trace(root.trace_id)}
    assert stored[llm.span_id].agent == "claims-triage"
    assert stored[llm.span_id].team == "claims"
    assert stored[llm.span_id].cost_usd is None  # no price configured


async def test_unattributed_and_unpriced(container: Container) -> None:
    lone = OtlpSpan(name="chat", start_ns=NOW_NS - 10**9, attributes=model_attrs(model="m-x"))
    await ingest(container, [lone])
    (stored,) = await container.span_store.trace(lone.trace_id)
    assert (stored.agent, stored.team, stored.cost_usd) == ("unattributed", "unattributed", None)
    text = container.metrics.registry.get_sample_value(
        "agentops_unpriced_model_calls_total", {"model": "m-x"}
    )
    assert text == 1.0


async def test_duplicates_and_time_window_rejections(container: Container) -> None:
    spans = claims_trace()
    first = await container.processor.process(decode(spans), caller=CALLER, now_ns=NOW_NS)
    again = await container.processor.process(decode(spans), caller=CALLER, now_ns=NOW_NS)
    assert (first.accepted, again.accepted, again.duplicates) == (3, 0, 3)
    future = OtlpSpan(name="f", start_ns=NOW_NS + 3600 * 10**9)
    ancient = OtlpSpan(name="a", start_ns=NOW_NS - 400 * 86400 * 10**9)
    result = await container.processor.process(
        decode([future, ancient]), caller=CALLER, now_ns=NOW_NS
    )
    assert result.accepted == 0
    assert result.message() == "timestamp in the future; timestamp older than retention"


async def test_error_text_is_scrubbed(container: Container) -> None:
    span = OtlpSpan(
        name="tool",
        start_ns=NOW_NS - 10**9,
        attributes=tool_attrs("email.send", **{"error.type": "SendError"}),
        error=True,
        status_message=(
            "failed for jane.doe@corp.test with key sk-ant-abcdefghijklmnopqrstuvwxyz0123"
        ),
    )
    await ingest(container, [span])
    (stored,) = await container.span_store.trace(span.trace_id)
    assert stored.error_type == "SendError"
    assert "jane.doe" not in (stored.error_message or "")
    assert "sk-ant" not in (stored.error_message or "")
    assert "[REDACTED:email]" in (stored.error_message or "")


async def test_finalize_classifies_once(container: Container, clock: FakeClock) -> None:
    spans = claims_trace()
    spans[2].error = True
    spans[2].attributes["http.response.status_code"] = 429
    spans[0].error = True
    await ingest(container, spans)
    assert await container.analytics.finalize_due() == 0  # still within the quiet period
    clock.advance(31)
    assert await container.analytics.finalize_due() == 1
    assert await container.analytics.finalize_due() == 0
    view = await container.analytics.workflow(spans[0].trace_id)
    assert view.finalized
    assert view.diagnosis.outcome is Outcome.FAILED
    assert view.diagnosis.failure_class is FailureClass.RATE_LIMITED
    runs = await container.analytics.workflows(
        outcome=Outcome.FAILED, workflow="claims-intake", hours=1, limit=10
    )
    assert [r.trace_id for r in runs] == [spans[0].trace_id]
    sample = container.metrics.registry.get_sample_value(
        "agentops_workflow_runs_total",
        {
            "workflow": "claims-intake",
            "team": "unattributed",
            "outcome": "failed",
            "failure_class": "rate_limited",
        },
    )
    assert sample == 1.0


async def test_orphan_traces_finalize_as_incomplete(container: Container, clock: FakeClock) -> None:
    root = agent_span("run", NOW_NS - 10**9)
    orphan = root.child("tool", attributes=tool_attrs("crm.lookup"))
    await ingest(container, [orphan])
    clock.advance(60)
    assert await container.analytics.finalize_due() == 0  # no root, wait limit not reached
    provisional = await container.analytics.workflow(root.trace_id)
    assert not provisional.finalized
    assert provisional.diagnosis.outcome is Outcome.INCOMPLETE
    clock.advance(900)
    assert await container.analytics.finalize_due() == 1
    assert (await container.analytics.workflow(root.trace_id)).diagnosis.outcome is (
        Outcome.INCOMPLETE
    )


async def test_unknown_trace(container: Container) -> None:
    with pytest.raises(NotFoundError):
        await container.analytics.workflow("e" * 32)


async def test_operation_report(container: Container) -> None:
    for i in range(4):
        spans = claims_trace(NOW_NS - (i + 2) * 10**9)
        spans[1].duration_ms = 100 * (i + 1)
        spans[1].error = i == 3
        await ingest(container, spans)
    report = await container.analytics.operations(
        SpanKind.TOOL, group_by=GroupBy.NAME, window_minutes=60
    )
    (row,) = report.rows
    assert (row.key, row.calls, row.errors, row.error_rate) == ("crm.lookup", 4, 1, 0.25)
    assert row.p50_ms == 200.0  # successful calls only: 100, 200, 300
    by_agent = await container.analytics.operations(
        SpanKind.MODEL, group_by=GroupBy.AGENT, window_minutes=60
    )
    assert by_agent.rows[0].key == "claims-triage"
    assert by_agent.rows[0].input_tokens == 4000


async def test_cost_reports(container: Container, clock: FakeClock) -> None:
    await price(container)
    await ingest(
        container, claims_trace(), resource={"service.name": "x", "agentops.team": "claims"}
    )
    expected = (1000 * 2.0 + 200 * 8.0) / 1_000_000
    for grouping, key in (
        (CostGrouping.AGENT, "claims-triage"),
        (CostGrouping.TEAM, "claims"),
        (CostGrouping.MODEL, "gpt-4.1"),
    ):
        report = await container.analytics.recent_costs(grouping, hours=1)
        assert report.rows[0].key == key
        assert report.total_cost_usd == pytest.approx(expected)
    clock.advance(31)
    await container.analytics.finalize_due()
    by_workflow = await container.analytics.recent_costs(CostGrouping.WORKFLOW, hours=1)
    assert by_workflow.rows[0].key == "claims-intake"
    assert by_workflow.rows[0].model_calls == 1
    assert by_workflow.total_cost_usd == pytest.approx(expected)
    with pytest.raises(PlatformError, match="before end"):
        await container.analytics.costs(CostGrouping.TEAM, start_ns=10, end_ns=5)
    with pytest.raises(PlatformError, match="93 days"):
        await container.analytics.costs(CostGrouping.TEAM, start_ns=0, end_ns=NOW_NS)


async def test_retention_purge(container: Container, clock: FakeClock) -> None:
    await ingest(container, claims_trace())
    clock.advance(36 * 86400)
    assert await container.purge_expired() == 4  # three spans and one workflow run
