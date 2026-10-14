"""A2A (Agent2Agent) interface: Agent Card and JSON-RPC ``message/send``.

Peers send a Message whose first DataPart names a skill and its arguments,
e.g. ``{"skill": "slo-status", "slo_id": "claims-tool-availability"}``.
Text-only messages are routed deterministically: a 32-hex trace ID asks for
a workflow diagnosis, a mention of cost or spend asks for a cost report,
anything else gets the operational health summary. The reply is a
completed Task whose artifact carries the structured result as a DataPart
and a one-paragraph summary as a TextPart.

Authentication is transport-level (HTTP bearer, per the A2A specification);
each skill is then authorized separately with its own action.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from agentic_core.domain.classification import DataClassification
from agentic_core.domain.identifiers import new_id
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from agentops_slo_platform import __version__
from agentops_slo_platform.application.analytics import AnalyticsService
from agentops_slo_platform.application.recovery_service import RecoveryService
from agentops_slo_platform.application.slo_service import SloService
from agentops_slo_platform.domain.costs import CostGrouping
from agentops_slo_platform.domain.errors import ForbiddenError, PlatformError, UnauthorizedError
from agentops_slo_platform.domain.ports import Auditor, Clock
from agentops_slo_platform.domain.spans import SpanKind
from agentops_slo_platform.security.authz import AccessGuard, Actions

PROTOCOL_VERSION = "0.3.0"
PARSE_ERROR, INVALID_REQUEST, METHOD_NOT_FOUND, INVALID_PARAMS = -32700, -32600, -32601, -32602
CONTENT_TYPE_NOT_SUPPORTED = -32005
_TRACE = re.compile(r"\b[0-9a-f]{32}\b")
_COST = re.compile(r"\b(cost|costs|spend|spending|token usage|budget)\b", re.I)


class SkillSpec(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    name: str
    description: str
    tags: tuple[str, ...]
    examples: tuple[str, ...]
    action: str


SKILLS: tuple[SkillSpec, ...] = (
    SkillSpec(
        id="health-summary",
        name="Operational health summary",
        description="Health of every SLO and the burn-rate alerts currently firing.",
        tags=("slo", "alerts", "health"),
        examples=("How healthy are our agents right now?",),
        action=Actions.SLO_READ,
    ),
    SkillSpec(
        id="slo-status",
        name="SLO status",
        description="Error budget, per-window burn rates and alert rule state of one SLO.",
        tags=("slo", "error-budget", "burn-rate"),
        examples=('{"skill": "slo-status", "slo_id": "claims-tool-availability"}',),
        action=Actions.SLO_READ,
    ),
    SkillSpec(
        id="cost-report",
        name="Cost report",
        description="Token usage and attributed spend by agent, team, model or workflow.",
        tags=("finops", "tokens", "cost"),
        examples=('{"skill": "cost-report", "group_by": "team", "hours": 24}',),
        action=Actions.COST_READ,
    ),
    SkillSpec(
        id="workflow-diagnosis",
        name="Workflow diagnosis",
        description="Outcome, failure class, root cause and recovery strategy of one trace.",
        tags=("workflow", "rca", "tracing"),
        examples=("Why did trace 4bf92f3577b34da6a3ce929d0e0e4736 fail?",),
        action=Actions.WORKFLOW_READ,
    ),
    SkillSpec(
        id="recovery-recommendation",
        name="Recovery recommendation",
        description="Retry policy and circuit-breaker state derived for a dependency.",
        tags=("recovery", "retry", "circuit-breaker"),
        examples=('{"skill": "recovery-recommendation", "kind": "tool", "name": "crm.lookup"}',),
        action=Actions.RECOVERY_READ,
    ),
)
_BY_ID = {s.id: s for s in SKILLS}


def agent_card(public_url: str) -> dict[str, Any]:
    return {
        "protocolVersion": PROTOCOL_VERSION,
        "name": "AgentOps SLO Platform",
        "description": (
            "Observability and reliability agent for enterprise AI agents: SLO error budgets, "
            "burn-rate alerts, cost attribution, workflow failure diagnosis and recovery advice."
        ),
        "url": f"{public_url}/a2a",
        "preferredTransport": "JSONRPC",
        "version": __version__,
        "provider": {
            "organization": "kogunlowo123",
            "url": "https://github.com/kogunlowo123/agentops-slo-platform",
        },
        "documentationUrl": "https://github.com/kogunlowo123/agentops-slo-platform",
        "capabilities": {
            "streaming": False,
            "pushNotifications": False,
            "stateTransitionHistory": False,
        },
        "defaultInputModes": ["application/json", "text/plain"],
        "defaultOutputModes": ["application/json", "text/plain"],
        "securitySchemes": {
            "bearer": {
                "type": "http",
                "scheme": "bearer",
                "bearerFormat": "JWT",
                "description": "Agent credential issued by agent-identity-fabric",
            }
        },
        "security": [{"bearer": []}],
        "skills": [
            {
                "id": s.id,
                "name": s.name,
                "description": s.description,
                "tags": list(s.tags),
                "examples": list(s.examples),
                "inputModes": ["application/json", "text/plain"],
                "outputModes": ["application/json", "text/plain"],
            }
            for s in SKILLS
        ],
    }


class RpcError(Exception):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class Part(BaseModel):
    model_config = ConfigDict(extra="allow")

    kind: str
    text: str | None = None
    data: dict[str, Any] | None = None


class Message(BaseModel):
    model_config = ConfigDict(extra="allow")

    role: str
    parts: list[Part] = Field(min_length=1, max_length=20)
    message_id: str = Field(alias="messageId", min_length=1, max_length=128)
    context_id: str | None = Field(default=None, alias="contextId", max_length=128)
    kind: str = "message"


class SendParams(BaseModel):
    model_config = ConfigDict(extra="allow")

    message: Message


def route(message: Message) -> tuple[SkillSpec, dict[str, Any]]:
    for part in message.parts:
        if part.kind == "data" and part.data is not None:
            arguments = dict(part.data)
            skill = arguments.pop("skill", "health-summary")
            if not isinstance(skill, str) or skill not in _BY_ID:
                raise RpcError(INVALID_PARAMS, f"unknown skill {skill!r}")
            return _BY_ID[skill], arguments
    text = " ".join(p.text for p in message.parts if p.kind == "text" and p.text)
    if not text:
        raise RpcError(CONTENT_TYPE_NOT_SUPPORTED, "message needs a text or data part")
    if trace := _TRACE.search(text.lower()):
        return _BY_ID["workflow-diagnosis"], {"trace_id": trace.group(0)}
    if _COST.search(text):
        return _BY_ID["cost-report"], {"group_by": "team", "hours": 24}
    return _BY_ID["health-summary"], {}


class A2AHandler:
    def __init__(
        self,
        *,
        guard: AccessGuard,
        audit: Auditor,
        slos: SloService,
        analytics: AnalyticsService,
        recovery: RecoveryService,
        clock: Clock,
    ) -> None:
        self._clock = clock
        self._guard = guard
        self._audit = audit
        self._slos = slos
        self._analytics = analytics
        self._recovery = recovery
        self._skills: dict[str, Callable[[dict[str, Any]], Awaitable[tuple[Any, str]]]] = {
            "health-summary": self._health,
            "slo-status": self._slo_status,
            "cost-report": self._cost_report,
            "workflow-diagnosis": self._workflow,
            "recovery-recommendation": self._recommendation,
        }

    async def handle(self, body: Any, authorization: str | None) -> dict[str, Any]:
        """JSON-RPC envelope. Auth errors propagate (HTTP 401/403); the rest become RPC errors."""
        request_id = body.get("id") if isinstance(body, dict) else None
        try:
            if not isinstance(body, dict) or body.get("jsonrpc") != "2.0":
                raise RpcError(INVALID_REQUEST, "expected a JSON-RPC 2.0 request object")
            if body.get("method") != "message/send":
                raise RpcError(METHOD_NOT_FOUND, f"method {body.get('method')!r} not supported")
            try:
                params = SendParams.model_validate(body.get("params"))
            except ValidationError as exc:
                raise RpcError(INVALID_PARAMS, "params.message is invalid") from exc
            result = await self._send(params.message, authorization)
        except RpcError as exc:
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": exc.code, "message": exc.message},
            }
        return {"jsonrpc": "2.0", "id": request_id, "result": result}

    async def _send(self, message: Message, authorization: str | None) -> dict[str, Any]:
        skill, arguments = route(message)
        classification = (
            DataClassification.CONFIDENTIAL
            if skill.id == "cost-report"
            else DataClassification.INTERNAL
        )
        decision = await self._guard.require(
            authorization,
            action=skill.action,
            resource_id=f"a2a:{skill.id}",
            classification=classification,
        )
        state, parts = "completed", []
        try:
            data, summary = await self._skills[skill.id](arguments)
            parts = [{"kind": "data", "data": data}, {"kind": "text", "text": summary}]
        except (UnauthorizedError, ForbiddenError):
            raise
        except (PlatformError, ValidationError, ValueError) as exc:
            state = "failed"
            detail = exc.message if isinstance(exc, PlatformError) else "invalid skill arguments"
            parts = [{"kind": "text", "text": detail}]
        await self._audit(decision.subject, f"a2a.{skill.id}", f"a2a:{skill.id}", state)
        context = message.context_id or new_id()
        now = (
            datetime.fromtimestamp(self._clock.now_ns() / 1e9, tz=UTC)
            .isoformat()
            .replace("+00:00", "Z")
        )
        return {
            "kind": "task",
            "id": new_id(),
            "contextId": context,
            "status": {"state": state, "timestamp": now},
            "artifacts": [{"artifactId": new_id(), "name": skill.id, "parts": parts}],
            "history": [message.model_dump(by_alias=True, exclude_none=True)],
            "metadata": {"skill": skill.id},
        }

    # --------------------------------------------------------------------- skills

    async def _health(self, _: dict[str, Any]) -> tuple[Any, str]:
        statuses = [await self._slos.status(s.id) for s in await self._slos.all()]
        firing = await self._slos.firing()
        data = {
            "slos": [
                {
                    "slo_id": s.slo_id,
                    "health": s.health.value,
                    "error_budget_remaining": s.error_budget_remaining,
                }
                for s in statuses
            ],
            "firing_alerts": [a.model_dump(mode="json") for a in firing],
        }
        unhealthy = [s.slo_id for s in statuses if s.health.value in ("warning", "critical")]
        summary = f"{len(statuses)} SLOs evaluated; {len(firing)} burn-rate alerts firing; " + (
            f"attention needed on {', '.join(unhealthy)}." if unhealthy else "all healthy."
        )
        return data, summary

    async def _slo_status(self, arguments: dict[str, Any]) -> tuple[Any, str]:
        slo_id = arguments.get("slo_id")
        if not isinstance(slo_id, str):
            raise PlatformError("slo_id is required")
        status = await self._slos.status(slo_id)
        firing = [r.rule_id for r in status.rules if r.firing]
        summary = (
            f"SLO {status.slo_id} is {status.health.value}: "
            f"{status.error_budget_remaining:.1%} of the error budget remains"
            + (f"; firing rules {', '.join(firing)}." if firing else ".")
        )
        return status.model_dump(mode="json"), summary

    async def _cost_report(self, arguments: dict[str, Any]) -> tuple[Any, str]:
        group_by = CostGrouping(arguments.get("group_by", "team"))
        hours = int(arguments.get("hours", 24))
        if not 1 <= hours <= 24 * 93:
            raise PlatformError("hours must be between 1 and 2232")
        report = await self._analytics.recent_costs(group_by, hours=hours)
        top = report.rows[0].key if report.rows else "none"
        summary = (
            f"{report.total_cost_usd:.2f} {report.currency} attributed over {hours}h across "
            f"{len(report.rows)} {group_by.value}s; highest: {top}."
        )
        return report.model_dump(mode="json"), summary

    async def _workflow(self, arguments: dict[str, Any]) -> tuple[Any, str]:
        trace_id = str(arguments.get("trace_id", ""))
        if not _TRACE.fullmatch(trace_id):
            raise PlatformError("trace_id must be 32 lowercase hex characters")
        view = await self._analytics.workflow(trace_id)
        d = view.diagnosis
        cause = (
            f" Root cause: {d.failure_class.value} in {d.root_cause_kind.value} "
            f"{d.root_cause_name}; recovery: {', '.join(r.value for r in d.recovery)}."
            if d.failure_class and d.root_cause_kind
            else ""
        )
        summary = f"Workflow {d.workflow} ({trace_id}) outcome: {d.outcome.value}.{cause}"
        return view.model_dump(mode="json"), summary

    async def _recommendation(self, arguments: dict[str, Any]) -> tuple[Any, str]:
        kind = SpanKind(arguments.get("kind", "tool"))
        name = arguments.get("name")
        if not isinstance(name, str) or not name:
            raise PlatformError("name is required")
        result = await self._recovery.recommend(kind, name)
        summary = (
            f"{kind.value} {name}: circuit {result.circuit.state.value}; retry up to "
            f"{result.retry_policy.max_attempts} attempts. {result.retry_policy.rationale}"
        )
        return result.model_dump(mode="json"), summary
