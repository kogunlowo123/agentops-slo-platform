"""HTTP routes.

Operator endpoints (``/v1/slos``, ``/v1/costs``, ``/v1/recovery/actions`` ...)
require an enterprise OIDC token with an AgentOps role. Agent endpoints
(``/v1/traces``, ``/v1/recovery/directives``, ``/a2a``, ``/mcp``) are
authorized per call through the identity fabric or the local policy.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, Header, Path, Query, Request, Response
from fastapi.responses import JSONResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from sqlalchemy import text

from agentops_slo_platform.api.schemas import (
    AlertHistory,
    Decision,
    Directives,
    FiringAlertList,
    OtlpPartialSuccess,
    PriceList,
    RecoveryActionList,
    SloList,
    WorkflowList,
)
from agentops_slo_platform.application.analytics import GroupBy, OperationReport, WorkflowView
from agentops_slo_platform.application.recovery_service import Proposer
from agentops_slo_platform.application.slo_service import EvaluationSummary
from agentops_slo_platform.domain.costs import CostGrouping, CostReport, PriceEntry
from agentops_slo_platform.domain.errors import (
    PayloadTooLargeError,
    PlatformError,
    UnavailableError,
)
from agentops_slo_platform.domain.recovery import (
    ActionStatus,
    ProposeAction,
    RecoveryAction,
    RecoveryRecommendation,
)
from agentops_slo_platform.domain.slo import SloDefinition, SloStatus
from agentops_slo_platform.domain.spans import SpanKind
from agentops_slo_platform.domain.workflows import Outcome
from agentops_slo_platform.ingestion import otlp
from agentops_slo_platform.integrations.a2a import agent_card
from agentops_slo_platform.runtime.container import Container
from agentops_slo_platform.security.admin import (
    READERS,
    OperatorPrincipal,
    OperatorRole,
    require_role,
)
from agentops_slo_platform.security.authz import Actions

SLO_ID = r"^[a-z0-9]([a-z0-9\-]{0,62}[a-z0-9])?$"
TRACE_ID = r"^[0-9a-f]{32}$"
ACTION_ID = r"^[0-9a-f]{32}$"

router = APIRouter()


def container(request: Request) -> Container:
    value: Container = request.app.state.container
    return value


async def operator(
    request: Request, authorization: Annotated[str | None, Header()] = None
) -> OperatorPrincipal:
    return await container(request).operators.authenticate(authorization)


Deps = Annotated[Container, Depends(container)]
Operator = Annotated[OperatorPrincipal, Depends(operator)]
SloIdPath = Annotated[str, Path(pattern=SLO_ID)]


# ------------------------------------------------------------------ ingestion


@router.post(
    "/v1/traces",
    tags=["ingestion"],
    response_model=OtlpPartialSuccess,
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                "application/x-protobuf": {"schema": {"type": "string", "format": "binary"}},
                "application/json": {"schema": {"type": "object"}},
            },
        }
    },
)
async def ingest_traces(
    request: Request,
    c: Deps,
    authorization: Annotated[str | None, Header()] = None,
) -> Response:
    """OTLP/HTTP trace export (protobuf or JSON, optionally gzip)."""
    decision = await c.guard.require(
        authorization, action=Actions.TRACES_INGEST, resource_id="traces"
    )
    content_type = otlp.media_type(request.headers.get("content-type"))
    limit = c.settings.max_body_bytes
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > limit:
        raise PayloadTooLargeError(f"body exceeds {limit} bytes")
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            raise PayloadTooLargeError(f"body exceeds {limit} bytes")
        chunks.append(chunk)
    body = otlp.inflate(
        b"".join(chunks),
        request.headers.get("content-encoding"),
        max_bytes=c.settings.max_inflated_bytes,
    )
    records, malformed = otlp.records(otlp.parse_request(body, content_type))
    if len(records) > c.settings.max_spans_per_request:
        raise PayloadTooLargeError(
            f"request carries {len(records)} spans; limit {c.settings.max_spans_per_request}"
        )
    for reason in malformed:
        c.metrics.spans_rejected.labels(reason).inc()
    try:
        result = await c.processor.process(
            records, caller=decision.subject, now_ns=c.clock.now_ns()
        )
    except PlatformError:
        raise
    except Exception as exc:  # database or I/O failure: let the exporter retry
        raise UnavailableError("span storage unavailable") from exc
    result.rejected.extend(malformed)
    payload, media = otlp.response(
        content_type, rejected=result.rejected_count, message=result.message()
    )
    return Response(content=payload, media_type=media)


# ---------------------------------------------------------------- discovery


@router.get("/.well-known/agent-card.json", tags=["discovery"])
async def card(c: Deps) -> dict[str, Any]:
    return agent_card(c.settings.public_url_str)


@router.post("/a2a", tags=["a2a"])
async def a2a(
    c: Deps,
    body: Annotated[dict[str, Any], Body()],
    authorization: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    """A2A JSON-RPC 2.0 endpoint (``message/send``)."""
    return await c.a2a.handle(body, authorization)


# ---------------------------------------------------------------------- SLOs


@router.put("/v1/slos/{slo_id}", tags=["slo"])
async def put_slo(
    slo_id: SloIdPath, body: SloDefinition, c: Deps, who: Operator, response: Response
) -> SloDefinition:
    require_role(who, OperatorRole.SRE)
    if body.id != slo_id:
        raise PlatformError("body id must match the path")
    created = await c.slos.put(body, actor=who.subject)
    response.status_code = 201 if created else 200
    return body


@router.get("/v1/slos", tags=["slo"])
async def list_slos(c: Deps, who: Operator) -> SloList:
    require_role(who, *READERS)
    return SloList(items=await c.slos.all())


@router.get("/v1/slos/{slo_id}", tags=["slo"])
async def get_slo(slo_id: SloIdPath, c: Deps, who: Operator) -> SloDefinition:
    require_role(who, *READERS)
    return await c.slos.get(slo_id)


@router.delete("/v1/slos/{slo_id}", status_code=204, tags=["slo"])
async def delete_slo(slo_id: SloIdPath, c: Deps, who: Operator) -> Response:
    require_role(who, OperatorRole.SRE)
    await c.slos.delete(slo_id, actor=who.subject)
    return Response(status_code=204)


@router.get("/v1/slos/{slo_id}/status", tags=["slo"])
async def slo_status(slo_id: SloIdPath, c: Deps, who: Operator) -> SloStatus:
    require_role(who, *READERS)
    return await c.slos.status(slo_id)


@router.get("/v1/slos/{slo_id}/prometheus-rules", tags=["slo"])
async def slo_rules(slo_id: SloIdPath, c: Deps, who: Operator) -> dict[str, Any]:
    require_role(who, *READERS)
    return c.slos.prometheus_rules(await c.slos.get(slo_id))


@router.post("/v1/slos:evaluate", tags=["slo"])
async def evaluate_slos(c: Deps, who: Operator) -> EvaluationSummary:
    require_role(who, OperatorRole.SRE)
    return await c.slos.evaluate_all()


@router.get("/v1/alerts", tags=["slo"])
async def firing_alerts(c: Deps, who: Operator) -> FiringAlertList:
    require_role(who, *READERS)
    return FiringAlertList(items=await c.slos.firing())


@router.get("/v1/alerts/history", tags=["slo"])
async def alert_history(
    c: Deps,
    who: Operator,
    slo_id: Annotated[str | None, Query(pattern=SLO_ID)] = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
) -> AlertHistory:
    require_role(who, *READERS)
    return AlertHistory(items=await c.slos.history(slo_id=slo_id, limit=limit))


# ---------------------------------------------------------- metrics and costs


@router.get("/v1/operations", tags=["metrics"])
async def operations(
    c: Deps,
    who: Operator,
    kind: SpanKind,
    group_by: GroupBy = GroupBy.NAME,
    window_minutes: Annotated[int, Query(ge=1, le=7 * 24 * 60)] = 60,
) -> OperationReport:
    require_role(who, *READERS)
    return await c.analytics.operations(kind, group_by=group_by, window_minutes=window_minutes)


@router.get("/v1/costs", tags=["costs"])
async def costs(
    c: Deps,
    who: Operator,
    group_by: CostGrouping = CostGrouping.TEAM,
    start_ns: Annotated[int | None, Query(ge=0, le=2**62)] = None,
    end_ns: Annotated[int | None, Query(ge=0, le=2**62)] = None,
) -> CostReport:
    require_role(who, *READERS)
    return await c.analytics.costs(group_by, start_ns=start_ns, end_ns=end_ns)


@router.get("/v1/pricing", tags=["costs"])
async def list_prices(c: Deps, who: Operator) -> PriceList:
    require_role(who, *READERS)
    return PriceList(items=await c.prices.all())


@router.put("/v1/pricing", tags=["costs"])
async def put_price(body: PriceEntry, c: Deps, who: Operator) -> PriceEntry:
    require_role(who, OperatorRole.FINOPS)
    await c.prices.put(body, actor=who.subject, now_ns=c.clock.now_ns())
    c.price_cache.invalidate()
    await c.auditor(who.subject, "pricing.set", f"model:{body.model}", "success", body.model_dump())
    return body


# ------------------------------------------------------------------ workflows


@router.get("/v1/workflows", tags=["workflows"])
async def list_workflows(
    *,
    c: Deps,
    who: Operator,
    outcome: Outcome | None = None,
    workflow: Annotated[str | None, Query(max_length=120)] = None,
    hours: Annotated[int, Query(ge=1, le=24 * 35)] = 24,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> WorkflowList:
    require_role(who, *READERS)
    return WorkflowList(
        items=await c.analytics.workflows(
            outcome=outcome, workflow=workflow, hours=hours, limit=limit
        )
    )


@router.get("/v1/workflows/{trace_id}", tags=["workflows"])
async def get_workflow(
    trace_id: Annotated[str, Path(pattern=TRACE_ID)], c: Deps, who: Operator
) -> WorkflowView:
    require_role(who, *READERS)
    return await c.analytics.workflow(trace_id)


# ------------------------------------------------------------------- recovery


@router.get("/v1/recovery/recommendations", tags=["recovery"])
async def recommendations(
    c: Deps,
    who: Operator,
    kind: SpanKind,
    name: Annotated[str, Query(min_length=1, max_length=120)],
) -> RecoveryRecommendation:
    require_role(who, *READERS)
    return await c.recovery.recommend(kind, name)


@router.post("/v1/recovery/actions", status_code=201, tags=["recovery"])
async def propose(body: ProposeAction, c: Deps, who: Operator) -> RecoveryAction:
    require_role(who, OperatorRole.SRE)
    return await c.recovery.propose(
        body, proposer=Proposer(id=who.subject, kind="user", roles=who.roles)
    )


@router.get("/v1/recovery/actions", tags=["recovery"])
async def list_actions(
    c: Deps,
    who: Operator,
    status: ActionStatus | None = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> RecoveryActionList:
    require_role(who, *READERS)
    return RecoveryActionList(items=await c.recovery.find(status=status, limit=limit))


@router.get("/v1/recovery/actions/{action_id}", tags=["recovery"])
async def get_action(
    action_id: Annotated[str, Path(pattern=ACTION_ID)], c: Deps, who: Operator
) -> RecoveryAction:
    require_role(who, *READERS)
    return await c.recovery.get(action_id)


@router.post("/v1/recovery/actions/{action_id}/approve", tags=["recovery"])
async def approve(
    action_id: Annotated[str, Path(pattern=ACTION_ID)], body: Decision, c: Deps, who: Operator
) -> RecoveryAction:
    require_role(who, OperatorRole.SRE)
    return await c.recovery.decide(
        action_id, approve=True, approver=who.subject, comment=body.comment
    )


@router.post("/v1/recovery/actions/{action_id}/reject", tags=["recovery"])
async def reject(
    action_id: Annotated[str, Path(pattern=ACTION_ID)], body: Decision, c: Deps, who: Operator
) -> RecoveryAction:
    require_role(who, OperatorRole.SRE)
    return await c.recovery.decide(
        action_id, approve=False, approver=who.subject, comment=body.comment
    )


@router.get("/v1/recovery/directives", tags=["recovery"])
async def directives(c: Deps, authorization: Annotated[str | None, Header()] = None) -> Directives:
    """Active recovery directives for agent runtimes and gateways to apply."""
    await c.guard.require(authorization, action=Actions.DIRECTIVES_READ, resource_id="directives")
    return Directives(items=await c.recovery.directives(), generated_at_ns=c.clock.now_ns())


# ----------------------------------------------------------------- operations


@router.get("/healthz", tags=["operations"], include_in_schema=False)
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/readyz", tags=["operations"], include_in_schema=False)
async def readyz(c: Deps) -> JSONResponse:
    try:
        async with c.engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception:
        return JSONResponse({"status": "database unavailable"}, status_code=503)
    return JSONResponse({"status": "ready"})


@router.get("/metrics", tags=["operations"], include_in_schema=False)
async def metrics(c: Deps) -> Response:
    return Response(generate_latest(c.metrics.registry), media_type=CONTENT_TYPE_LATEST)
