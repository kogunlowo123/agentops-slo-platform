"""Settings validation, production wiring, contracts, metrics and background loops."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ec import SECP256R1, generate_private_key
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pydantic import ValidationError

from agentops_slo_platform.api.app import run_periodically
from agentops_slo_platform.contracts import main as contracts_main
from agentops_slo_platform.infrastructure.database import make_engine
from agentops_slo_platform.observability.metrics import (
    OVERFLOW,
    CardinalityLimiter,
    PlatformMetrics,
)
from agentops_slo_platform.runtime.container import Container, build_container
from agentops_slo_platform.security.authz import FabricAuthorizer, LocalAuthorizer
from agentops_slo_platform.security.jwks import RemoteJwks
from tests.conftest import ROOT, make_settings
from tests.factories import fact


def write_pem(path: Path, key: Any) -> None:
    path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"environment": "production"}, "dev_ephemeral_keys"),
        ({"dev_ephemeral_keys": False}, "AUDIT_KEY_FILE"),
        ({"agent_issuer": None}, "FABRIC_URL"),
        ({"fabric_agent_id": "spiffe://a/agent/b/c"}, "go together"),
        (
            {"fabric_agent_id": "spiffe://a/agent/b/c", "workload_token_file": Path("t")},
            "requires AGENTOPS_FABRIC_URL",
        ),
        ({"workflow_quiet_s": 900, "workflow_max_wait_s": 600}, "exceed"),
    ],
)
def test_settings_fail_closed(tmp_path: Path, overrides: dict[str, Any], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        make_settings(tmp_path, **overrides)


async def test_production_wiring_with_fabric_and_alertmanager(tmp_path: Path) -> None:
    audit_key = tmp_path / "audit-2026-10.pem"
    write_pem(audit_key, Ed25519PrivateKey.generate())
    token = tmp_path / "token"
    token.write_text("projected", encoding="utf-8")
    settings = make_settings(
        tmp_path,
        environment="production",
        dev_ephemeral_keys=False,
        audit_key_file=audit_key,
        fabric_url="https://identity.corp.test",
        fabric_agent_id="spiffe://agents.corp.test/agent/observability/agentops",
        workload_token_file=token,
        alertmanager_url="https://alertmanager.corp.test",
        slo_seed_file=ROOT / "configs" / "slos.yaml",
        mcp_allowed_hosts=["agentops.corp.test"],
    )
    container = await build_container(settings)
    try:
        assert container.audit_log._key_id == "audit-2026-10"
        assert isinstance(container.guard._authorizer, FabricAuthorizer)
        assert container.slos._notifier is not None
        assert container.slos._notifier._credentials is not None
        assert len(await container.slos.all()) == 5
        assert isinstance(container.operators._resolver, RemoteJwks)
    finally:
        await container.aclose()


async def test_local_mode_uses_remote_jwks_by_default(tmp_path: Path) -> None:
    container = await build_container(make_settings(tmp_path))
    try:
        authorizer = container.guard._authorizer
        assert isinstance(authorizer, LocalAuthorizer)
        assert isinstance(authorizer._resolver, RemoteJwks)
        assert container.slos._notifier is None
    finally:
        await container.aclose()


async def test_rejects_non_ed25519_audit_key(tmp_path: Path) -> None:
    audit_key = tmp_path / "audit.pem"
    write_pem(audit_key, generate_private_key(SECP256R1()))
    settings = make_settings(tmp_path, dev_ephemeral_keys=False, audit_key_file=audit_key)
    with pytest.raises(ValueError, match="Ed25519"):
        await build_container(settings)


def test_unsupported_database_dialect() -> None:
    with pytest.raises(ValueError, match="unsupported"):
        make_engine("mysql+aiomysql://u:p@db.corp.test/agentops")


def test_committed_contracts_are_current() -> None:
    assert contracts_main(["--check"], root=ROOT) == 0


def test_export_and_check_round_trip(tmp_path: Path) -> None:
    assert contracts_main(["--check"], root=tmp_path) == 1
    assert contracts_main([], root=tmp_path) == 0
    assert contracts_main(["--check"], root=tmp_path) == 0
    assert (tmp_path / "docs" / "api" / "mcp-tools.json").is_file()
    assert (tmp_path / "schemas" / "slo-definition.schema.json").is_file()


def test_cardinality_limiter_folds_overflow() -> None:
    limiter = CardinalityLimiter(2)
    assert [limiter.admit("tool", v) for v in ("a", "b", "a", "c", "d")] == [
        "a",
        "b",
        "a",
        OVERFLOW,
        OVERFLOW,
    ]
    assert limiter.overflowed == {"tool": 2}


def test_metrics_observe_and_forget() -> None:
    metrics = PlatformMetrics(label_limit=10)
    metrics.observe_fact(fact(name="t1"))
    from agentops_slo_platform.domain.spans import SpanKind

    metrics.observe_fact(
        fact(
            kind=SpanKind.MODEL, name="m", model="m", input_tokens=5, output_tokens=2, cost_usd=0.1
        )
    )
    assert (
        metrics.registry.get_sample_value(
            "agentops_tokens_total",
            {"agent": "claims-triage", "team": "claims", "model": "m", "type": "input"},
        )
        == 5.0
    )
    from agentops_slo_platform.domain.slo import SliType, SloHealth, SloStatus, WindowBurn

    status = SloStatus(
        slo_id="s",
        sli=SliType.AVAILABILITY,
        objective=0.99,
        window_days=30,
        health=SloHealth.OK,
        total_events=1,
        bad_events=0,
        sli_value=1.0,
        error_budget_remaining=1.0,
        time_to_exhaustion_hours=None,
        windows=(WindowBurn(window="5m", total=1, bad=0, error_ratio=0.0, burn_rate=0.0),),
        rules=(),
        evaluated_at_ns=0,
    )
    metrics.observe_slo(status)
    assert (
        metrics.registry.get_sample_value("agentops_slo_burn_rate", {"slo": "s", "window": "5m"})
        == 0
    )
    metrics.forget_slo("s")
    assert (
        metrics.registry.get_sample_value("agentops_slo_error_budget_remaining", {"slo": "s"})
        is None
    )


async def test_background_loop_counts_failures_and_continues(container: Container) -> None:
    calls = {"n": 0}

    async def flaky() -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("database hiccup")

    async def stop_after_two(_: float) -> None:
        if calls["n"] >= 2:
            raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await run_periodically("evaluate_slos", container, flaky, 60, sleep=stop_after_two)
    assert calls["n"] == 2
    assert (
        container.metrics.registry.get_sample_value(
            "agentops_background_failures_total", {"loop": "evaluate_slos"}
        )
        == 1.0
    )


async def test_remote_jwks_resolution_for_operator_tokens(tmp_path: Path) -> None:
    from tests.conftest import FakeIdp

    idp = FakeIdp("https://login.corp.test/tenant", kid="op-1")

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=idp.jwks())

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        container = await build_container(make_settings(tmp_path), http=http)
        try:
            principal = await container.operators.authenticate(
                "Bearer " + idp.mint(sub="sre@corp.test", aud="agentops", roles="not-a-list")
            )
            assert principal.roles == frozenset()
        finally:
            await container.engine.dispose()
