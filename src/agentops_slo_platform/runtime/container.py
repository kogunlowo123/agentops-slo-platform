"""Composition root: builds every dependency once per process from Settings."""

from __future__ import annotations

from dataclasses import dataclass

import httpx
from agentic_core.audit import AuditLog, JsonlAuditSink
from agentic_core.policy import PolicyEngine, PolicySet
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from sqlalchemy.ext.asyncio import AsyncEngine

from agentops_slo_platform.application.analytics import AnalyticsService
from agentops_slo_platform.application.audit import ChainAuditor
from agentops_slo_platform.application.recovery_service import RecoveryService
from agentops_slo_platform.application.slo_service import SloService
from agentops_slo_platform.config import Settings
from agentops_slo_platform.domain.ports import Clock
from agentops_slo_platform.infrastructure.database import (
    SqlAlertStore,
    SqlPriceStore,
    SqlRecoveryStore,
    SqlSloStore,
    SqlSpanStore,
    SqlWorkflowStore,
    SystemClock,
    create_schema,
    make_engine,
)
from agentops_slo_platform.infrastructure.fabric_client import FabricClient
from agentops_slo_platform.infrastructure.notifier import AlertmanagerNotifier, CredentialProvider
from agentops_slo_platform.ingestion.processor import PriceCache, SpanProcessor
from agentops_slo_platform.ingestion.redaction import Scrubber
from agentops_slo_platform.integrations.a2a import A2AHandler
from agentops_slo_platform.integrations.mcp_server import build_mcp_server
from agentops_slo_platform.observability.metrics import PlatformMetrics
from agentops_slo_platform.security.admin import OperatorAuthenticator
from agentops_slo_platform.security.authz import (
    AccessGuard,
    AgentAuthorizer,
    DecisionCache,
    FabricAuthorizer,
    LocalAuthorizer,
)
from agentops_slo_platform.security.jwks import KeyResolver, RemoteJwks


@dataclass
class Container:
    settings: Settings
    engine: AsyncEngine
    http: httpx.AsyncClient
    clock: Clock
    metrics: PlatformMetrics
    audit_log: AuditLog
    auditor: ChainAuditor
    operators: OperatorAuthenticator
    guard: AccessGuard
    processor: SpanProcessor
    span_store: SqlSpanStore
    workflow_store: SqlWorkflowStore
    prices: SqlPriceStore
    price_cache: PriceCache
    slos: SloService
    analytics: AnalyticsService
    recovery: RecoveryService
    mcp: MCPServer
    a2a: A2AHandler

    async def purge_expired(self) -> int:
        """Delete span facts and workflow runs older than the retention period."""
        cutoff = self.clock.now_ns() - self.settings.retention_days * 86_400 * 1_000_000_000
        removed = await self.span_store.purge_before(cutoff)
        return removed + await self.workflow_store.purge_before(cutoff)

    async def aclose(self) -> None:
        await self.http.aclose()
        await self.engine.dispose()


