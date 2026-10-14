"""Operation metrics, cost attribution and workflow classification use cases."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from agentops_slo_platform.domain.costs import CostGrouping, CostReport
from agentops_slo_platform.domain.errors import NotFoundError, PlatformError
from agentops_slo_platform.domain.ports import Clock
from agentops_slo_platform.domain.recovery import percentile
from agentops_slo_platform.domain.spans import SpanKind
from agentops_slo_platform.domain.workflows import Diagnosis, Outcome, diagnose
from agentops_slo_platform.infrastructure.database import SqlSpanStore, SqlWorkflowStore
from agentops_slo_platform.observability.metrics import PlatformMetrics

NS_PER_MIN = 60 * 1_000_000_000
LATENCY_SAMPLE = 20_000
MAX_REPORT_SPAN_NS = 93 * 24 * 60 * NS_PER_MIN


class GroupBy(StrEnum):
    NAME = "name"
    AGENT = "agent"
    TEAM = "team"


class OperationRow(BaseModel):
    model_config = ConfigDict(frozen=True)

    key: str
    calls: int
    errors: int
    error_rate: float
    p50_ms: float | None
    p95_ms: float | None
    p99_ms: float | None
    input_tokens: int
    output_tokens: int
    cost_usd: float


class OperationReport(BaseModel):
    model_config = ConfigDict(frozen=True)

    kind: SpanKind
    group_by: GroupBy
    window_minutes: int
    latency_sample_limit: int = Field(
        description="Percentiles use at most this many most-recent successful calls"
    )
    rows: tuple[OperationRow, ...]


class WorkflowView(BaseModel):
    model_config = ConfigDict(frozen=True)

    trace_id: str
    finalized: bool = Field(description="False while spans may still arrive (provisional)")
    diagnosis: Diagnosis


class AnalyticsService:
    def __init__(
        self,
        *,
        spans: SqlSpanStore,
        workflows: SqlWorkflowStore,
        metrics: PlatformMetrics,
        clock: Clock,
        quiet_s: int,
        max_wait_s: int,
        loop_threshold: int,
    ) -> None:
        self._spans = spans
        self._workflows = workflows
        self._metrics = metrics
        self._clock = clock
        self._quiet_ns = quiet_s * 1_000_000_000
        self._max_wait_ns = max_wait_s * 1_000_000_000
        self._loop_threshold = loop_threshold

    async def operations(
        self, kind: SpanKind, *, group_by: GroupBy, window_minutes: int
    ) -> OperationReport:
        now = self._clock.now_ns()
        since = now - window_minutes * NS_PER_MIN
        groups = await self._spans.summary(kind, group_by.value, since_ns=since, until_ns=now)
        latencies = await self._spans.latencies(
            kind, group_by.value, since_ns=since, until_ns=now, sample=LATENCY_SAMPLE
        )
        rows = []
        for group in groups:
            sample = latencies.get(group.key, [])
            rows.append(
                OperationRow(
                    key=group.key,
                    calls=group.calls,
                    errors=group.errors,
                    error_rate=group.errors / group.calls if group.calls else 0.0,
                    p50_ms=percentile(sample, 50),
                    p95_ms=percentile(sample, 95),
                    p99_ms=percentile(sample, 99),
                    input_tokens=group.input_tokens,
                    output_tokens=group.output_tokens,
                    cost_usd=round(group.cost_usd, 8),
                )
            )
        return OperationReport(
            kind=kind,
            group_by=group_by,
            window_minutes=window_minutes,
            latency_sample_limit=LATENCY_SAMPLE,
            rows=tuple(rows),
        )

    async def costs(
        self, group_by: CostGrouping, *, start_ns: int | None, end_ns: int | None
    ) -> CostReport:
        end = end_ns if end_ns is not None else self._clock.now_ns()
        start = start_ns if start_ns is not None else end - 24 * 60 * NS_PER_MIN
        if start >= end:
            raise PlatformError("start must be before end")
        if end - start > MAX_REPORT_SPAN_NS:
            raise PlatformError("report range is limited to 93 days")
        if group_by is CostGrouping.WORKFLOW:
            rows = await self._workflows.cost_rows(since_ns=start, until_ns=end)
        else:
            rows = await self._spans.cost_rows(group_by, since_ns=start, until_ns=end)
        return CostReport(
            group_by=group_by,
            start_ns=start,
            end_ns=end,
            total_cost_usd=round(sum(r.cost_usd for r in rows), 8),
            rows=tuple(rows),
        )

    async def recent_costs(self, group_by: CostGrouping, *, hours: int) -> CostReport:
        end = self._clock.now_ns()
        return await self.costs(group_by, start_ns=end - hours * 60 * NS_PER_MIN, end_ns=end)

    async def finalize_due(self) -> int:
        """Classify traces whose spans have stopped arriving. Returns runs finalized here."""
        now = self._clock.now_ns()
        finalized = 0
        due = await self._workflows.due(
            now_ns=now, quiet_ns=self._quiet_ns, max_wait_ns=self._max_wait_ns
        )
        for trace_id, root_seen in due:
            spans = await self._spans.trace(trace_id)
            if not spans:
                continue
            diagnosis = diagnose(spans, complete=root_seen, loop_threshold=self._loop_threshold)
            if await self._workflows.finalize(diagnosis):
                self._metrics.observe_workflow(diagnosis)
                finalized += 1
        return finalized

    async def workflow(self, trace_id: str) -> WorkflowView:
        finalized, stored = await self._workflows.get(trace_id)
        if finalized and stored is not None:
            return WorkflowView(trace_id=trace_id, finalized=True, diagnosis=stored)
        spans = await self._spans.trace(trace_id)
        if not spans:
            raise NotFoundError(f"trace {trace_id} has no stored spans")
        provisional = diagnose(
            spans, complete=any(s.is_root for s in spans), loop_threshold=self._loop_threshold
        )
        return WorkflowView(trace_id=trace_id, finalized=False, diagnosis=provisional)

    async def workflows(
        self, *, outcome: Outcome | None, workflow: str | None, hours: int, limit: int
    ) -> list[Diagnosis]:
        since = self._clock.now_ns() - hours * 60 * NS_PER_MIN
        return await self._workflows.recent(
            outcome=outcome, workflow=workflow, since_ns=since, limit=limit
        )
