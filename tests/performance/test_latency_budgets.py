"""Latency and throughput budgets for hot paths. Loose enough for shared CI runners."""

from __future__ import annotations

import time

import pytest

from agentops_slo_platform.domain.slo import Selector, SliType, SloDefinition
from agentops_slo_platform.domain.spans import SpanKind
from agentops_slo_platform.domain.workflows import diagnose
from agentops_slo_platform.ingestion import otlp
from agentops_slo_platform.runtime.container import Container
from tests.conftest import NOW_NS
from tests.factories import agent_span, fact, model_attrs, protobuf_body, tool_attrs

pytestmark = pytest.mark.performance


def batch(traces: int) -> bytes:
    spans = []
    for i in range(traces):
        root = agent_span("run", NOW_NS - (i + 1) * 10**8)
        spans += [
            root,
            root.child("tool", attributes=tool_attrs("crm.lookup")),
            root.child("chat", attributes=model_attrs()),
            root.child("tool", attributes=tool_attrs("kb.search")),
        ]
    return protobuf_body(spans)


def test_decode_budget() -> None:
    body = batch(250)  # 1,000 spans, a typical collector batch
    started = time.perf_counter()
    for _ in range(5):
        records, _ = otlp.records(otlp.parse_request(body, otlp.PROTOBUF))
    per_batch_ms = (time.perf_counter() - started) * 1000 / 5
    assert len(records) == 1000
    assert per_batch_ms < 500


async def test_ingest_budget(container: Container) -> None:
    records, _ = otlp.records(otlp.parse_request(batch(250), otlp.PROTOBUF))
    started = time.perf_counter()
    result = await container.processor.process(records, caller="collector", now_ns=NOW_NS)
    elapsed_ms = (time.perf_counter() - started) * 1000
    assert result.accepted == 1000
    assert elapsed_ms < 5000  # SQLite on a CI runner; PostgreSQL is faster


async def test_slo_evaluation_budget(container: Container) -> None:
    await container.span_store.add_new(
        [fact(start_ns=NOW_NS - (i % 3600 + 1) * 10**9, error=i % 50 == 0) for i in range(20_000)],
        ingested_ns=NOW_NS,
    )
    await container.slos.put(
        SloDefinition(
            id="tool-availability",
            name="Tools succeed",
            sli=SliType.AVAILABILITY,
            selector=Selector(kind=SpanKind.TOOL),
            objective=0.99,
            owner="platform",
        ),
        actor="sre",
    )
    started = time.perf_counter()
    for _ in range(10):
        status = await container.slos.status("tool-availability")
    per_eval_ms = (time.perf_counter() - started) * 1000 / 10
    assert status.total_events == 20_000
    assert per_eval_ms < 500  # one conditional-aggregation query over every window


def test_diagnosis_budget() -> None:
    trace = "a" * 32
    spans = [fact(trace_id=trace, parent=None, kind=SpanKind.AGENT, span_id="f" * 16)]
    spans += [
        fact(trace_id=trace, span_id=f"{i:016x}", parent="f" * 16, error=i % 7 == 0)
        for i in range(2000)
    ]
    started = time.perf_counter()
    diagnosis = diagnose(spans, complete=True, loop_threshold=5000)
    assert diagnosis.span_count == 2001
    assert (time.perf_counter() - started) * 1000 < 1000
