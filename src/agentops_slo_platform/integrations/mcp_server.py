"""MCP server (official ``mcp`` SDK, ``MCPServer``, streamable HTTP) exposing AgentOps tools.

Every tool call is authorized with the caller's bearer credential through the
same :class:`AccessGuard` as the HTTP API (identity fabric or local policy)
and recorded in the signed audit chain. Input and output schemas are the
Pydantic models below, published by ``tools/list``.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Annotated

from agentic_core.domain.classification import DataClassification
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import BaseModel, ConfigDict, Field

from agentops_slo_platform import __version__
from agentops_slo_platform.application.analytics import AnalyticsService, WorkflowView
from agentops_slo_platform.application.recovery_service import Proposer, RecoveryService
from agentops_slo_platform.application.slo_service import FiringAlert, SloService
from agentops_slo_platform.domain.costs import CostGrouping, CostReport
from agentops_slo_platform.domain.errors import PlatformError
from agentops_slo_platform.domain.ports import Auditor
from agentops_slo_platform.domain.recovery import (
    ActionTarget,
    ActionType,
    ProposeAction,
    RecoveryAction,
    RecoveryRecommendation,
)
from agentops_slo_platform.domain.slo import SloStatus
from agentops_slo_platform.domain.spans import SpanKind
from agentops_slo_platform.security.authz import AccessDecision, AccessGuard, Actions

TRACE_ID = r"^[0-9a-f]{32}$"
SLO_ID = r"^[a-z0-9]([a-z0-9\-]{0,62}[a-z0-9])?$"


class FiringAlerts(BaseModel):
    model_config = ConfigDict(frozen=True)

    alerts: tuple[FiringAlert, ...]


def build_mcp_server(
    *,
    guard: AccessGuard,
    audit: Auditor,
    slos: SloService,
    analytics: AnalyticsService,
    recovery: RecoveryService,
) -> MCPServer:
    server: MCPServer = MCPServer(
        name="agentops-slo-platform",
        title="AgentOps SLO Platform",
        description="SLO status, cost attribution, workflow diagnosis and recovery for agents",
        version=__version__,
        instructions=(
            "Read-only operational insight into AI agents plus policy-gated recovery proposals. "
            "High-risk recovery actions are never applied automatically; they wait for an SRE."
        ),
    )

    async def authorized[T](
        ctx: Context,
        *,
        tool: str,
        action: str,
        resource_id: str,
        run: Callable[[AccessDecision], Awaitable[T]],
        classification: DataClassification = DataClassification.INTERNAL,
    ) -> T:
        headers = ctx.headers or {}
        try:
            decision = await guard.require(
                headers.get("authorization"),
                action=action,
                resource_id=resource_id,
                classification=classification,
            )
        except PlatformError as exc:
            raise ToolError(f"{exc.code}: {exc.message}") from exc
        try:
            result = await run(decision)
        except PlatformError as exc:
            await audit(decision.subject, f"mcp.{tool}", resource_id, "error", {"code": exc.code})
            raise ToolError(f"{exc.code}: {exc.message}") from exc
        await audit(decision.subject, f"mcp.{tool}", resource_id, "success")
        return result

    @server.tool(title="SLO status")
    async def slo_status(
        slo_id: Annotated[str, Field(pattern=SLO_ID, description="SLO identifier")],
        ctx: Context,
    ) -> SloStatus:
        """Error budget, burn rates per window and burn-rate rule state of one SLO."""

        async def run(_: AccessDecision) -> SloStatus:
            return await slos.status(slo_id)

        return await authorized(
            ctx, tool="slo_status", action=Actions.SLO_READ, resource_id=f"slo:{slo_id}", run=run
        )

    @server.tool(title="Firing SLO alerts")
    async def firing_alerts(ctx: Context) -> FiringAlerts:
        """Burn-rate alerts currently firing across all SLOs."""

        async def run(_: AccessDecision) -> FiringAlerts:
            return FiringAlerts(alerts=tuple(await slos.firing()))

        return await authorized(
            ctx, tool="firing_alerts", action=Actions.SLO_READ, resource_id="alerts", run=run
        )

    @server.tool(title="Cost report")
    async def cost_report(
        ctx: Context,
        group_by: Annotated[CostGrouping, Field(description="agent, team, model or workflow")],
        hours: Annotated[int, Field(ge=1, le=24 * 93, description="Look-back window")] = 24,
    ) -> CostReport:
        """Token usage and attributed model spend for the last ``hours`` hours."""

        async def run(_: AccessDecision) -> CostReport:
            return await analytics.recent_costs(group_by, hours=hours)

        return await authorized(
            ctx,
            tool="cost_report",
            action=Actions.COST_READ,
            resource_id=f"costs:{group_by.value}",
            run=run,
            classification=DataClassification.CONFIDENTIAL,
        )

    @server.tool(title="Workflow diagnosis")
    async def workflow_diagnosis(
        trace_id: Annotated[str, Field(pattern=TRACE_ID, description="32 hex trace ID")],
        ctx: Context,
    ) -> WorkflowView:
        """Outcome, failure class, root-cause span and recovery strategy for one trace."""

        async def run(_: AccessDecision) -> WorkflowView:
            return await analytics.workflow(trace_id)

        return await authorized(
            ctx,
            tool="workflow_diagnosis",
            action=Actions.WORKFLOW_READ,
            resource_id=f"trace:{trace_id}",
            run=run,
        )

    @server.tool(title="Recovery recommendation")
    async def recovery_recommendation(
        kind: Annotated[SpanKind, Field(description="tool, model or memory")],
        name: Annotated[str, Field(min_length=1, max_length=120)],
        ctx: Context,
    ) -> RecoveryRecommendation:
        """Derived retry policy and circuit-breaker state for a dependency."""

        async def run(_: AccessDecision) -> RecoveryRecommendation:
            return await recovery.recommend(kind, name)

        return await authorized(
            ctx,
            tool="recovery_recommendation",
            action=Actions.RECOVERY_READ,
            resource_id=f"{kind.value}:{name}",
            run=run,
        )

    @server.tool(title="Propose recovery action")
    async def propose_recovery_action(  # noqa: PLR0917 - MCP passes arguments by name
        action: ActionType,
        target_kind: SpanKind,
        target_name: Annotated[str, Field(min_length=1, max_length=120)],
        reason: Annotated[str, Field(min_length=5, max_length=1000)],
        ctx: Context,
        parameters: dict[str, int] | None = None,
        ttl_minutes: Annotated[int, Field(ge=1, le=10_080)] = 60,
    ) -> RecoveryAction:
        """Propose a recovery action. Policy decides: active, pending human approval, or denied."""

        async def run(decision: AccessDecision) -> RecoveryAction:
            return await recovery.propose(
                ProposeAction(
                    action=action,
                    target=ActionTarget(kind=target_kind, name=target_name),
                    parameters=dict(parameters or {}),
                    reason=reason,
                    ttl_minutes=ttl_minutes,
                ),
                proposer=Proposer(id=decision.subject, kind="agent"),
            )

        return await authorized(
            ctx,
            tool="propose_recovery_action",
            action=Actions.RECOVERY_PROPOSE,
            resource_id=f"{target_kind.value}:{target_name}",
            run=run,
        )

    return server
