"""Agent authorization (local and fabric), decision cache, fabric client and notifier."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from agentic_core.audit import AuditLog, InMemoryAuditSink
from agentic_core.domain.classification import DataClassification, Decision
from agentic_core.policy import PolicyEngine, PolicySet
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from agentops_slo_platform.domain.errors import (
    ForbiddenError,
    PlatformError,
    UnauthorizedError,
    UnavailableError,
)
from agentops_slo_platform.infrastructure.fabric_client import (
    FabricAuthorizeRequest,
    FabricClient,
)
from agentops_slo_platform.infrastructure.notifier import (
    AlertmanagerNotifier,
    AlertNotice,
    CredentialProvider,
)
from agentops_slo_platform.observability.metrics import PlatformMetrics
from agentops_slo_platform.security.authz import (
    AccessDecision,
    AccessGuard,
    DecisionCache,
    FabricAuthorizer,
    LocalAuthorizer,
    bearer_token,
)
from agentops_slo_platform.security.jwks import StaticJwks
from tests.conftest import AGENT_ISSUER, ROOT, FakeClock, FakeIdp, agent_token

FABRIC = "https://identity.corp.test"
DECISION = {
    "decision": "allow",
    "agent": "spiffe://agents.corp.test/agent/observability/otel-collector",
    "action": "agentops:traces.ingest",
    "resource": "agentops:traces",
    "matched_rules": ["allow-scoped-actions"],
    "policy_version": "2026.10.0",
    "policy_digest": "sha256:abc",
    "reason": "explicit allow",
    "delegation_chain": [],
}


def local(idp: FakeIdp) -> LocalAuthorizer:
    return LocalAuthorizer(
        StaticJwks(idp.jwks()),
        issuer=AGENT_ISSUER,
        audience="agentops",
        policy=PolicyEngine(PolicySet.from_file(ROOT / "policies" / "agentops-access.yaml")),
    )


async def decide(authorizer: Any, token: str, action: str, **kw: Any) -> AccessDecision:
    kw.setdefault("classification", DataClassification.INTERNAL)
    result: AccessDecision = await authorizer.decide(
        token, action=action, resource_id="r", environment={}, **kw
    )
    return result


async def test_local_authorizer_scopes(agent_idp: FakeIdp) -> None:
    authorizer = local(agent_idp)
    allowed = await decide(authorizer, agent_token(agent_idp), "agentops:slo.read")
    assert allowed.decision is Decision.ALLOW
    assert allowed.source == "local"
    assert allowed.expires_at is not None
    narrow = agent_token(agent_idp, scope="agentops:slo.read")
    assert (await decide(authorizer, narrow, "agentops:cost.read")).decision is Decision.DENY
    wildcard = agent_token(agent_idp, scope="agentops:*.read")
    assert (await decide(authorizer, wildcard, "agentops:cost.read")).decision is Decision.ALLOW
    assert (
        await decide(authorizer, agent_token(agent_idp, scope=7), "agentops:slo.read")
    ).decision is (Decision.DENY)


async def test_local_authorizer_ceilings(agent_idp: FakeIdp) -> None:
    authorizer = local(agent_idp)
    token = agent_token(agent_idp, max_cls=1)
    confidential = await decide(
        authorizer, token, "agentops:cost.read", classification=DataClassification.CONFIDENTIAL
    )
    assert confidential.decision is Decision.DENY
    assert confidential.reason == "resource exceeds agent classification"
    critical = await decide(
        authorizer, agent_token(agent_idp, risk=4, max_cls=3), "agentops:cost.read"
    )
    assert critical.matched_rules == ("deny-critical-risk-cost-access",)
    odd_risk = await decide(authorizer, agent_token(agent_idp, risk="high"), "agentops:slo.read")
    assert odd_risk.decision is Decision.ALLOW
    delegated = await decide(
        authorizer, agent_token(agent_idp, act={"sub": "x"}), "agentops:recovery.propose"
    )
    assert delegated.matched_rules == ("deny-delegated-recovery-proposals",)


async def test_local_authorizer_rejects_foreign_tokens(agent_idp: FakeIdp) -> None:
    stranger = FakeIdp(AGENT_ISSUER, kid=agent_idp.kid)
    with pytest.raises(UnauthorizedError):
        await decide(local(agent_idp), agent_token(stranger), "agentops:slo.read")


def fabric_transport(
    *, active: bool = True, decision: dict[str, Any] | None = None, status: int = 200
) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oauth2/introspect":
            assert b"token=" in request.content
            return httpx.Response(200, json={"active": active, "sub": "s", "exp": 2_000_000_000})
        if request.url.path == "/v1/authorize":
            assert request.headers["authorization"].startswith("Bearer ")
            body = json.loads(request.content)
            assert body["resource_type"] == "agentops"
            return httpx.Response(status, json=decision or DECISION)
        if request.url.path == "/oauth2/token":
            assert b"jwt-bearer" in request.content
            return httpx.Response(
                200, json={"access_token": "agent-cred", "token_type": "Bearer", "expires_in": 600}
            )
        return httpx.Response(404)

    return httpx.MockTransport(handler)


async def test_fabric_authorizer() -> None:
    async with httpx.AsyncClient(transport=fabric_transport()) as http:
        result = await decide(FabricAuthorizer(FabricClient(FABRIC, http=http)), "t", "a")
        assert result.decision is Decision.ALLOW
        assert result.source == "fabric"
        assert result.subject.endswith("otel-collector")
        assert result.expires_at == 2_000_000_000
    async with httpx.AsyncClient(transport=fabric_transport(active=False)) as http:
        with pytest.raises(UnauthorizedError, match="not active"):
            await decide(FabricAuthorizer(FabricClient(FABRIC, http=http)), "t", "a")


@pytest.mark.parametrize(
    ("status", "body", "error"),
    [
        (401, {"error": "unauthorized", "error_description": "token expired"}, UnauthorizedError),
        (403, {"error": "forbidden"}, ForbiddenError),
        (422, {"detail": "x"}, PlatformError),
        (503, {}, UnavailableError),
        (200, {"unexpected": True}, UnavailableError),
    ],
)
async def test_fabric_client_authorize_errors(
    status: int, body: dict[str, Any], error: type[Exception]
) -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = FabricClient(FABRIC + "/", http=http)
        with pytest.raises(error):
            await client.authorize(
                "t", FabricAuthorizeRequest(action="agentops:slo.read", resource_type="agentops")
            )


async def test_fabric_client_transport_and_token_errors() -> None:
    def broken(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(broken)) as http:
        with pytest.raises(UnavailableError, match="unreachable"):
            await FabricClient(FABRIC, http=http).introspect("t")

    def rejecting(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oauth2/token":
            return httpx.Response(400, text="not json")
        return httpx.Response(400, json=["list"])

    async with httpx.AsyncClient(transport=httpx.MockTransport(rejecting)) as http:
        client = FabricClient(FABRIC, http=http)
        with pytest.raises(UnauthorizedError, match="HTTP 400"):
            await client.token_jwt_bearer(client_id="c", assertion="a")
        with pytest.raises(UnavailableError, match="HTTP 400"):
            await client.introspect("t")

    async with httpx.AsyncClient(transport=fabric_transport()) as http:
        credential = await FabricClient(FABRIC, http=http).token_jwt_bearer(
            client_id="c", assertion="a", scope="alerts:write"
        )
        assert credential.access_token == "agent-cred"


def test_decision_cache_ttl_expiry_and_eviction() -> None:
    now = [1000.0]
    cache = DecisionCache(ttl_s=10, max_entries=2, clock=lambda: now[0])
    decision = AccessDecision(
        decision=Decision.ALLOW,
        subject="s",
        action="a",
        resource="r",
        reason="",
        matched_rules=(),
        policy_digest="d",
        source="local",
        expires_at=1005.0,
    )
    key = DecisionCache.key("token", "a", "r")
    cache.put(key, decision)
    assert cache.get(key) == decision
    now[0] = 1005.0  # credential expiry bounds the cache entry
    assert cache.get(key) is None
    cache.put(DecisionCache.key("1", "a", "r"), decision.model_copy(update={"expires_at": None}))
    cache.put(DecisionCache.key("2", "a", "r"), decision.model_copy(update={"expires_at": None}))
    cache.put(DecisionCache.key("3", "a", "r"), decision.model_copy(update={"expires_at": None}))
    assert cache.get(DecisionCache.key("1", "a", "r")) is None
    assert cache.get(DecisionCache.key("3", "a", "r")) is not None
    disabled = DecisionCache(ttl_s=0)
    disabled.put(key, decision)
    assert disabled.get(key) is None


def test_bearer_token_parsing() -> None:
    assert bearer_token("Bearer abc") == "abc"
    for bad in (None, "Basic abc", "Bearer ", "Bearer " + "x" * 9000):
        with pytest.raises(UnauthorizedError):
            bearer_token(bad)


async def test_access_guard_audits_and_caches(agent_idp: FakeIdp) -> None:
    sink = InMemoryAuditSink()
    metrics = PlatformMetrics()
    guard = AccessGuard(
        local(agent_idp),
        audit=AuditLog(sink, Ed25519PrivateKey.generate(), key_id="t"),
        metrics=metrics,
        cache=DecisionCache(ttl_s=60),
    )
    header = f"Bearer {agent_token(agent_idp)}"
    await guard.require(header, action="agentops:slo.read")
    await guard.require(header, action="agentops:slo.read")  # cached: no second audit event
    with pytest.raises(ForbiddenError):
        await guard.require(
            f"Bearer {agent_token(agent_idp, scope='x:y')}", action="agentops:slo.read"
        )
    with pytest.raises(UnauthorizedError):
        await guard.require("Bearer not-a-jwt", action="agentops:slo.read")
    assert [e.outcome for e in sink.events] == ["allow", "deny", "unauthenticated"]
    assert (
        metrics.registry.get_sample_value(
            "agentops_authorization_decisions_total",
            {"action": "agentops:slo.read", "decision": "allow"},
        )
        == 2.0
    )


async def test_credential_provider_caches_and_renews(tmp_path: Path, clock: FakeClock) -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        assert b"assertion=projected-token" in request.content
        return httpx.Response(
            200,
            json={"access_token": f"cred-{calls['n']}", "token_type": "Bearer", "expires_in": 300},
        )

    token_file = tmp_path / "token"
    token_file.write_text("projected-token\n", encoding="utf-8")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        provider = CredentialProvider(
            FabricClient(FABRIC, http=http),
            agent_id="spiffe://agents.corp.test/agent/observability/agentops",
            workload_token_file=token_file,
            clock=clock,
        )
        assert await provider.bearer() == "cred-1"
        clock.advance(200)
        assert await provider.bearer() == "cred-1"
        clock.advance(50)  # within 60 s of expiry: renew
        assert await provider.bearer() == "cred-2"


def notice(firing: bool = True) -> AlertNotice:
    return AlertNotice(
        slo_id="crm-availability",
        slo_name="CRM lookups succeed",
        owner="claims",
        rule_id="page-1h-5m",
        severity="page",
        firing=firing,
        long_burn=None,
        short_burn=20.0,
        factor=14.4,
        since_ns=0,
    )


async def test_alertmanager_notifier(tmp_path: Path, clock: FakeClock) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/oauth2/token":
            return httpx.Response(
                200, json={"access_token": "cred", "token_type": "Bearer", "expires_in": 600}
            )
        return httpx.Response(200)

    token_file = tmp_path / "token"
    token_file.write_text("projected", encoding="utf-8")
    metrics = PlatformMetrics()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        notifier = AlertmanagerNotifier(
            "https://alertmanager.corp.test/",
            http=http,
            metrics=metrics,
            public_url="https://agentops.corp.test",
            clock=clock,
            resend_horizon_s=240,
            credentials=CredentialProvider(
                FabricClient(FABRIC, http=http),
                agent_id="spiffe://a/agent/b/c",
                workload_token_file=token_file,
                clock=clock,
            ),
        )
        assert await notifier.send([])
        assert await notifier.send([notice(), notice(firing=False)])
    post = seen[-1]
    assert post.url.path == "/api/v2/alerts"
    assert post.headers["authorization"] == "Bearer cred"
    firing, resolved = json.loads(post.content)
    assert firing["annotations"]["summary"].endswith("n/ax")
    assert firing["endsAt"] > resolved["endsAt"]

    def down(_: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    async with httpx.AsyncClient(transport=httpx.MockTransport(down)) as http:
        failing = AlertmanagerNotifier(
            "https://alertmanager.corp.test",
            http=http,
            metrics=metrics,
            public_url="https://agentops.corp.test",
            clock=clock,
            resend_horizon_s=240,
        )
        assert not await failing.send([notice()])
    assert metrics.registry.get_sample_value("agentops_alert_notifications_failed_total") == 1.0