def _audit_signer(settings: Settings) -> tuple[Ed25519PrivateKey, str]:
    if settings.audit_key_file is None:
        return Ed25519PrivateKey.generate(), "audit-ephemeral"
    key = serialization.load_pem_private_key(settings.audit_key_file.read_bytes(), password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise ValueError("audit key must be Ed25519")
    return key, settings.audit_key_file.stem


def _authorizer(
    settings: Settings, http: httpx.AsyncClient, agent_resolver: KeyResolver | None
) -> AgentAuthorizer:
    if settings.fabric_url is not None:
        return FabricAuthorizer(FabricClient(settings.fabric_url, http=http))
    if not (settings.agent_issuer and settings.agent_audience and settings.agent_jwks_url):
        raise ValueError("agent authorization is not configured")  # pragma: no cover - validated
    return LocalAuthorizer(
        agent_resolver or RemoteJwks(settings.agent_jwks_url, client=http),
        issuer=settings.agent_issuer,
        audience=settings.agent_audience,
        policy=PolicyEngine(PolicySet.from_file(settings.access_policy_file)),
    )


async def build_container(
    settings: Settings,
    *,
    operator_resolver: KeyResolver | None = None,
    agent_resolver: KeyResolver | None = None,
    clock: Clock | None = None,
    http: httpx.AsyncClient | None = None,
) -> Container:
    http = http or httpx.AsyncClient(timeout=httpx.Timeout(5.0))
    clock = clock or SystemClock()
    engine = make_engine(settings.database_url)
    await create_schema(engine)

    metrics = PlatformMetrics(label_limit=settings.metric_label_limit)
    signer, key_id = _audit_signer(settings)
    audit_log = AuditLog(JsonlAuditSink(settings.audit_log_path), signer, key_id=key_id)
    auditor = ChainAuditor(audit_log)

    spans = SqlSpanStore(engine)
    workflows = SqlWorkflowStore(engine)
    price_store = SqlPriceStore(engine)
    price_cache = PriceCache(price_store)
    guard = AccessGuard(
        _authorizer(settings, http, agent_resolver),
        audit=audit_log,
        metrics=metrics,
        cache=DecisionCache(ttl_s=settings.decision_cache_ttl_s),
    )

    notifier = None
    if settings.alertmanager_url:
        credentials = None
        if settings.fabric_url and settings.fabric_agent_id and settings.workload_token_file:
            credentials = CredentialProvider(
                FabricClient(settings.fabric_url, http=http),
                agent_id=settings.fabric_agent_id,
                workload_token_file=settings.workload_token_file,
                clock=clock,
            )
        notifier = AlertmanagerNotifier(
            settings.alertmanager_url,
            http=http,
            metrics=metrics,
            public_url=settings.public_url_str,
            clock=clock,
            resend_horizon_s=int(settings.evaluation_interval_s * 4),
            credentials=credentials,
        )

    slos = SloService(
        slos=SqlSloStore(engine),
        alerts=SqlAlertStore(engine),
        spans=spans,
        workflows=workflows,
        metrics=metrics,
        audit=auditor,
        clock=clock,
        notifier=notifier,
    )
    if settings.slo_seed_file is not None:
        await slos.seed(settings.slo_seed_file)

    analytics = AnalyticsService(
        spans=spans,
        workflows=workflows,
        metrics=metrics,
        clock=clock,
        quiet_s=settings.workflow_quiet_s,
        max_wait_s=settings.workflow_max_wait_s,
        loop_threshold=settings.loop_threshold,
    )
    recovery = RecoveryService(
        spans=spans,
        actions=SqlRecoveryStore(engine),
        policy=PolicyEngine(PolicySet.from_file(settings.recovery_policy_file)),
        metrics=metrics,
        audit=auditor,
        clock=clock,
    )
    processor = SpanProcessor(
        spans=spans,
        workflows=workflows,
        prices=price_cache,
        metrics=metrics,
        scrubber=Scrubber(),
        max_future_skew_s=settings.max_future_skew_s,
        max_age_s=settings.retention_days * 86400,
    )
    mcp = build_mcp_server(
        guard=guard, audit=auditor, slos=slos, analytics=analytics, recovery=recovery
    )
    allowed = settings.mcp_allowed_hosts
    mcp.streamable_http_app(
        streamable_http_path="/mcp",
        stateless_http=True,
        json_response=True,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=bool(allowed), allowed_hosts=allowed
        ),
    )
    a2a = A2AHandler(
        guard=guard,
        audit=auditor,
        slos=slos,
        analytics=analytics,
        recovery=recovery,
        clock=clock,
    )
    return Container(
        settings=settings,
        engine=engine,
        http=http,
        clock=clock,
        metrics=metrics,
        audit_log=audit_log,
        auditor=auditor,
        operators=OperatorAuthenticator(
            operator_resolver or RemoteJwks(settings.operator_jwks_url, client=http),
            issuer=settings.operator_issuer,
            audience=settings.operator_audience,
            roles_claim=settings.operator_roles_claim,
        ),
        guard=guard,
        processor=processor,
        span_store=spans,
        workflow_store=workflows,
        prices=price_store,
        price_cache=price_cache,
        slos=slos,
        analytics=analytics,
        recovery=recovery,
        mcp=mcp,
        a2a=a2a,
    )
