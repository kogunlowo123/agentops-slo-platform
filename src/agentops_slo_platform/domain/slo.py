"""Service level objectives and multi-window, multi-burn-rate alerting.

Burn rate is the ratio of the observed error rate to the error rate the SLO
allows: ``burn = (bad / total) / (1 - objective)``. A burn rate of 1 spends
the whole error budget exactly over the SLO window.

Alert rules follow the multi-window, multi-burn-rate method from the Google
SRE Workbook ("Alerting on SLOs"): each rule pairs a long window, which
establishes that enough budget has been spent to matter, with a short window
(1/12 of the long one), which establishes that it is still being spent, so
alerts reset quickly after recovery. Factors are derived from the share of
budget each rule tolerates, so they stay correct for any SLO window length
(the workbook's 14.4/6/3/1 are the 30-day values).
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from datetime import timedelta
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from agentops_slo_platform.domain.spans import SpanKind

SLO_ID = re.compile(r"^[a-z0-9]([a-z0-9\-]{0,62}[a-z0-9])?$")


class SliType(StrEnum):
    AVAILABILITY = "availability"  # good = span not in error
    LATENCY = "latency"  # good = span duration <= threshold
    WORKFLOW_SUCCESS = "workflow_success"  # good = workflow outcome success/recovered


class Severity(StrEnum):
    PAGE = "page"
    TICKET = "ticket"


class SloHealth(StrEnum):
    OK = "ok"
    WARNING = "warning"  # a ticket rule is firing
    CRITICAL = "critical"  # a page rule is firing
    NO_DATA = "no_data"


class Selector(BaseModel):
    """Which events count toward an SLI. Unset fields match everything."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: SpanKind | None = Field(default=None, description="Span kind; ignored for workflows")
    name: str | None = Field(
        default=None,
        max_length=120,
        description="Tool, model, memory operation, agent or workflow name",
    )
    agent: str | None = Field(default=None, max_length=120)
    team: str | None = Field(default=None, max_length=120)


