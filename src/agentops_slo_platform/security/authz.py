"""Authorization of calls made by agents (OTLP exporters, MCP clients, A2A peers).

With ``AGENTOPS_FABRIC_URL`` set, every decision comes from the identity
fabric: the caller's credential is introspected (active, expiry) and then
authorized for the action. Without it, the credential is verified locally
against a configured issuer's JWKS and the local policy set decides. There
is no allow-all mode: configuration validation refuses to start without one
of the two.

Decisions are cached briefly per (credential digest, action, resource) and
never beyond the credential's expiry, so steady OTLP export does not put the
fabric on the hot path while revocations still take effect within the TTL.
"""

from __future__ import annotations

import fnmatch
import hashlib
import time
from collections import OrderedDict
from collections.abc import Callable
from typing import Any, Literal, Protocol

from agentic_core.audit import AuditLog
from agentic_core.domain.classification import DataClassification, Decision, RiskLevel
from agentic_core.observability import correlation_id
from agentic_core.policy import PolicyEngine, PolicyRequest, Resource, Subject
from pydantic import BaseModel, ConfigDict

from agentops_slo_platform.domain.errors import ForbiddenError, UnauthorizedError
from agentops_slo_platform.infrastructure.fabric_client import (
    FabricAuthorizeRequest,
    FabricClient,
)
from agentops_slo_platform.observability.metrics import PlatformMetrics
from agentops_slo_platform.security.jwks import KeyResolver, verify_jwt

RESOURCE_TYPE = "agentops"


class Actions:
    """Authorization action names (fabric scopes ``agentops:<name>``)."""

    SLO_READ = "agentops:slo.read"
    COST_READ = "agentops:cost.read"
    WORKFLOW_READ = "agentops:workflow.read"
    RECOVERY_READ = "agentops:recovery.read"
    RECOVERY_PROPOSE = "agentops:recovery.propose"
    DIRECTIVES_READ = "agentops:directives.read"
    TRACES_INGEST = "agentops:traces.ingest"
    A2A_INVOKE = "agentops:a2a.invoke"


class AccessDecision(BaseModel):
    model_config = ConfigDict(frozen=True)

    decision: Decision
    subject: str
    action: str
    resource: str
    reason: str
    matched_rules: tuple[str, ...]
    policy_digest: str
    source: Literal["fabric", "local"]
    expires_at: float | None = None  # unix seconds of the credential's expiry


class AgentAuthorizer(Protocol):
    async def decide(
        self,
        token: str,
        *,
        action: str,
        resource_id: str,
        classification: DataClassification,
        environment: dict[str, Any],
    ) -> AccessDecision: ...


class FabricAuthorizer:
    def __init__(self, client: FabricClient) -> None:
        self._client = client

    async def decide(
        self,
        token: str,
        *,
        action: str,
        resource_id: str,
        classification: DataClassification,
        environment: dict[str, Any],
    ) -> AccessDecision:
        status = await self._client.introspect(token)
        if not status.active:
            raise UnauthorizedError("credential is not active")
        decision = await self._client.authorize(
            token,
            FabricAuthorizeRequest(
                action=action,
                resource_type=RESOURCE_TYPE,
                resource_id=resource_id,
                resource_classification=classification,
                environment=environment,
            ),
        )
        return AccessDecision(
            decision=decision.decision,
            subject=decision.agent,
            action=action,
            resource=f"{RESOURCE_TYPE}:{resource_id}",
            reason=decision.reason,
            matched_rules=decision.matched_rules,
            policy_digest=decision.policy_digest,
            source="fabric",
            expires_at=None if status.exp is None else float(status.exp),
        )


