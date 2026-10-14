"""Fault injection: the platform fails closed, tells exporters to retry, and keeps evaluating."""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest
from agentic_core.domain.classification import DataClassification
from fastapi.testclient import TestClient

from agentops_slo_platform.api.app import create_app, run_periodically
from agentops_slo_platform.application.recovery_service import Proposer
from agentops_slo_platform.domain.errors import UnavailableError
from agentops_slo_platform.domain.recovery import ActionTarget, ActionType, ProposeAction
from agentops_slo_platform.domain.slo import Selector, SliType, SloDefinition
from agentops_slo_platform.domain.spans import SpanKind
from agentops_slo_platform.infrastructure.database import make_engine
from agentops_slo_platform.infrastructure.fabric_client import FabricClient
from agentops_slo_platform.infrastructure.notifier import AlertmanagerNotifier
from agentops_slo_platform.runtime.container import Container, build_container
from agentops_slo_platform.security.authz import FabricAuthorizer
from agentops_slo_platform.security.jwks import StaticJwks
from tests.conftest import NOW_NS, FakeClock, FakeIdp, agent_headers, make_settings
from tests.factories import agent_span, fact, protobuf_body

pytestmark = pytest.mark.chaos

PROTOBUF = {"content-type": "application/x-protobuf"}


def test_database_outage_flips_readiness_and_ingestion_returns_503(
    client: TestClient, agent_idp: FakeIdp
) -> None:
    container: Container = client.app.state.container  # type: ignore[attr-defined]
    assert client.get("/readyz").status_code == 200
    healthy = container.engine
    broken = make_engine("sqlite+aiosqlite:////nonexistent/dir/agentops.db")
    container.engine = broken
    stores = (container.span_store, container.workflow_store)
    saved = [s._engine for s in stores]
    for store in stores:
        store._engine = broken
    try:
        assert client.get("/readyz").status_code == 503
        assert client.get("/healthz").status_code == 200  # liveness unaffected
        response = client.post(
            "/v1/traces",
            content=protobuf_body([agent_span("run", NOW_NS - 10**9)]),
            headers={**PROTOBUF, **agent_headers(agent_idp)},
        )
        assert response.status_code == 503
        assert response.headers["retry-after"] == "5"  # exporters back off and retry
    finally:
        container.engine = healthy
        for store, engine in zip(stores, saved, strict=True):
            store._engine = engine


async def test_fabric_outage_fails_closed(tmp_path: Path) -> None:
    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("fabric unreachable", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(down)) as http:
        authorizer = FabricAuthorizer(FabricClient("https://identity.corp.test", http=http))
        with pytest.raises(UnavailableError):
            await authorizer.decide(
                "token",
                action="agentops:traces.ingest",
                resource_id="traces",
                classification=DataClassification.INTERNAL,
                environment={},
            )

    # Through the full app: the export is refused with 503 (retryable), never accepted.
    async with httpx.AsyncClient(transport=httpx.MockTransport(down)) as http:
        settings = make_settings(tmp_path, fabric_url="https://identity.corp.test")
        container = await build_container(settings, http=http)
        try:
            with pytest.raises(UnavailableError):
                await container.guard.require("Bearer abc", action="agentops:traces.ingest")
        finally:
            await container.engine.dispose()


async def test_alertmanager_outage_does_not_block_evaluation(
    container: Container, clock: FakeClock
) -> None:
    def down(_: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    async with httpx.AsyncClient(transport=httpx.MockTransport(down)) as http:
        container.slos._notifier = AlertmanagerNotifier(
            "https://alertmanager.corp.test",
            http=http,
            metrics=container.metrics,
            public_url="https://agentops.corp.test",
            clock=clock,
            resend_horizon_s=240,
        )
        await container.slos.put(
            SloDefinition(
                id="crm-availability",
                name="CRM lookups succeed",
                sli=SliType.AVAILABILITY,
                selector=Selector(kind=SpanKind.TOOL),
                objective=0.99,
                owner="claims",
            ),
            actor="sre",
        )
        await container.span_store.add_new(
            [fact(start_ns=NOW_NS - 60 * 10**9, error=i < 50) for i in range(100)],
            ingested_ns=NOW_NS,
        )
        summary = await container.slos.evaluate_all()
    assert summary.fired == 4
    assert not summary.notified
    # Alert state is persisted regardless; the next cycle re-sends.
    assert len(await container.slos.firing()) == 4
    assert (
        container.metrics.registry.get_sample_value("agentops_alert_notifications_failed_total")
        == 4.0
    )


async def test_audit_sink_failure_blocks_recovery_action(container: Container) -> None:
    """If evidence cannot be written, the action must not complete silently."""

    async def broken_append(_: object) -> None:
        raise OSError("disk full")

    container.audit_log._sink.append = broken_append  # type: ignore[method-assign]
    with pytest.raises(OSError, match="disk full"):
        await container.recovery.propose(
            ProposeAction(
                action=ActionType.APPLY_RETRY_POLICY,
                target=ActionTarget(kind=SpanKind.TOOL, name="crm.lookup"),
                parameters={"max_attempts": 2},
                reason="tighten retries",
            ),
            proposer=Proposer(id="alice@corp.test", kind="user", roles=frozenset({"agentops-sre"})),
        )


async def test_background_loop_survives_failures(container: Container) -> None:
    """A failing evaluation (e.g. database blip) is counted and the loop keeps running."""
    attempts = {"n": 0}
    original = container.slos.evaluate_all

    async def flaky() -> object:
        attempts["n"] += 1
        if attempts["n"] <= 2:
            raise ConnectionError("database restarted")
        return await original()

    async def sleep(_: float) -> None:
        if attempts["n"] >= 3:
            raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await run_periodically("evaluate_slos", container, flaky, 60, sleep=sleep)
    assert attempts["n"] == 3
    assert (
        container.metrics.registry.get_sample_value(
            "agentops_background_failures_total", {"loop": "evaluate_slos"}
        )
        == 2.0
    )


def test_lifespan_runs_background_jobs(tmp_path: Path) -> None:
    """With background enabled the app finalizes, evaluates and purges on its own."""
    idp = FakeIdp("https://identity.corp.test", kid="ag-1")
    clock = FakeClock()
    settings = make_settings(
        tmp_path, finalize_interval_s=1, evaluation_interval_s=1, purge_interval_s=60
    )
    built: dict[str, Container] = {}

    async def factory() -> Container:
        built["c"] = await build_container(
            settings,
            operator_resolver=StaticJwks(idp.jwks()),
            agent_resolver=StaticJwks(idp.jwks()),
            clock=clock,
        )
        return built["c"]

    with TestClient(create_app(factory)) as client:
        client.post(
            "/v1/traces",
            content=protobuf_body([agent_span("run", NOW_NS - 10**9)]),
            headers={**PROTOBUF, **agent_headers(idp)},
        )
        clock.advance(31)
        for _ in range(50):
            done = client.get("/metrics").text
            if "agentops_workflow_runs_total{" in done:
                break
            client.portal.call(asyncio.sleep, 0.1)  # type: ignore[union-attr]
        assert "agentops_workflow_runs_total{" in done
