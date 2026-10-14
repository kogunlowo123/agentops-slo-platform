"""SQLAlchemy Core repositories. PostgreSQL in production, SQLite in tests.

Writes that several replicas may race on are made idempotent with the
database: span facts are keyed by (trace_id, span_id) so exporter retries
never double count; workflow finalization and alert transitions are
conditional updates, and only the replica whose update changed a row emits
metrics and notifications. No leader election is needed.
"""

from __future__ import annotations

import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from sqlalchemy import (
    ColumnElement,
    Integer,
    and_,
    case,
    cast,
    delete,
    func,
    insert,
    or_,
    select,
    tuple_,
    update,
)
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from agentops_slo_platform.domain.costs import CostGrouping, CostRow, PriceEntry
from agentops_slo_platform.domain.errors import NotFoundError
from agentops_slo_platform.domain.recovery import ActionStatus, RecoveryAction
from agentops_slo_platform.domain.slo import Selector, SliType, SloDefinition, WindowCount
from agentops_slo_platform.domain.spans import SpanFact, SpanKind
from agentops_slo_platform.domain.workflows import GOOD_OUTCOMES, Diagnosis, Outcome
from agentops_slo_platform.infrastructure.tables import (
    SCHEMA_VERSION,
    metadata,
    prices,
    recovery_actions,
    schema_version,
    slo_alert_events,
    slo_alert_state,
    slos,
    span_facts,
    workflow_runs,
)

SUPPORTED_DIALECTS = ("postgresql", "sqlite")
NS_PER_S = 1_000_000_000


def make_engine(url: str) -> AsyncEngine:
    backend = make_url(url).get_backend_name()
    if backend not in SUPPORTED_DIALECTS:
        raise ValueError(f"unsupported database dialect {backend}")
    return create_async_engine(url, pool_pre_ping=True)