class LocalAuthorizer:
    """JWKS verification plus the local policy set (no fabric deployment)."""

    def __init__(
        self, resolver: KeyResolver, *, issuer: str, audience: str, policy: PolicyEngine
    ) -> None:
        self._resolver = resolver
        self._issuer = issuer
        self._audience = audience
        self._policy = policy

    async def decide(
        self,
        token: str,
        *,
        action: str,
        resource_id: str,
        classification: DataClassification,
        environment: dict[str, Any],
    ) -> AccessDecision:
        claims = await verify_jwt(
            token, self._resolver, issuer=self._issuer, audience=self._audience
        )
        raw_scope = claims.get("scope", "")
        scopes = sorted(set(raw_scope.split())) if isinstance(raw_scope, str) else []
        risk = claims.get("risk", int(RiskLevel.MODERATE))
        risk_level = RiskLevel(max(1, min(4, risk))) if isinstance(risk, int) else RiskLevel.HIGH
        max_cls = claims.get("max_cls", int(DataClassification.INTERNAL))
        subject = Subject(
            id=str(claims["sub"]),
            kind="agent",
            attributes={
                "status": "active",
                "scopes": scopes,
                "risk": int(risk_level),
                "max_classification": max_cls if isinstance(max_cls, int) else 0,
                "delegated": "act" in claims,
                "action_in_scope": any(fnmatch.fnmatchcase(action, s) for s in scopes),
            },
        )
        result = self._policy.evaluate(
            PolicyRequest(
                subject=subject,
                action=action,
                resource=Resource(
                    type=RESOURCE_TYPE, id=resource_id, classification=classification
                ),
                risk=risk_level,
                environment=environment,
            )
        )
        decision, reason, rules = result.decision, result.reason, result.matched_rules
        ceiling = subject.attributes["max_classification"]
        if classification > ceiling and decision is not Decision.DENY:
            decision, reason, rules = Decision.DENY, "resource exceeds agent classification", ()
        exp = claims.get("exp")
        return AccessDecision(
            decision=decision,
            subject=subject.id,
            action=action,
            resource=f"{RESOURCE_TYPE}:{resource_id}",
            reason=reason,
            matched_rules=rules,
            policy_digest=result.policy_digest,
            source="local",
            expires_at=float(exp) if isinstance(exp, int | float) else None,
        )


class DecisionCache:
    def __init__(
        self,
        *,
        ttl_s: float,
        max_entries: int = 10_000,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._ttl = ttl_s
        self._max = max_entries
        self._clock = clock
        self._entries: OrderedDict[tuple[str, str, str], tuple[float, AccessDecision]] = (
            OrderedDict()
        )

    @staticmethod
    def key(token: str, action: str, resource_id: str) -> tuple[str, str, str]:
        return hashlib.sha256(token.encode()).hexdigest(), action, resource_id

    def get(self, key: tuple[str, str, str]) -> AccessDecision | None:
        entry = self._entries.get(key)
        if entry is None:
            return None
        if entry[0] <= self._clock():
            del self._entries[key]
            return None
        self._entries.move_to_end(key)
        return entry[1]

    def put(self, key: tuple[str, str, str], decision: AccessDecision) -> None:
        if self._ttl <= 0:
            return
        until = self._clock() + self._ttl
        if decision.expires_at is not None:
            until = min(until, decision.expires_at)
        self._entries[key] = (until, decision)
        self._entries.move_to_end(key)
        while len(self._entries) > self._max:
            self._entries.popitem(last=False)


def bearer_token(authorization: str | None) -> str:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise UnauthorizedError("agent bearer token required")
    token = authorization[7:].strip()
    if not token or len(token) > 8192:
        raise UnauthorizedError("malformed bearer token")
    return token


class AccessGuard:
    """Authorize, audit and measure every agent call. Only ALLOW proceeds."""

    def __init__(
        self,
        authorizer: AgentAuthorizer,
        *,
        audit: AuditLog,
        metrics: PlatformMetrics,
        cache: DecisionCache,
    ) -> None:
        self._authorizer = authorizer
        self._audit = audit
        self._metrics = metrics
        self._cache = cache

    async def require(
        self,
        authorization: str | None,
        *,
        action: str,
        resource_id: str = "*",
        classification: DataClassification = DataClassification.INTERNAL,
        environment: dict[str, Any] | None = None,
    ) -> AccessDecision:
        try:
            token = bearer_token(authorization)
        except UnauthorizedError:
            # Counted, not audited: anonymous probes must not be able to flood the evidence chain.
            self._metrics.authz_decisions.labels(action, "missing_credentials").inc()
            raise
        key = DecisionCache.key(token, action, resource_id)
        decision = self._cache.get(key)
        if decision is None:
            try:
                decision = await self._authorizer.decide(
                    token,
                    action=action,
                    resource_id=resource_id,
                    classification=classification,
                    environment=environment or {},
                )
            except UnauthorizedError:
                self._metrics.authz_decisions.labels(action, "unauthenticated").inc()
                await self._audit.record(
                    actor="anonymous",
                    action=action,
                    resource=f"{RESOURCE_TYPE}:{resource_id}",
                    outcome="unauthenticated",
                    correlation_id=correlation_id(),
                )
                raise
            self._cache.put(key, decision)
            await self._audit.record(
                actor=decision.subject,
                action=action,
                resource=decision.resource,
                outcome=decision.decision.value,
                correlation_id=correlation_id(),
                details={
                    "rules": list(decision.matched_rules),
                    "policy_digest": decision.policy_digest,
                    "source": decision.source,
                },
            )
        self._metrics.authz_decisions.labels(action, decision.decision.value).inc()
        if decision.decision is not Decision.ALLOW:
            raise ForbiddenError(f"{action} not permitted: {decision.reason}")
        return decision
