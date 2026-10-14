"""Generate Prometheus recording and alerting rules for an SLO.

The platform evaluates SLOs itself (for the API, MCP and A2A), and also
exports rules so the same objectives are enforced by Prometheus and
Alertmanager on the metrics the platform exposes. Both use the identical
burn-rate rule set from :mod:`agentops_slo_platform.domain.slo`.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, cast

from prometheus_client import utils as prometheus_utils

from agentops_slo_platform.domain.errors import PlatformError
from agentops_slo_platform.domain.slo import (
    DEFAULT_RULES,
    SliType,
    SloDefinition,
    evaluation_windows,
    window_label,
)
from agentops_slo_platform.domain.workflows import GOOD_OUTCOMES
from agentops_slo_platform.observability.metrics import DURATION_BUCKETS

_go_float = cast(Callable[[float], str], prometheus_utils.floatToGoString)


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _matchers(slo: SloDefinition) -> list[str]:
    s = slo.selector
    if slo.sli is SliType.WORKFLOW_SUCCESS:
        pairs = [("workflow", s.name), ("team", s.team)]
        if s.agent is not None:
            raise PlatformError("workflow metrics are not labelled by agent; remove selector.agent")
    else:
        pairs = [
            ("kind", s.kind.value if s.kind else None),
            ("name", s.name),
            ("agent", s.agent),
            ("team", s.team),
        ]
    return [f'{k}="{_escape(v)}"' for k, v in pairs if v is not None]


def _ratio(slo: SloDefinition, window: str) -> str:
    matchers = _matchers(slo)
    base = ",".join(matchers)
    if slo.sli is SliType.AVAILABILITY:
        bad = ",".join([*matchers, 'outcome="error"'])
        return (
            f"sum(rate(agentops_operations_total{{{bad}}}[{window}]))\n/\n"
            f"clamp_min(sum(rate(agentops_operations_total{{{base}}}[{window}])), 1e-12)"
        )
    if slo.sli is SliType.LATENCY:
        threshold_s = (slo.latency_threshold_ms or 0) / 1000
        if threshold_s not in DURATION_BUCKETS:
            raise PlatformError(
                f"latency threshold {threshold_s}s is not a histogram bucket bound; "
                f"use one of {list(DURATION_BUCKETS)} to export Prometheus rules"
            )
        good = ",".join([*matchers, f'le="{_go_float(threshold_s)}"'])
        return (
            "1 - (\n"
            f"  sum(rate(agentops_operation_duration_seconds_bucket{{{good}}}[{window}]))\n"
            "  /\n"
            f"  clamp_min(sum(rate(agentops_operation_duration_seconds_count{{{base}}}"
            f"[{window}])), 1e-12)\n)"
        )
    good_outcomes = "|".join(sorted(o.value for o in GOOD_OUTCOMES))
    bad = ",".join([*matchers, f'outcome!~"{good_outcomes}"'])
    return (
        f"sum(rate(agentops_workflow_runs_total{{{bad}}}[{window}]))\n/\n"
        f"clamp_min(sum(rate(agentops_workflow_runs_total{{{base}}}[{window}])), 1e-12)"
    )


def rule_groups(slo: SloDefinition) -> dict[str, Any]:
    """A Prometheus ``groups:`` document (rule_files / PrometheusRule spec)."""
    record = "agentops:slo_error_ratio:rate{window}"
    labels = {"slo": slo.id, "owner": slo.owner}
    recording = [
        {
            "record": record.format(window=window_label(w)),
            "expr": _ratio(slo, window_label(w)),
            "labels": labels,
        }
        for w in evaluation_windows()
    ]
    alerts = []
    for rule in DEFAULT_RULES:
        factor = rule.factor(slo.window)
        threshold = f"({factor:.6g} * {slo.error_budget:.6g})"
        long_metric = record.format(window=window_label(rule.long_window))
        short_metric = record.format(window=window_label(rule.short_window))
        alerts.append(
            {
                "alert": "AgentSloErrorBudgetBurn",
                "expr": (
                    f'{long_metric}{{slo="{slo.id}"}} > {threshold}\n'
                    f'and\n{short_metric}{{slo="{slo.id}"}} > {threshold}'
                ),
                "labels": {**labels, "severity": rule.severity.value, "rule": rule.id},
                "annotations": {
                    "summary": f"{slo.name}: error budget burn above {factor:.3g}x",
                    "description": (
                        f"{rule.budget_fraction:.0%} of the {slo.window_days}-day error budget "
                        f"would be spent within {window_label(rule.long_window)}."
                    ),
                    "runbook_url": "docs/runbooks/slo-burn-rate.md",
                },
            }
        )
    return {
        "groups": [
            {"name": f"agentops-slo-{slo.id}-recording", "interval": "30s", "rules": recording},
            {"name": f"agentops-slo-{slo.id}-alerts", "rules": alerts},
        ]
    }