async def create_schema(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        await conn.run_sync(metadata.create_all)
        current = (await conn.execute(select(schema_version.c.version))).scalar()
        if current is None:
            await conn.execute(insert(schema_version).values(version=SCHEMA_VERSION))


def _upsert(engine: AsyncEngine, table: Any) -> Any:
    """Dialect insert supporting ON CONFLICT (PostgreSQL and SQLite share the syntax)."""
    if engine.dialect.name == "postgresql":
        return postgresql.insert(table)
    return sqlite.insert(table)


def _ns(window: timedelta) -> int:
    return int(window.total_seconds() * NS_PER_S)


# Bind parameters per IN-list chunk: two per key, far below the SQLite and
# asyncpg limit of 32,767 parameters per statement.
KEY_CHUNK = 1000


def _chunks[T](items: Sequence[T], size: int = KEY_CHUNK) -> Iterable[Sequence[T]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


class SystemClock:
    def now_ns(self) -> int:
        return time.time_ns()


# ------------------------------------------------------------------ span facts


def _fact_row(fact: SpanFact, ingested_ns: int) -> dict[str, Any]:
    row = fact.model_dump(exclude={"finish_reasons"})
    row["kind"] = fact.kind.value
    row["finish_reasons"] = ",".join(fact.finish_reasons)[:256]
    row["duration_ms"] = fact.duration_ms
    row["ingested_ns"] = ingested_ns
    return row


def _fact(row: Any) -> SpanFact:
    data = dict(row._mapping)
    data.pop("duration_ms", None)
    data.pop("ingested_ns", None)
    reasons = data.pop("finish_reasons") or ""
    return SpanFact.model_validate(
        {**data, "finish_reasons": tuple(r for r in reasons.split(",") if r)}
    )


def _selector_filters(table: Any, selector: Selector, *, spans: bool) -> list[ColumnElement[bool]]:
    filters: list[ColumnElement[bool]] = []
    if spans and selector.kind is not None:
        filters.append(table.c.kind == selector.kind.value)
    if selector.name is not None:
        filters.append((table.c.name if spans else table.c.workflow) == selector.name)
    if selector.agent is not None:
        filters.append(table.c.agent == selector.agent)
    if selector.team is not None:
        filters.append(table.c.team == selector.team)
    return filters


@dataclass(frozen=True)
class GroupSummary:
    key: str
    calls: int
    errors: int
    input_tokens: int
    output_tokens: int
    cost_usd: float


class SqlSpanStore:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def add_new(self, facts: Sequence[SpanFact], *, ingested_ns: int) -> list[SpanFact]:
        """Insert facts not stored yet; returns the newly stored ones."""
        if not facts:
            return []
        keys = [(f.trace_id, f.span_id) for f in facts]
        async with self._engine.begin() as conn:
            existing: set[tuple[str, str]] = set()
            for chunk in _chunks(keys):
                existing.update(
                    (r[0], r[1])
                    for r in await conn.execute(
                        select(span_facts.c.trace_id, span_facts.c.span_id).where(
                            tuple_(span_facts.c.trace_id, span_facts.c.span_id).in_(chunk)
                        )
                    )
                )
            unique: dict[tuple[str, str], SpanFact] = {}
            for fact in facts:
                key = (fact.trace_id, fact.span_id)
                if key not in existing:
                    unique.setdefault(key, fact)
            fresh = list(unique.values())
            if fresh:
                statement = _upsert(self._engine, span_facts).on_conflict_do_nothing(
                    index_elements=["trace_id", "span_id"]
                )
                await conn.execute(statement, [_fact_row(f, ingested_ns) for f in fresh])
        return fresh

    async def attribution(self, keys: Iterable[tuple[str, str]]) -> dict[str, tuple[str, str]]:
        """span_id -> (agent, team) for already stored spans (parents in earlier batches)."""
        found: dict[str, tuple[str, str]] = {}
        async with self._engine.connect() as conn:
            for chunk in _chunks(list(keys)):
                rows = await conn.execute(
                    select(span_facts.c.span_id, span_facts.c.agent, span_facts.c.team).where(
                        tuple_(span_facts.c.trace_id, span_facts.c.span_id).in_(chunk)
                    )
                )
                found.update({r[0]: (r[1], r[2]) for r in rows})
        return found

    async def trace(self, trace_id: str) -> list[SpanFact]:
        async with self._engine.connect() as conn:
            rows = await conn.execute(
                select(span_facts)
                .where(span_facts.c.trace_id == trace_id)
                .order_by(span_facts.c.start_ns, span_facts.c.span_id)
            )
            return [_fact(r) for r in rows]

    async def window_counts(
        self,
        slo: SloDefinition,
        windows: Sequence[timedelta],
        *,
        now_ns: int,
    ) -> tuple[dict[timedelta, WindowCount], WindowCount]:
        """Totals and bad events for every alert window plus the SLO window, in one query."""
        bad: ColumnElement[bool] = span_facts.c.is_error.is_(True)
        if slo.sli is SliType.LATENCY:
            bad = or_(bad, span_facts.c.duration_ms > slo.latency_threshold_ms)
        cutoffs = [now_ns - _ns(w) for w in windows] + [now_ns - _ns(slo.window)]
        columns = []
        for cutoff in cutoffs:
            recent = span_facts.c.end_ns >= cutoff
            columns.append(func.sum(case((recent, 1), else_=0)))
            columns.append(func.sum(case((and_(recent, bad), 1), else_=0)))
        query = select(*columns).where(
            span_facts.c.end_ns >= min(cutoffs),
            span_facts.c.end_ns <= now_ns,
            *_selector_filters(span_facts, slo.selector, spans=True),
        )
        async with self._engine.connect() as conn:
            row = (await conn.execute(query)).one()
        values = [int(v or 0) for v in row]
        counts = [WindowCount(total=values[i], bad=values[i + 1]) for i in range(0, len(values), 2)]
        return dict(zip(windows, counts[:-1], strict=True)), counts[-1]

    async def calls(
        self, kind: SpanKind, name: str, *, since_ns: int, until_ns: int, limit: int = 50_000
    ) -> list[SpanFact]:
        async with self._engine.connect() as conn:
            rows = await conn.execute(
                select(span_facts)
                .where(
                    span_facts.c.kind == kind.value,
                    span_facts.c.name == name,
                    span_facts.c.end_ns >= since_ns,
                    span_facts.c.end_ns <= until_ns,
                )
                .order_by(span_facts.c.end_ns.desc())
                .limit(limit)
            )
            return [_fact(r) for r in rows]

    async def summary(
        self, kind: SpanKind, group_by: str, *, since_ns: int, until_ns: int
    ) -> list[GroupSummary]:
        key = span_facts.c[group_by]
        query = (
            select(
                key,
                func.count(),
                func.sum(cast(span_facts.c.is_error, Integer)),
                func.sum(span_facts.c.input_tokens),
                func.sum(span_facts.c.output_tokens),
                func.sum(span_facts.c.cost_usd),
            )
            .where(
                span_facts.c.kind == kind.value,
                span_facts.c.end_ns >= since_ns,
                span_facts.c.end_ns <= until_ns,
            )
            .group_by(key)
            .order_by(func.count().desc(), key)
            .limit(500)
        )
        async with self._engine.connect() as conn:
            rows = (await conn.execute(query)).all()
        return [
            GroupSummary(
                key=r[0],
                calls=int(r[1]),
                errors=int(r[2] or 0),
                input_tokens=int(r[3] or 0),
                output_tokens=int(r[4] or 0),
                cost_usd=float(r[5] or 0.0),
            )
            for r in rows
        ]

    async def latencies(
        self, kind: SpanKind, group_by: str, *, since_ns: int, until_ns: int, sample: int
    ) -> dict[str, list[float]]:
        """Most recent successful durations per group, bounded by ``sample`` rows overall."""
        key = span_facts.c[group_by]
        query = (
            select(key, span_facts.c.duration_ms)
            .where(
                span_facts.c.kind == kind.value,
                span_facts.c.is_error.is_(False),
                span_facts.c.end_ns >= since_ns,
                span_facts.c.end_ns <= until_ns,
            )
            .order_by(span_facts.c.end_ns.desc())
            .limit(sample)
        )
        grouped: dict[str, list[float]] = {}
        async with self._engine.connect() as conn:
            for name, duration in await conn.execute(query):
                grouped.setdefault(name, []).append(float(duration))
        return grouped

    async def cost_rows(
        self, group_by: CostGrouping, *, since_ns: int, until_ns: int
    ) -> list[CostRow]:
        key = span_facts.c[group_by.value]
        has_tokens = (span_facts.c.input_tokens + span_facts.c.output_tokens) > 0
        unpriced = and_(span_facts.c.cost_usd.is_(None), has_tokens)
        query = (
            select(
                key,
                func.count(),
                func.sum(span_facts.c.input_tokens),
                func.sum(span_facts.c.output_tokens),
                func.sum(span_facts.c.cached_input_tokens),
                func.sum(span_facts.c.cost_usd),
                func.sum(case((unpriced, 1), else_=0)),
            )
            .where(
                span_facts.c.kind == SpanKind.MODEL.value,
                span_facts.c.end_ns >= since_ns,
                span_facts.c.end_ns <= until_ns,
            )
            .group_by(key)
            .order_by(func.sum(span_facts.c.cost_usd).desc(), key)
            .limit(1000)
        )
        async with self._engine.connect() as conn:
            rows = (await conn.execute(query)).all()
        return [
            CostRow(
                key=str(r[0]) if r[0] is not None else "unknown-model",
                model_calls=int(r[1]),
                input_tokens=int(r[2] or 0),
                output_tokens=int(r[3] or 0),
                cached_input_tokens=int(r[4] or 0),
                cost_usd=round(float(r[5] or 0.0), 8),
                unpriced_calls=int(r[6] or 0),
            )
            for r in rows
        ]

    async def purge_before(self, cutoff_ns: int) -> int:
        async with self._engine.begin() as conn:
            result = await conn.execute(delete(span_facts).where(span_facts.c.end_ns < cutoff_ns))
        return int(result.rowcount or 0)


# --------------------------------------------------------------- workflow runs


@dataclass(frozen=True)
class TraceTouch:
    trace_id: str
    root_seen: bool
    workflow: str | None
    agent: str | None
    team: str | None


class SqlWorkflowStore:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def touch(self, touches: Sequence[TraceTouch], *, now_ns: int) -> None:
        """Record span arrival per trace; finalized runs are not reopened."""
        if not touches:
            return
        statement = _upsert(self._engine, workflow_runs)
        excluded = statement.excluded
        statement = statement.on_conflict_do_update(
            index_elements=["trace_id"],
            set_={
                "last_seen_ns": excluded.last_seen_ns,
                "root_seen": or_(workflow_runs.c.root_seen, excluded.root_seen),
                "workflow": func.coalesce(excluded.workflow, workflow_runs.c.workflow),
                "agent": func.coalesce(workflow_runs.c.agent, excluded.agent),
                "team": func.coalesce(workflow_runs.c.team, excluded.team),
            },
        )
        rows = [
            {
                "trace_id": t.trace_id,
                "root_seen": t.root_seen,
                "workflow": t.workflow,
                "agent": t.agent,
                "team": t.team,
                "first_seen_ns": now_ns,
                "last_seen_ns": now_ns,
                "finalized": False,
            }
            for t in touches
        ]
        async with self._engine.begin() as conn:
            for row in rows:  # per-row so ON CONFLICT sees each trace once per statement
                await conn.execute(statement, row)

    async def due(
        self, *, now_ns: int, quiet_ns: int, max_wait_ns: int, limit: int = 500
    ) -> list[tuple[str, bool]]:
        """Traces ready to classify: root seen and quiet, or abandoned past the wait limit."""
        query = (
            select(workflow_runs.c.trace_id, workflow_runs.c.root_seen)
            .where(
                workflow_runs.c.finalized.is_(False),
                or_(
                    and_(
                        workflow_runs.c.root_seen.is_(True),
                        workflow_runs.c.last_seen_ns <= now_ns - quiet_ns,
                    ),
                    workflow_runs.c.last_seen_ns <= now_ns - max_wait_ns,
                ),
            )
            .order_by(workflow_runs.c.last_seen_ns)
            .limit(limit)
        )
        async with self._engine.connect() as conn:
            return [(r[0], bool(r[1])) for r in await conn.execute(query)]

    async def finalize(self, diagnosis: Diagnosis) -> bool:
        """Store the classification once; False if another replica already did."""
        async with self._engine.begin() as conn:
            result = await conn.execute(
                update(workflow_runs)
                .where(
                    workflow_runs.c.trace_id == diagnosis.trace_id,
                    workflow_runs.c.finalized.is_(False),
                )
                .values(
                    finalized=True,
                    workflow=diagnosis.workflow,
                    agent=diagnosis.agent,
                    team=diagnosis.team,
                    outcome=diagnosis.outcome.value,
                    failure_class=(
                        diagnosis.failure_class.value if diagnosis.failure_class else None
                    ),
                    started_ns=diagnosis.started_ns,
                    ended_ns=diagnosis.ended_ns,
                    cost_usd=diagnosis.cost_usd,
                    diagnosis=diagnosis.model_dump_json(),
                )
            )
        return bool(result.rowcount)

    async def get(self, trace_id: str) -> tuple[bool, Diagnosis | None]:
        async with self._engine.connect() as conn:
            row = (
                await conn.execute(
                    select(workflow_runs.c.finalized, workflow_runs.c.diagnosis).where(
                        workflow_runs.c.trace_id == trace_id
                    )
                )
            ).first()
        if row is None:
            raise NotFoundError(f"trace {trace_id} not known")
        return bool(row[0]), Diagnosis.model_validate_json(row[1]) if row[1] else None

    async def recent(
        self,
        *,
        outcome: Outcome | None,
        workflow: str | None,
        since_ns: int,
        limit: int,
    ) -> list[Diagnosis]:
        query = (
            select(workflow_runs.c.diagnosis)
            .where(workflow_runs.c.finalized.is_(True), workflow_runs.c.ended_ns >= since_ns)
            .order_by(workflow_runs.c.ended_ns.desc())
            .limit(limit)
        )
        if outcome is not None:
            query = query.where(workflow_runs.c.outcome == outcome.value)
        if workflow is not None:
            query = query.where(workflow_runs.c.workflow == workflow)
        async with self._engine.connect() as conn:
            return [Diagnosis.model_validate_json(r[0]) for r in await conn.execute(query)]

    async def window_counts(
        self, slo: SloDefinition, windows: Sequence[timedelta], *, now_ns: int
    ) -> tuple[dict[timedelta, WindowCount], WindowCount]:
        good = workflow_runs.c.outcome.in_([o.value for o in GOOD_OUTCOMES])
        cutoffs = [now_ns - _ns(w) for w in windows] + [now_ns - _ns(slo.window)]
        columns = []
        for cutoff in cutoffs:
            recent = workflow_runs.c.ended_ns >= cutoff
            columns.append(func.sum(case((recent, 1), else_=0)))
            columns.append(func.sum(case((and_(recent, ~good), 1), else_=0)))
        query = select(*columns).where(
            workflow_runs.c.finalized.is_(True),
            workflow_runs.c.ended_ns >= min(cutoffs),
            workflow_runs.c.ended_ns <= now_ns,
            *_selector_filters(workflow_runs, slo.selector, spans=False),
        )
        async with self._engine.connect() as conn:
            row = (await conn.execute(query)).one()
        values = [int(v or 0) for v in row]
        counts = [WindowCount(total=values[i], bad=values[i + 1]) for i in range(0, len(values), 2)]
        return dict(zip(windows, counts[:-1], strict=True)), counts[-1]

    async def cost_rows(self, *, since_ns: int, until_ns: int) -> list[CostRow]:
        query = (
            select(
                workflow_runs.c.workflow,
                func.count(),
                func.sum(workflow_runs.c.cost_usd),
            )
            .where(
                workflow_runs.c.finalized.is_(True),
                workflow_runs.c.ended_ns >= since_ns,
                workflow_runs.c.ended_ns <= until_ns,
            )
            .group_by(workflow_runs.c.workflow)
            .order_by(func.sum(workflow_runs.c.cost_usd).desc(), workflow_runs.c.workflow)
            .limit(1000)
        )
        async with self._engine.connect() as conn:
            rows = (await conn.execute(query)).all()
        runs = await self._run_tokens(since_ns=since_ns, until_ns=until_ns)
        result = []
        for name, count, cost in rows:
            tokens = runs.get(name, (0, 0, 0))
            result.append(
                CostRow(
                    key=str(name),
                    model_calls=tokens[2],
                    input_tokens=tokens[0],
                    output_tokens=tokens[1],
                    cached_input_tokens=0,
                    cost_usd=round(float(cost or 0.0), 8),
                    unpriced_calls=int(count) if cost is None else 0,
                )
            )
        return result

    async def _run_tokens(self, *, since_ns: int, until_ns: int) -> dict[str, tuple[int, int, int]]:
        query = select(workflow_runs.c.workflow, workflow_runs.c.diagnosis).where(
            workflow_runs.c.finalized.is_(True),
            workflow_runs.c.ended_ns >= since_ns,
            workflow_runs.c.ended_ns <= until_ns,
        )
        totals: dict[str, tuple[int, int, int]] = {}
        async with self._engine.connect() as conn:
            for name, document in await conn.execute(query):
                diagnosis = Diagnosis.model_validate_json(document)
                current = totals.get(name, (0, 0, 0))
                totals[name] = (
                    current[0] + diagnosis.input_tokens,
                    current[1] + diagnosis.output_tokens,
                    current[2] + diagnosis.model_calls,
                )
        return totals

    async def purge_before(self, cutoff_ns: int) -> int:
        async with self._engine.begin() as conn:
            result = await conn.execute(
                delete(workflow_runs).where(workflow_runs.c.last_seen_ns < cutoff_ns)
            )
        return int(result.rowcount or 0)


# ---------------------------------------------------------------------- prices


class SqlPriceStore:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def put(self, entry: PriceEntry, *, actor: str, now_ns: int) -> None:
        statement = _upsert(self._engine, prices)
        statement = statement.on_conflict_do_update(
            index_elements=["model", "effective_from_ns"],
            set_={
                "document": statement.excluded.document,
                "set_by": statement.excluded.set_by,
                "set_ns": statement.excluded.set_ns,
            },
        )
        async with self._engine.begin() as conn:
            await conn.execute(
                statement,
                {
                    "model": entry.model,
                    "effective_from_ns": entry.effective_from_ns,
                    "document": entry.model_dump_json(),
                    "set_by": actor,
                    "set_ns": now_ns,
                },
            )

    async def all(self) -> list[PriceEntry]:
        async with self._engine.connect() as conn:
            rows = await conn.execute(
                select(prices.c.document).order_by(prices.c.model, prices.c.effective_from_ns)
            )
            return [PriceEntry.model_validate_json(r[0]) for r in rows]


# ------------------------------------------------------------------------ SLOs


class SqlSloStore:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def put(self, slo: SloDefinition, *, actor: str, now_ns: int) -> bool:
        """Create or replace; returns True when created."""
        async with self._engine.begin() as conn:
            exists = (await conn.execute(select(slos.c.id).where(slos.c.id == slo.id))).first()
            if exists:
                await conn.execute(
                    update(slos)
                    .where(slos.c.id == slo.id)
                    .values(document=slo.model_dump_json(), updated_ns=now_ns, updated_by=actor)
                )
                return False
            await conn.execute(
                insert(slos).values(
                    id=slo.id,
                    document=slo.model_dump_json(),
                    created_ns=now_ns,
                    updated_ns=now_ns,
                    updated_by=actor,
                )
            )
            return True

    async def get(self, slo_id: str) -> SloDefinition:
        async with self._engine.connect() as conn:
            row = (await conn.execute(select(slos.c.document).where(slos.c.id == slo_id))).first()
        if row is None:
            raise NotFoundError(f"SLO {slo_id} not found")
        return SloDefinition.model_validate_json(row[0])

    async def all(self) -> list[SloDefinition]:
        async with self._engine.connect() as conn:
            rows = await conn.execute(select(slos.c.document).order_by(slos.c.id))
            return [SloDefinition.model_validate_json(r[0]) for r in rows]

    async def delete(self, slo_id: str) -> None:
        async with self._engine.begin() as conn:
            result = await conn.execute(delete(slos).where(slos.c.id == slo_id))
            if not result.rowcount:
                raise NotFoundError(f"SLO {slo_id} not found")
            await conn.execute(delete(slo_alert_state).where(slo_alert_state.c.slo_id == slo_id))


@dataclass(frozen=True)
class AlertEvent:
    slo_id: str
    rule_id: str
    severity: str
    transition: str
    at_ns: int
    long_burn: float | None
    short_burn: float | None


class SqlAlertStore:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def transition(self, event: AlertEvent) -> bool:
        """Move a rule to firing/resolved. True only for the caller that changed it."""
        firing = event.transition == "firing"
        async with self._engine.begin() as conn:
            await conn.execute(
                _upsert(self._engine, slo_alert_state)
                .values(slo_id=event.slo_id, rule_id=event.rule_id, firing=False, since_ns=0)
                .on_conflict_do_nothing(index_elements=["slo_id", "rule_id"])
            )
            result = await conn.execute(
                update(slo_alert_state)
                .where(
                    slo_alert_state.c.slo_id == event.slo_id,
                    slo_alert_state.c.rule_id == event.rule_id,
                    slo_alert_state.c.firing.is_(not firing),
                )
                .values(firing=firing, since_ns=event.at_ns)
            )
            if not result.rowcount:
                return False
            await conn.execute(
                insert(slo_alert_events).values(
                    slo_id=event.slo_id,
                    rule_id=event.rule_id,
                    severity=event.severity,
                    transition=event.transition,
                    at_ns=event.at_ns,
                    long_burn=event.long_burn,
                    short_burn=event.short_burn,
                )
            )
        return True

    async def firing(self) -> dict[tuple[str, str], int]:
        """(slo_id, rule_id) -> firing since."""
        async with self._engine.connect() as conn:
            rows = await conn.execute(
                select(
                    slo_alert_state.c.slo_id, slo_alert_state.c.rule_id, slo_alert_state.c.since_ns
                ).where(slo_alert_state.c.firing.is_(True))
            )
            return {(r[0], r[1]): int(r[2]) for r in rows}

    async def events(self, *, slo_id: str | None, limit: int) -> list[AlertEvent]:
        query = select(slo_alert_events).order_by(slo_alert_events.c.id.desc()).limit(limit)
        if slo_id is not None:
            query = query.where(slo_alert_events.c.slo_id == slo_id)
        async with self._engine.connect() as conn:
            rows = await conn.execute(query)
            return [
                AlertEvent(
                    slo_id=r.slo_id,
                    rule_id=r.rule_id,
                    severity=r.severity,
                    transition=r.transition,
                    at_ns=int(r.at_ns),
                    long_burn=r.long_burn,
                    short_burn=r.short_burn,
                )
                for r in rows
            ]


# ------------------------------------------------------------ recovery actions


class SqlRecoveryStore:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def add(self, action: RecoveryAction) -> None:
        async with self._engine.begin() as conn:
            await conn.execute(insert(recovery_actions).values(**self._row(action)))

    async def get(self, action_id: str) -> RecoveryAction:
        async with self._engine.connect() as conn:
            row = (
                await conn.execute(
                    select(recovery_actions.c.document).where(recovery_actions.c.id == action_id)
                )
            ).first()
        if row is None:
            raise NotFoundError(f"recovery action {action_id} not found")
        return RecoveryAction.model_validate_json(row[0])

    async def replace(self, action: RecoveryAction, *, expected: ActionStatus) -> bool:
        """Compare-and-set on status so two approvers cannot both decide."""
        async with self._engine.begin() as conn:
            result = await conn.execute(
                update(recovery_actions)
                .where(
                    recovery_actions.c.id == action.id,
                    recovery_actions.c.status == expected.value,
                )
                .values(**self._row(action))
            )
        return bool(result.rowcount)

    async def find(
        self,
        *,
        statuses: Sequence[ActionStatus] | None,
        target: tuple[SpanKind, str] | None,
        limit: int,
    ) -> list[RecoveryAction]:
        query = (
            select(recovery_actions.c.document)
            .order_by(recovery_actions.c.proposed_ns.desc())
            .limit(limit)
        )
        if statuses:
            query = query.where(recovery_actions.c.status.in_([s.value for s in statuses]))
        if target is not None:
            query = query.where(
                recovery_actions.c.target_kind == target[0].value,
                recovery_actions.c.target_name == target[1],
            )
        async with self._engine.connect() as conn:
            return [RecoveryAction.model_validate_json(r[0]) for r in await conn.execute(query)]

    async def active(self, *, now_ns: int) -> list[RecoveryAction]:
        async with self._engine.connect() as conn:
            rows = await conn.execute(
                select(recovery_actions.c.document)
                .where(
                    recovery_actions.c.status == ActionStatus.ACTIVE.value,
                    recovery_actions.c.expires_ns > now_ns,
                )
                .order_by(recovery_actions.c.proposed_ns)
            )
            return [RecoveryAction.model_validate_json(r[0]) for r in rows]

    @staticmethod
    def _row(action: RecoveryAction) -> dict[str, Any]:
        return {
            "id": action.id,
            "status": action.status.value,
            "target_kind": action.target.kind.value,
            "target_name": action.target.name,
            "action": action.action.value,
            "expires_ns": action.expires_ns,
            "proposed_ns": action.proposed_ns,
            "document": action.model_dump_json(),
        }
