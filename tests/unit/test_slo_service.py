"""SLO management, evaluation from stored events, alert lifecycle and rule export."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from agentops_slo_platform.domain.errors import NotFoundError, PlatformError
from agentops_slo_platform.domain.slo import Selector, SliType, SloDefinition, SloHealth
from agentops_slo_platform.domain.spans import SpanKind
from agentops_slo_platform.infrastructure.notifier import AlertmanagerNotifier
from agentops_slo_platform.observability.rules import rule_groups
from agentops_slo_platform.runtime.container import Container
from tests.conftest import NOW_NS, ROOT, FakeClock
from tests.factories import fact


def tool_slo(**overrides: object) -> SloDefinition:
    values: dict[str, object] = {
        "id": "crm-availability",
        "name": "CRM lookups succeed",
        "sli": SliType.AVAILABILITY,
        "selector": Selector(kind=SpanKind.TOOL, name="crm.lookup"),
        "objective": 0.99,
        "owner": "claims-platform",
    }
    values.update(overrides)
    return SloDefinition.model_validate(values)


async def load(container: Container, *, total: int, bad: int, age_s: int = 60) -> None:
    facts = [
        fact(start_ns=NOW_NS - age_s * 10**9, error=i < bad, duration_ms=50 + i)
        for i in range(total)
    ]
    await container.span_store.add_new(facts, ingested_ns=NOW_NS)


async def test_crud_and_seed(container: Container, tmp_path: Path) -> None:
    slos = container.slos
    assert await slos.put(tool_slo(), actor="sre@corp.test")
    assert not await slos.put(tool_slo(objective=0.995), actor="sre@corp.test")
    assert (await slos.get("crm-availability")).objective == 0.995
    assert await slos.seed(ROOT / "configs" / "slos.yaml") == 5
    assert await slos.seed(ROOT / "configs" / "slos.yaml") == 0
    assert len(await slos.all()) == 6
    await slos.delete("crm-availability", actor="sre@corp.test")
    with pytest.raises(NotFoundError):
        await slos.get("crm-availability")
    with pytest.raises(NotFoundError):
        await slos.delete("crm-availability", actor="sre@corp.test")
    bad = tmp_path / "bad.yaml"
    bad.write_text("slos: {}\n", encoding="utf-8")
    with pytest.raises(PlatformError, match="slos"):
        await slos.seed(bad)


async def test_status_counts_only_selected_events(container: Container) -> None:
    await container.slos.put(tool_slo(), actor="sre")
    await load(container, total=200, bad=40)
    await container.span_store.add_new(
        [fact(name="other.tool", start_ns=NOW_NS - 10**9, error=True) for _ in range(50)],
        ingested_ns=NOW_NS,
    )
    status = await container.slos.status("crm-availability")
    assert (status.total_events, status.bad_events) == (200, 40)
    assert status.health is SloHealth.CRITICAL  # 20% errors = 20x burn in 5m and 1h
    gauge = container.metrics.registry.get_sample_value(
        "agentops_slo_error_budget_remaining", {"slo": "crm-availability"}
    )
    assert gauge == pytest.approx(1 - 40 / 2)


async def test_latency_sli(container: Container) -> None:
    await container.slos.put(
        tool_slo(id="crm-latency", sli=SliType.LATENCY, latency_threshold_ms=100), actor="sre"
    )
    await load(container, total=100, bad=0)  # durations 50..149 ms
    status = await container.slos.status("crm-latency")
    assert status.bad_events == 49


async def test_evaluate_all_fires_resolves_and_notifies(
    container: Container, clock: FakeClock
) -> None:
    posted: list[list[dict[str, object]]] = []

    def alertmanager(request: httpx.Request) -> httpx.Response:
        posted.append(json.loads(request.content))
        return httpx.Response(200)

    async with httpx.AsyncClient(transport=httpx.MockTransport(alertmanager)) as http:
        container.slos._notifier = AlertmanagerNotifier(
            "https://alertmanager.corp.test",
            http=http,
            metrics=container.metrics,
            public_url="https://agentops.corp.test",
            clock=clock,
            resend_horizon_s=240,
        )
        await container.slos.put(tool_slo(), actor="sre")
        await load(container, total=200, bad=40)
        summary = await container.slos.evaluate_all()
        # 20% errors burn 20x: every rule (14.4x, 6x, 3x, 1x) fires.
        assert (summary.evaluated, summary.fired, summary.resolved) == (1, 4, 0)
        firing = await container.slos.firing()
        assert {a.severity for a in firing} == {"page", "ticket"}
        assert posted[0][0]["labels"]["severity"] == "page"  # type: ignore[index]

        # Steady state: no new transitions, alerts are re-sent for Alertmanager.
        again = await container.slos.evaluate_all()
        assert (again.fired, again.resolved) == (0, 0)
        assert len(posted[1]) == 4

        # Seven hours later every short window is clean: all rules reset.
        clock.advance(7 * 3600)
        await load(container, total=200, bad=0, age_s=30)
        resolved = await container.slos.evaluate_all()
        assert resolved.resolved == 4
        assert await container.slos.firing() == []
        assert len(posted[2]) == 4
        history = await container.slos.history(slo_id="crm-availability", limit=10)
        assert [h.transition for h in history].count("resolved") == 4
        assert len(await container.slos.history(slo_id=None, limit=1)) == 1


async def test_workflow_success_slo(container: Container, clock: FakeClock) -> None:
    from tests.unit.test_processing import claims_trace, ingest

    await container.slos.put(
        SloDefinition(
            id="claims-workflows",
            name="Claims intake workflows succeed",
            sli=SliType.WORKFLOW_SUCCESS,
            selector=Selector(name="claims-intake"),
            objective=0.9,
            min_events=1,
            owner="claims-platform",
        ),
        actor="sre",
    )
    for i in range(4):
        spans = claims_trace(NOW_NS - (i + 2) * 10**9)
        spans[0].error = i == 0
        await ingest(container, spans)
    clock.advance(31)
    await container.analytics.finalize_due()
    status = await container.slos.status("claims-workflows")
    assert (status.total_events, status.bad_events) == (4, 1)


def test_prometheus_rules_for_each_sli() -> None:
    availability = rule_groups(tool_slo())
    recording, alerts = availability["groups"]
    assert len(recording["rules"]) == 7
    assert 'outcome="error"' in recording["rules"][0]["expr"]
    assert 'name="crm.lookup"' in recording["rules"][0]["expr"]
    assert "(14.4 * 0.01)" in alerts["rules"][0]["expr"]

    latency = rule_groups(tool_slo(id="lat", sli=SliType.LATENCY, latency_threshold_ms=500))
    assert 'le="0.5"' in latency["groups"][0]["rules"][0]["expr"]
    with pytest.raises(PlatformError, match="bucket"):
        rule_groups(tool_slo(id="lat2", sli=SliType.LATENCY, latency_threshold_ms=123))

    workflow = rule_groups(
        SloDefinition(
            id="wf",
            name="workflows",
            sli=SliType.WORKFLOW_SUCCESS,
            selector=Selector(name='quote"d', team="claims"),
            objective=0.98,
            owner="x1",
        )
    )
    expr = workflow["groups"][0]["rules"][0]["expr"]
    assert 'workflow="quote\\"d"' in expr
    assert 'outcome!~"recovered|success"' in expr
    with pytest.raises(PlatformError, match="agent"):
        rule_groups(
            SloDefinition(
                id="wf2",
                name="workflows",
                sli=SliType.WORKFLOW_SUCCESS,
                selector=Selector(agent="a"),
                objective=0.98,
                owner="x1",
            )
        )
