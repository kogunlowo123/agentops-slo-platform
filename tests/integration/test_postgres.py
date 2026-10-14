"""Repository behaviour against a real PostgreSQL (CI service container).

Runs when AGENTOPS_TEST_DATABASE_URL points at PostgreSQL, e.g.
postgresql+asyncpg://agentops:ci-only@localhost:5432/agentops
"""

from __future__ import annotations

import os
import uuid

import pytest

from agentops_slo_platform.domain.costs import CostGrouping, PriceEntry
from agentops_slo_platform.domain.recovery import ActionStatus
from agentops_slo_platform.domain.slo import Selector, SliType, SloDefinition, evaluation_windows
from agentops_slo_platform.domain.spans import SpanKind
from agentops_slo_platform.domain.workflows import diagnose
from agentops_slo_platform.infrastructure.database import (
    AlertEvent,
    SqlAlertStore,
    SqlPriceStore,
    SqlSloStore,
    SqlSpanStore,
    SqlWorkflowStore,
    TraceTouch,
    create_schema,
    make_engine,
)
from tests.conftest import NOW_NS
from tests.factories import fact

URL = os.environ.get("AGENTOPS_TEST_DATABASE_URL", "")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not URL.startswith("postgresql"), reason="PostgreSQL not configured"),
]


async def test_span_and_workflow_stores_round_trip() -> None:
    engine = make_engine(URL)
    await create_schema(engine)
    await create_schema(engine)  # idempotent
    spans, workflows = SqlSpanStore(engine), SqlWorkflowStore(engine)
    tool = f"pg-tool-{uuid.uuid4().hex[:8]}"
    trace = uuid.uuid4().hex
    root = fact(
        trace_id=trace,
        span_id="1" * 16,
        parent=None,
        kind=SpanKind.AGENT,
        name="agent",
        start_ns=NOW_NS - 10**9,
    )
    calls = [
        fact(
            trace_id=trace,
            span_id=f"{i + 2:016x}",
            parent="1" * 16,
            name=tool,
            start_ns=NOW_NS - 10**9,
            error=i == 0,
            input_tokens=10,
            cost_usd=0.01,
        )
        for i in range(4)
    ]
    assert len(await spans.add_new([root, *calls], ingested_ns=NOW_NS)) == 5
    assert await spans.add_new([root], ingested_ns=NOW_NS) == []  # ON CONFLICT path
    assert len(await spans.trace(trace)) == 5
    assert (await spans.attribution([(trace, "1" * 16)]))["1" * 16][0] == "claims-triage"

    slo = SloDefinition(
        id=f"pg-{uuid.uuid4().hex[:8]}",
        name="pg availability",
        sli=SliType.AVAILABILITY,
        selector=Selector(kind=SpanKind.TOOL, name=tool),
        objective=0.99,
        owner="ci",
    )
    _counts, budget = await spans.window_counts(slo, evaluation_windows(), now_ns=NOW_NS)
    assert (budget.total, budget.bad) == (4, 1)
    latency = slo.model_copy(update={"sli": SliType.LATENCY, "latency_threshold_ms": 50.0})
    _, slow = await spans.window_counts(latency, evaluation_windows(), now_ns=NOW_NS)
    assert slow.bad == 4
    summary = await spans.summary(SpanKind.TOOL, "name", since_ns=0, until_ns=NOW_NS)
    assert any(g.key == tool and g.calls == 4 for g in summary)
    assert await spans.latencies(SpanKind.TOOL, "name", since_ns=0, until_ns=NOW_NS, sample=10)
    assert len(await spans.calls(SpanKind.TOOL, tool, since_ns=0, until_ns=NOW_NS)) == 4

    await workflows.touch(
        [TraceTouch(trace_id=trace, root_seen=True, workflow="pg-wf", agent=None, team=None)],
        now_ns=NOW_NS,
    )
    await workflows.touch(
        [TraceTouch(trace_id=trace, root_seen=False, workflow=None, agent="a", team="t")],
        now_ns=NOW_NS + 1,
    )
    due = await workflows.due(now_ns=NOW_NS + 60 * 10**9, quiet_ns=30 * 10**9, max_wait_ns=10**12)
    assert (trace, True) in due
    diagnosis = diagnose(await spans.trace(trace), complete=True)
    assert await workflows.finalize(diagnosis)
    assert not await workflows.finalize(diagnosis)
    finalized, stored = await workflows.get(trace)
    assert finalized
    assert stored == diagnosis
    assert await workflows.recent(outcome=None, workflow=diagnosis.workflow, since_ns=0, limit=5)
    wf_slo = SloDefinition(
        id="pg-wf",
        name="wf",
        sli=SliType.WORKFLOW_SUCCESS,
        objective=0.9,
        owner="ci",
        selector=Selector(name=diagnosis.workflow),
    )
    _, runs = await workflows.window_counts(wf_slo, evaluation_windows(), now_ns=NOW_NS + 10**9)
    assert runs.total >= 1
    assert await workflows.cost_rows(since_ns=0, until_ns=NOW_NS + 10**9)
    assert await spans.cost_rows(CostGrouping.AGENT, since_ns=0, until_ns=NOW_NS) is not None
    await engine.dispose()


async def test_slo_alert_price_and_recovery_stores() -> None:
    engine = make_engine(URL)
    await create_schema(engine)
    slos, alerts, prices = SqlSloStore(engine), SqlAlertStore(engine), SqlPriceStore(engine)
    slo_id = f"pg-{uuid.uuid4().hex[:8]}"
    slo = SloDefinition(
        id=slo_id, name="pg slo", sli=SliType.AVAILABILITY, objective=0.99, owner="ci"
    )
    assert await slos.put(slo, actor="ci", now_ns=NOW_NS)
    assert not await slos.put(slo, actor="ci", now_ns=NOW_NS)
    event = AlertEvent(slo_id, "page-1h-5m", "page", "firing", NOW_NS, 20.0, 18.0)
    assert await alerts.transition(event)
    assert not await alerts.transition(event)  # only one replica wins
    assert (slo_id, "page-1h-5m") in await alerts.firing()
    assert (await alerts.events(slo_id=slo_id, limit=5))[0].transition == "firing"
    await slos.delete(slo_id)

    model = f"pg-model-{uuid.uuid4().hex[:6]}"
    entry = PriceEntry(model=model, input_per_mtok=1, output_per_mtok=2, effective_from_ns=5)
    await prices.put(entry, actor="finops", now_ns=NOW_NS)
    await prices.put(entry.model_copy(update={"output_per_mtok": 3}), actor="finops", now_ns=NOW_NS)
    assert [p.output_per_mtok for p in await prices.all() if p.model == model] == [3]
    assert ActionStatus.ACTIVE.value == "active"
    await engine.dispose()
