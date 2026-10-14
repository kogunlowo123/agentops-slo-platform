"""SLO definitions, burn-rate math and multi-window rule evaluation."""

from __future__ import annotations

from datetime import timedelta

import pytest
from pydantic import ValidationError

from agentops_slo_platform.domain.slo import (
    DEFAULT_RULES,
    Selector,
    Severity,
    SliType,
    SloDefinition,
    SloHealth,
    WindowCount,
    evaluate,
    evaluation_windows,
    window_label,
)
from agentops_slo_platform.domain.spans import SpanKind


def slo(**overrides: object) -> SloDefinition:
    values: dict[str, object] = {
        "id": "tool-availability",
        "name": "Tool calls succeed",
        "sli": SliType.AVAILABILITY,
        "selector": Selector(kind=SpanKind.TOOL),
        "objective": 0.99,
        "window_days": 30,
        "owner": "claims-platform",
    }
    values.update(overrides)
    return SloDefinition.model_validate(values)


def counts(**by_label: tuple[int, int]) -> dict[timedelta, WindowCount]:
    result = {}
    for window in evaluation_windows():
        total, bad = by_label.get(window_label(window), (0, 0))
        result[window] = WindowCount(total=total, bad=bad)
    return result


def test_workbook_factors_for_thirty_days() -> None:
    factors = [round(r.factor(timedelta(days=30)), 6) for r in DEFAULT_RULES]
    assert factors == [14.4, 6.0, 3.0, 1.0]
    # 28-day windows scale proportionally.
    assert round(DEFAULT_RULES[0].factor(timedelta(days=28)), 2) == 13.44


def test_windows_are_sorted_and_labelled() -> None:
    labels = [window_label(w) for w in evaluation_windows()]
    assert labels == ["5m", "30m", "1h", "2h", "6h", "1d", "3d"]
    assert window_label(timedelta(seconds=45)) == "45s"


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"id": "Bad_ID"}, "slug"),
        ({"sli": SliType.LATENCY}, "latency_threshold_ms"),
        ({"latency_threshold_ms": 100.0}, "latency SLOs only"),
        (
            {"sli": SliType.WORKFLOW_SUCCESS, "selector": Selector(kind=SpanKind.TOOL)},
            "workflow",
        ),
    ],
)
def test_definition_validation(overrides: dict[str, object], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        slo(**overrides)


def test_objective_bounds() -> None:
    with pytest.raises(ValidationError):
        slo(objective=1.0)
    with pytest.raises(ValidationError):
        slo(objective=0.4)
    assert slo().error_budget == pytest.approx(0.01)


def test_no_data() -> None:
    status = evaluate(slo(), counts(), WindowCount(total=0, bad=0), now_ns=1)
    assert status.health is SloHealth.NO_DATA
    assert status.error_budget_remaining == 1.0
    assert status.sli_value is None
    assert all(r.long_burn is None and not r.firing for r in status.rules)


def test_fast_burn_pages_only_when_both_windows_burn() -> None:
    # 20% errors in both 1h and 5m: burn 20x > 14.4 -> page.
    status = evaluate(
        slo(),
        counts(**{"5m": (100, 20), "1h": (1000, 200)}),
        WindowCount(total=100_000, bad=300),
        now_ns=1,
    )
    page = next(r for r in status.rules if r.rule_id == "page-1h-5m")
    assert page.firing
    assert page.long_burn == pytest.approx(20.0)
    assert status.health is SloHealth.CRITICAL
    assert status.error_budget_remaining == pytest.approx(0.7)
    assert status.time_to_exhaustion_hours == pytest.approx(0.7 * 720 / 20, rel=1e-3)

    # The incident is over: the short window is clean, so the alert resets.
    recovered = evaluate(
        slo(),
        counts(**{"5m": (100, 0), "1h": (1000, 200)}),
        WindowCount(total=100_000, bad=300),
        now_ns=2,
    )
    assert not next(r for r in recovered.rules if r.rule_id == "page-1h-5m").firing
    assert recovered.health is SloHealth.OK


def test_slow_burn_raises_ticket() -> None:
    status = evaluate(
        slo(),
        counts(**{"2h": (2000, 80), "1d": (20000, 700)}),
        WindowCount(total=50_000, bad=900),
        now_ns=1,
    )
    ticket = next(r for r in status.rules if r.rule_id == "ticket-1d-2h")
    assert ticket.firing
    assert ticket.severity is Severity.TICKET
    assert status.health is SloHealth.WARNING


def test_min_events_suppresses_low_traffic_alerts() -> None:
    status = evaluate(
        slo(min_events=50),
        counts(**{"5m": (10, 10), "1h": (40, 40)}),
        WindowCount(total=40, bad=40),
        now_ns=1,
    )
    assert status.health is SloHealth.NO_DATA
    five = next(w for w in status.windows if w.window == "5m")
    assert five.error_ratio == 1.0
    assert five.burn_rate is None
    assert status.error_budget_remaining < 0  # overspent, reported honestly


def test_budget_overspent_has_no_exhaustion_estimate() -> None:
    status = evaluate(
        slo(),
        counts(**{"1h": (100, 50)}),
        WindowCount(total=1000, bad=500),
        now_ns=1,
    )
    assert status.error_budget_remaining == pytest.approx(1 - 500 / 10)
    assert status.time_to_exhaustion_hours is None
