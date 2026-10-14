"""Recovery recommendations, policy-gated recovery actions and the directive feed.

The platform never changes production behaviour itself. An action that the
recovery policy allows becomes an active *directive* for its TTL; agent
runtimes and gateways poll ``/v1/recovery/directives`` and apply it (for
example, stop calling an opened-circuit tool). Actions the policy marks
``require_approval`` wait for an SRE other than the proposer (four eyes).
"""

from __future__ import annotations

from typing import Any

from agentic_core.domain.classification import Decision
from agentic_core.domain.identifiers import new_id
from agentic_core.policy import PolicyEngine, PolicyRequest, Resource, Subject
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from agentops_slo_platform.domain.errors import (
    ConflictError,
    ForbiddenError,
    InvalidStateError,
    PlatformError,
)
from agentops_slo_platform.domain.ports import Auditor, Clock
from agentops_slo_platform.domain.recovery import (
    ACTION_RISK,
    MAX_ATTEMPTS_CAP,
    ActionStatus,
    ActionType,
    ProposeAction,
    RecoveryAction,
    RecoveryRecommendation,
    dependency_stats,
    recommend_circuit,
    recommend_retry_policy,
)
from agentops_slo_platform.domain.spans import SpanKind
from agentops_slo_platform.infrastructure.database import SqlRecoveryStore, SqlSpanStore
from agentops_slo_platform.observability.metrics import PlatformMetrics

NS_PER_MIN = 60 * 1_000_000_000
DEPENDENCY_KINDS = frozenset({SpanKind.TOOL, SpanKind.MODEL, SpanKind.MEMORY})


