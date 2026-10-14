"""Span processing: classify, attribute, price, scrub, store, measure.

Attribution: GenAI conventions put ``gen_ai.agent.name`` on the agent span,
not on the model and tool spans beneath it. Each span is attributed to the
nearest ancestor that names an agent (and a team), first within the batch,
then by looking up the batch's out-of-batch parents among stored spans. The
collector configuration groups spans by trace before export so most traces
resolve within one batch.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Sequence
from dataclasses import dataclass, field

from agentops_slo_platform.domain.costs import PriceBook
from agentops_slo_platform.domain.spans import (
    AGENT_ID,
    AGENT_NAME,
    LEGACY_SYSTEM,
    OPERATION,
    PROVIDER,
    REQUEST_MODEL,
    RESPONSE_MODEL,
    SERVICE_NAME,
    TEAM,
    UNATTRIBUTED,
    WORKFLOW,
    SpanFact,
    SpanKind,
    SpanRecord,
    ancestry_attribute,
    classify_kind,
    exception_details,
    finish_reasons,
    http_status,
    is_error,
    label_safe,
    retry_attempt,
    subject_name,
    token_counts,
)
from agentops_slo_platform.infrastructure.database import (
    SqlPriceStore,
    SqlSpanStore,
    SqlWorkflowStore,
    TraceTouch,
)
from agentops_slo_platform.ingestion.redaction import Scrubber
from agentops_slo_platform.observability.metrics import PlatformMetrics

NS_PER_S = 1_000_000_000


class PriceCache:
    """Price book shared by ingestion, reloaded at most every ``ttl_s`` or on change."""

    def __init__(self, store: SqlPriceStore, *, ttl_s: float = 30.0) -> None:
        self._store = store
        self._ttl = ttl_s
        self._book: PriceBook | None = None
        self._loaded_at = 0.0
        self._lock = asyncio.Lock()

    def invalidate(self) -> None:
        self._book = None

    async def book(self) -> PriceBook:
        async with self._lock:
            if self._book is None or time.monotonic() - self._loaded_at > self._ttl:
                self._book = PriceBook(await self._store.all())
                self._loaded_at = time.monotonic()
            return self._book


@dataclass
class IngestResult:
    accepted: int = 0
    duplicates: int = 0
    rejected: list[str] = field(default_factory=list)

    @property
    def rejected_count(self) -> int:
        return len(self.rejected)

    def message(self) -> str:
        reasons = sorted(set(self.rejected))
        return "; ".join(reasons)[:512]


class SpanProcessor:
    def __init__(
        self,
        *,
        spans: SqlSpanStore,
        workflows: SqlWorkflowStore,
        prices: PriceCache,
        metrics: PlatformMetrics,
        scrubber: Scrubber,
        max_future_skew_s: int = 600,
        max_age_s: int = 35 * 86400,
    ) -> None:
        self._spans = spans
        self._workflows = workflows
        self._prices = prices
        self._metrics = metrics
        self._scrubber = scrubber
        self._max_future_ns = max_future_skew_s * NS_PER_S
        self._max_age_ns = max_age_s * NS_PER_S

    async def process(
        self, records: Sequence[SpanRecord], *, caller: str, now_ns: int
    ) -> IngestResult:
        result = IngestResult()
        valid: list[SpanRecord] = []
        for record in records:
            if record.end_ns > now_ns + self._max_future_ns:
                result.rejected.append("timestamp in the future")
            elif record.end_ns < now_ns - self._max_age_ns:
                result.rejected.append("timestamp older than retention")
            else:
                valid.append(record)
        for reason in result.rejected:
            self._metrics.spans_rejected.labels(reason).inc()
        if not valid:
            return result

        facts = await self._facts(valid, caller=caller)
        fresh = await self._spans.add_new(facts, ingested_ns=now_ns)
        result.accepted = len(fresh)
        result.duplicates = len(facts) - len(fresh)
        if result.duplicates:
            self._metrics.spans_duplicate.inc(result.duplicates)
        for fact in fresh:
            self._metrics.observe_fact(fact)
        await self._workflows.touch(self._touches(fresh), now_ns=now_ns)
        return result

    async def _facts(self, records: Sequence[SpanRecord], *, caller: str) -> list[SpanFact]:
        parents = {r.span_id: r.parent_span_id for r in records}
        agents: dict[str, str] = {}
        teams: dict[str, str] = {}
        for record in records:
            agent = record.attributes.get(AGENT_NAME, record.attributes.get(AGENT_ID))
            if agent is not None:
                agents[record.span_id] = label_safe(agent)
            team = record.attr(TEAM)
            if team is not None:
                teams[record.span_id] = label_safe(team)

        # Parents outside this batch: look up attribution of already stored spans.
        external = {
            (r.trace_id, r.parent_span_id)
            for r in records
            if r.parent_span_id is not None and r.parent_span_id not in parents
        }
        for span_id, (agent, team) in (await self._spans.attribution(external)).items():
            if agent != UNATTRIBUTED:
                agents.setdefault(span_id, agent)
            if team != UNATTRIBUTED:
                teams.setdefault(span_id, team)

        roots = {r.trace_id: r for r in records if r.is_root}
        book = await self._prices.book()
        facts: list[SpanFact] = []
        for record in records:
            facts.append(
                self._fact(
                    record,
                    caller=caller,
                    agent=ancestry_attribute(record.span_id, parents, agents),
                    team=ancestry_attribute(record.span_id, parents, teams),
                    root=roots.get(record.trace_id),
                    book=book,
                )
            )
        return facts

    def _fact(
        self,
        record: SpanRecord,
        *,
        caller: str,
        agent: str | None,
        team: str | None,
        root: SpanRecord | None,
        book: PriceBook,
    ) -> SpanFact:
        kind = classify_kind(record)
        failed = is_error(record)
        exc_type, exc_message = exception_details(record)
        error_type = record.attributes.get("error.type", exc_type) if failed else None
        error_message, redacted = (
            self._scrubber.scrub(record.status_message or exc_message) if failed else (None, 0)
        )
        safe_type, type_redacted = self._scrubber.scrub(
            None if error_type is None else str(error_type)
        )
        if redacted or type_redacted:
            self._metrics.redactions.inc(redacted + type_redacted)
        input_tokens, output_tokens, cached = token_counts(record)
        model = record.attr(RESPONSE_MODEL, REQUEST_MODEL)
        cost: float | None = None
        if kind is SpanKind.MODEL:
            price = book.price(None if model is None else label_safe(model), record.start_ns)
            if price is not None:
                cost = round(price.cost(input_tokens, output_tokens, cached), 10)
        workflow = record.attr(WORKFLOW)
        if workflow is None and root is not None:
            workflow = root.attr(WORKFLOW)
        operation = record.attributes.get(OPERATION)
        provider = record.attr(PROVIDER, LEGACY_SYSTEM)
        return SpanFact(
            trace_id=record.trace_id,
            span_id=record.span_id,
            parent_span_id=record.parent_span_id,
            kind=kind,
            name=subject_name(record, kind),
            operation=label_safe(operation, default="none")[:64],
            agent=agent or UNATTRIBUTED,
            team=team or UNATTRIBUTED,
            service=label_safe(record.resource.get(SERVICE_NAME), default="unknown-service"),
            provider=None if provider is None else label_safe(provider),
            model=None if model is None else label_safe(model),
            start_ns=record.start_ns,
            end_ns=record.end_ns,
            is_error=failed,
            error_type=None if safe_type is None else safe_type[:256],
            error_message=error_message,
            http_status=http_status(record),
            finish_reasons=finish_reasons(record),
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cached_input_tokens=cached,
            cost_usd=cost,
            retry_attempt=retry_attempt(record),
            workflow=None if workflow is None else label_safe(workflow),
            ingested_by=caller[:512],
        )

    @staticmethod
    def _touches(facts: Sequence[SpanFact]) -> list[TraceTouch]:
        by_trace: dict[str, TraceTouch] = {}
        for fact in sorted(facts, key=lambda f: (not f.is_root, f.start_ns)):
            current = by_trace.get(fact.trace_id)
            workflow = fact.workflow or (fact.name if fact.is_root else None)
            agent = fact.agent if fact.agent != UNATTRIBUTED else None
            team = fact.team if fact.team != UNATTRIBUTED else None
            if current is None:
                by_trace[fact.trace_id] = TraceTouch(
                    trace_id=fact.trace_id,
                    root_seen=fact.is_root,
                    workflow=workflow,
                    agent=agent,
                    team=team,
                )
            else:
                by_trace[fact.trace_id] = TraceTouch(
                    trace_id=fact.trace_id,
                    root_seen=current.root_seen or fact.is_root,
                    workflow=current.workflow or workflow,
                    agent=current.agent or agent,
                    team=current.team or team,
                )
        return list(by_trace.values())