class SloDefinition(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(description="Slug, e.g. claims-triage-tool-availability")
    name: str = Field(min_length=3, max_length=200)
    description: str = Field(default="", max_length=2000)
    sli: SliType
    selector: Selector = Field(default_factory=Selector)
    objective: float = Field(gt=0.5, lt=1.0, description="Target good-event ratio, e.g. 0.995")
    window_days: int = Field(default=30, ge=1, le=90)
    latency_threshold_ms: float | None = Field(default=None, gt=0, le=3_600_000)
    min_events: int = Field(
        default=10,
        ge=1,
        le=100_000,
        description="Windows with fewer events are treated as no data and never alert",
    )
    owner: str = Field(min_length=2, max_length=120, description="Team accountable for the SLO")

    @model_validator(mode="after")
    def _consistent(self) -> SloDefinition:
        if not SLO_ID.fullmatch(self.id):
            raise ValueError("id must be a lowercase slug of 1-64 characters")
        if self.sli is SliType.LATENCY and self.latency_threshold_ms is None:
            raise ValueError("latency SLOs need latency_threshold_ms")
        if self.sli is not SliType.LATENCY and self.latency_threshold_ms is not None:
            raise ValueError("latency_threshold_ms applies to latency SLOs only")
        if self.sli is SliType.WORKFLOW_SUCCESS and self.selector.kind not in (
            None,
            SpanKind.WORKFLOW,
        ):
            raise ValueError("workflow_success SLOs select workflows, not span kinds")
        return self

    @property
    def error_budget(self) -> float:
        return 1.0 - self.objective

    @property
    def window(self) -> timedelta:
        return timedelta(days=self.window_days)


class BurnRateRule(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    severity: Severity
    long_window: timedelta
    short_window: timedelta
    budget_fraction: float = Field(gt=0, le=1, description="Share of budget spent in long window")

    def factor(self, slo_window: timedelta) -> float:
        """Burn rate that spends ``budget_fraction`` of the budget within ``long_window``."""
        return self.budget_fraction * (slo_window / self.long_window)


DEFAULT_RULES: tuple[BurnRateRule, ...] = (
    BurnRateRule(
        id="page-1h-5m",
        severity=Severity.PAGE,
        long_window=timedelta(hours=1),
        short_window=timedelta(minutes=5),
        budget_fraction=0.02,
    ),
    BurnRateRule(
        id="page-6h-30m",
        severity=Severity.PAGE,
        long_window=timedelta(hours=6),
        short_window=timedelta(minutes=30),
        budget_fraction=0.05,
    ),
    BurnRateRule(
        id="ticket-1d-2h",
        severity=Severity.TICKET,
        long_window=timedelta(days=1),
        short_window=timedelta(hours=2),
        budget_fraction=0.10,
    ),
    BurnRateRule(
        id="ticket-3d-6h",
        severity=Severity.TICKET,
        long_window=timedelta(days=3),
        short_window=timedelta(hours=6),
        budget_fraction=0.10,
    ),
)


def evaluation_windows(rules: Sequence[BurnRateRule] = DEFAULT_RULES) -> tuple[timedelta, ...]:
    """Distinct windows the rules need, shortest first."""
    windows = {w for r in rules for w in (r.long_window, r.short_window)}
    return tuple(sorted(windows))


def window_label(window: timedelta) -> str:
    seconds = int(window.total_seconds())
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds % size == 0:
            return f"{seconds // size}{unit}"
    return f"{seconds}s"


class WindowCount(BaseModel):
    model_config = ConfigDict(frozen=True)

    total: int = Field(ge=0)
    bad: int = Field(ge=0)


class WindowBurn(BaseModel):
    model_config = ConfigDict(frozen=True)

    window: str
    total: int
    bad: int
    error_ratio: float | None
    burn_rate: float | None = Field(description="None when below min_events")


class RuleEvaluation(BaseModel):
    model_config = ConfigDict(frozen=True)

    rule_id: str
    severity: Severity
    factor: float
    long_window: str
    short_window: str
    long_burn: float | None
    short_burn: float | None
    firing: bool


class SloStatus(BaseModel):
    model_config = ConfigDict(frozen=True)

    slo_id: str
    sli: SliType
    objective: float
    window_days: int
    health: SloHealth
    total_events: int
    bad_events: int
    sli_value: float | None = Field(description="Good-event ratio over the SLO window")
    error_budget_remaining: float = Field(
        description="1.0 = untouched, 0 = exhausted, negative = overspent"
    )
    time_to_exhaustion_hours: float | None = Field(
        description="At the 1-hour burn rate; None when not burning"
    )
    windows: tuple[WindowBurn, ...]
    rules: tuple[RuleEvaluation, ...]
    evaluated_at_ns: int


def _burn(count: WindowCount, slo: SloDefinition) -> tuple[float | None, float | None]:
    if count.total == 0:
        return None, None
    ratio = count.bad / count.total
    if count.total < slo.min_events:
        return ratio, None
    return ratio, ratio / slo.error_budget


def evaluate(
    slo: SloDefinition,
    counts: Mapping[timedelta, WindowCount],
    budget_window: WindowCount,
    *,
    now_ns: int,
    rules: Sequence[BurnRateRule] = DEFAULT_RULES,
) -> SloStatus:
    """Error budget, per-window burn rates and rule state for one SLO."""
    burns: dict[timedelta, float | None] = {}
    windows: list[WindowBurn] = []
    for window in evaluation_windows(rules):
        count = counts.get(window, WindowCount(total=0, bad=0))
        ratio, burn = _burn(count, slo)
        burns[window] = burn
        windows.append(
            WindowBurn(
                window=window_label(window),
                total=count.total,
                bad=count.bad,
                error_ratio=ratio,
                burn_rate=burn,
            )
        )

    evaluations: list[RuleEvaluation] = []
    for rule in rules:
        factor = rule.factor(slo.window)
        long_burn, short_burn = burns[rule.long_window], burns[rule.short_window]
        firing = (
            long_burn is not None
            and short_burn is not None
            and long_burn >= factor
            and short_burn >= factor
        )
        evaluations.append(
            RuleEvaluation(
                rule_id=rule.id,
                severity=rule.severity,
                factor=round(factor, 6),
                long_window=window_label(rule.long_window),
                short_window=window_label(rule.short_window),
                long_burn=long_burn,
                short_burn=short_burn,
                firing=firing,
            )
        )

    total, bad = budget_window.total, budget_window.bad
    allowed = slo.error_budget * total
    remaining = 1.0 if total == 0 else 1.0 - (bad / allowed)
    hourly = burns.get(timedelta(hours=1))
    exhaustion: float | None = None
    if hourly is not None and hourly > 0 and remaining > 0:
        exhaustion = remaining * slo.window.total_seconds() / 3600 / hourly

    if total < slo.min_events:
        health = SloHealth.NO_DATA
    elif any(e.firing and e.severity is Severity.PAGE for e in evaluations):
        health = SloHealth.CRITICAL
    elif any(e.firing for e in evaluations):
        health = SloHealth.WARNING
    else:
        health = SloHealth.OK

    return SloStatus(
        slo_id=slo.id,
        sli=slo.sli,
        objective=slo.objective,
        window_days=slo.window_days,
        health=health,
        total_events=total,
        bad_events=bad,
        sli_value=None if total == 0 else (total - bad) / total,
        error_budget_remaining=round(remaining, 6),
        time_to_exhaustion_hours=None if exhaustion is None else round(exhaustion, 3),
        windows=tuple(windows),
        rules=tuple(evaluations),
        evaluated_at_ns=now_ns,
    )