class RetryParameters(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_attempts: int = Field(ge=1, le=MAX_ATTEMPTS_CAP)
    initial_backoff_ms: int = Field(default=100, ge=0, le=60_000)
    max_backoff_ms: int = Field(default=10_000, ge=0, le=600_000)
    per_attempt_timeout_ms: int | None = Field(default=None, ge=1, le=3_600_000)


class CircuitParameters(BaseModel):
    model_config = ConfigDict(extra="forbid")

    half_open_after_s: int = Field(default=60, ge=5, le=3600)


class BudgetParameters(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_tokens_per_run: int = Field(gt=0, le=50_000_000)


class PauseParameters(BaseModel):
    model_config = ConfigDict(extra="forbid")


_PARAMETERS: dict[ActionType, type[BaseModel]] = {
    ActionType.APPLY_RETRY_POLICY: RetryParameters,
    ActionType.OPEN_CIRCUIT: CircuitParameters,
    ActionType.CLOSE_CIRCUIT: PauseParameters,
    ActionType.RAISE_BUDGET: BudgetParameters,
    ActionType.PAUSE_AGENT: PauseParameters,
}


_TARGETS: dict[ActionType, frozenset[SpanKind]] = {
    ActionType.APPLY_RETRY_POLICY: DEPENDENCY_KINDS,
    ActionType.OPEN_CIRCUIT: DEPENDENCY_KINDS,
    ActionType.CLOSE_CIRCUIT: DEPENDENCY_KINDS,
    ActionType.RAISE_BUDGET: frozenset({SpanKind.AGENT, SpanKind.WORKFLOW}),
    ActionType.PAUSE_AGENT: frozenset({SpanKind.AGENT}),
}


class Proposer(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    kind: str = Field(description="user (operator) or agent")
    roles: frozenset[str] = frozenset()
    scopes: frozenset[str] = frozenset()


class RecoveryService:
    def __init__(
        self,
        *,
        spans: SqlSpanStore,
        actions: SqlRecoveryStore,
        policy: PolicyEngine,
        metrics: PlatformMetrics,
        audit: Auditor,
        clock: Clock,
        short_window_minutes: int = 5,
        long_window_minutes: int = 30,
        open_threshold: float = 0.5,
        min_calls: int = 20,
    ) -> None:
        self._spans = spans
        self._actions = actions
        self._policy = policy
        self._metrics = metrics
        self._audit = audit
        self._clock = clock
        self._short = short_window_minutes
        self._long = long_window_minutes
        self._open_threshold = open_threshold
        self._min_calls = min_calls

    async def recommend(self, kind: SpanKind, name: str) -> RecoveryRecommendation:
        if kind not in DEPENDENCY_KINDS:
            raise PlatformError("recommendations apply to tool, model and memory dependencies")
        now = self._clock.now_ns()
        long = await self._spans.calls(
            kind, name, since_ns=now - self._long * NS_PER_MIN, until_ns=now
        )
        short_cutoff = now - self._short * NS_PER_MIN
        short = [c for c in long if c.end_ns >= short_cutoff]
        stats = dependency_stats(kind, name, long)
        return RecoveryRecommendation(
            stats=stats,
            retry_policy=recommend_retry_policy(stats),
            circuit=recommend_circuit(
                short,
                long,
                short_minutes=self._short,
                long_minutes=self._long,
                open_threshold=self._open_threshold,
                min_calls=self._min_calls,
            ),
        )

    @staticmethod
    def _validate(command: ProposeAction) -> dict[str, Any]:
        allowed = _TARGETS[command.action]
        if command.target.kind not in allowed:
            kinds = ", ".join(sorted(k.value for k in allowed))
            raise PlatformError(f"{command.action.value} targets one of: {kinds}")
        try:
            parameters = _PARAMETERS[command.action].model_validate(command.parameters)
        except ValidationError as exc:
            raise PlatformError(f"invalid parameters for {command.action}: {exc}") from exc
        return parameters.model_dump(exclude_none=True)

    async def propose(self, command: ProposeAction, *, proposer: Proposer) -> RecoveryAction:
        parameters = self._validate(command)
        now = self._clock.now_ns()
        target = (command.target.kind, command.target.name)
        live = await self._actions.find(
            statuses=[ActionStatus.ACTIVE, ActionStatus.PENDING_APPROVAL], target=target, limit=50
        )
        for existing in live:
            if existing.action is command.action and existing.effective_status(now) in (
                ActionStatus.ACTIVE,
                ActionStatus.PENDING_APPROVAL,
            ):
                raise ConflictError(
                    f"{command.action} on {command.target.name} already {existing.status.value} "
                    f"as {existing.id}"
                )
        risk = ACTION_RISK[command.action]
        decision = self._policy.evaluate(
            PolicyRequest(
                subject=Subject(
                    id=proposer.id,
                    kind=proposer.kind,
                    roles=proposer.roles,
                    attributes={"kind": proposer.kind, "scopes": sorted(proposer.scopes)},
                ),
                action=f"recovery:{command.action.value}",
                resource=Resource(type=f"dependency:{command.target.kind.value}", id=target[1]),
                risk=risk,
                environment={"parameters": parameters},
            )
        )
        status = {
            Decision.ALLOW: ActionStatus.ACTIVE,
            Decision.REQUIRE_APPROVAL: ActionStatus.PENDING_APPROVAL,
            Decision.DENY: ActionStatus.DENIED,
        }[decision.decision]
        action = RecoveryAction(
            id=new_id(),
            action=command.action,
            target=command.target,
            parameters=parameters,
            reason=command.reason,
            risk=risk,
            status=status,
            decision=decision.decision,
            matched_rules=decision.matched_rules,
            policy_version=decision.policy_version,
            policy_digest=decision.policy_digest,
            proposed_by=proposer.id,
            proposed_ns=now,
            decided_by="policy" if status is not ActionStatus.PENDING_APPROVAL else None,
            decided_ns=now if status is not ActionStatus.PENDING_APPROVAL else None,
            expires_ns=now + command.ttl_minutes * NS_PER_MIN,
            ttl_minutes=command.ttl_minutes,
        )
        await self._actions.add(action)
        self._metrics.recovery_actions.labels(command.action.value, status.value).inc()
        await self._audit(
            proposer.id,
            f"recovery.propose.{command.action.value}",
            f"{command.target.kind.value}:{command.target.name}",
            decision.decision.value,
            {
                "action_id": action.id,
                "risk": int(risk),
                "rules": list(decision.matched_rules),
                "policy_digest": decision.policy_digest,
                "parameters": parameters,
            },
        )
        return action

    async def decide(
        self, action_id: str, *, approve: bool, approver: str, comment: str = ""
    ) -> RecoveryAction:
        action = await self._actions.get(action_id)
        now = self._clock.now_ns()
        if action.effective_status(now) is not ActionStatus.PENDING_APPROVAL:
            raise InvalidStateError(f"action {action_id} is {action.effective_status(now).value}")
        if approver == action.proposed_by:
            raise ForbiddenError("the proposer cannot approve their own recovery action")
        decided = action.model_copy(
            update={
                "status": ActionStatus.ACTIVE if approve else ActionStatus.REJECTED,
                "decided_by": approver,
                "decided_ns": now,
                "expires_ns": now + action.ttl_minutes * NS_PER_MIN,
            }
        )
        if not await self._actions.replace(decided, expected=ActionStatus.PENDING_APPROVAL):
            raise ConflictError(f"action {action_id} was decided concurrently")
        self._metrics.recovery_actions.labels(action.action.value, decided.status.value).inc()
        await self._audit(
            approver,
            "recovery.approve" if approve else "recovery.reject",
            f"{action.target.kind.value}:{action.target.name}",
            "success",
            {
                "action_id": action.id,
                "action": action.action.value,
                "proposer": action.proposed_by,
                "comment": comment,
            },
        )
        return decided

    async def get(self, action_id: str) -> RecoveryAction:
        action = await self._actions.get(action_id)
        return action.model_copy(update={"status": action.effective_status(self._clock.now_ns())})

    async def find(self, *, status: ActionStatus | None, limit: int) -> list[RecoveryAction]:
        now = self._clock.now_ns()
        stored_statuses = None
        if status is ActionStatus.EXPIRED:
            stored_statuses = [ActionStatus.ACTIVE, ActionStatus.PENDING_APPROVAL]
        elif status is not None:
            stored_statuses = [status]
        actions = [
            a.model_copy(update={"status": a.effective_status(now)})
            for a in await self._actions.find(statuses=stored_statuses, target=None, limit=limit)
        ]
        return [a for a in actions if status is None or a.status is status]

    async def directives(self) -> list[RecoveryAction]:
        return await self._actions.active(now_ns=self._clock.now_ns())
