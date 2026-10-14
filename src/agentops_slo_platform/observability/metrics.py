"""Prometheus exposition: service metrics and agent operations metrics.

Label values derived from telemetry (agent, team, tool, model names) are
untrusted and unbounded. A per-label cardinality limiter admits the first N
distinct values and maps the rest to ``other``, so a misbehaving or hostile
exporter cannot explode the time-series count of the platform's own
Prometheus.
"""

from __future__ import annotations

import threading

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram

from agentops_slo_platform.domain.slo import SloStatus
from agentops_slo_platform.domain.spans import SpanFact, SpanKind
from agentops_slo_platform.domain.workflows import Diagnosis

OVERFLOW = "other"
# Operation latency buckets (seconds): agent operations range from sub-10ms
# memory reads to multi-minute agent runs. SLO latency thresholds exported as
# Prometheus rules must be one of these bounds.
DURATION_BUCKETS = (
    0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 5.0, 10.0, 20.0, 30.0, 60.0, 120.0, 300.0
)  # fmt: skip


class CardinalityLimiter:
    def __init__(self, limit: int) -> None:
        self._limit = limit
        self._seen: dict[str, set[str]] = {}
        self._lock = threading.Lock()
        self.overflowed: dict[str, int] = {}

    def admit(self, label: str, value: str) -> str:
        with self._lock:
            seen = self._seen.setdefault(label, set())
            if value in seen:
                return value
            if len(seen) < self._limit:
                seen.add(value)
                return value
            self.overflowed[label] = self.overflowed.get(label, 0) + 1
            return OVERFLOW


class PlatformMetrics:
    def __init__(
        self, *, label_limit: int = 500, registry: CollectorRegistry | None = None
    ) -> None:
        self.registry = registry or CollectorRegistry()
        self.limiter = CardinalityLimiter(label_limit)
        r = self.registry
        self.requests = Histogram(
            "agentops_http_request_duration_seconds",
            "HTTP request latency of the platform API",
            ["method", "route", "status"],
            registry=r,
            buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5),
        )
        self.spans_ingested = Counter(
            "agentops_spans_ingested_total", "Spans accepted by kind", ["kind"], registry=r
        )
        self.spans_duplicate = Counter(
            "agentops_spans_duplicate_total", "Spans already stored (exporter retries)", registry=r
        )
        self.spans_rejected = Counter(
            "agentops_spans_rejected_total", "Spans rejected by reason", ["reason"], registry=r
        )
        self.redactions = Counter(
            "agentops_redactions_total", "Secrets or PII removed from error text", registry=r
        )
        self.operations = Counter(
            "agentops_operations_total",
            "Agent operations (agent, tool, model, memory, workflow spans) by outcome",
            ["kind", "name", "agent", "team", "outcome"],
            registry=r,
        )
        self.operation_duration = Histogram(
            "agentops_operation_duration_seconds",
            "Agent operation latency",
            ["kind", "name"],
            registry=r,
            buckets=DURATION_BUCKETS,
        )
        self.tokens = Counter(
            "agentops_tokens_total",
            "Model tokens by agent, team, model and type",
            ["agent", "team", "model", "type"],
            registry=r,
        )
        self.cost = Counter(
            "agentops_cost_usd_total",
            "Attributed model spend in USD",
            ["agent", "team", "model"],
            registry=r,
        )
        self.unpriced_calls = Counter(
            "agentops_unpriced_model_calls_total",
            "Model calls with tokens but no configured price",
            ["model"],
            registry=r,
        )
        self.workflow_runs = Counter(
            "agentops_workflow_runs_total",
            "Classified workflow runs",
            ["workflow", "team", "outcome", "failure_class"],
            registry=r,
        )
        self.workflow_duration = Histogram(
            "agentops_workflow_duration_seconds",
            "Workflow run duration",
            ["workflow"],
            registry=r,
            buckets=DURATION_BUCKETS,
        )
        self.slo_budget_remaining = Gauge(
            "agentops_slo_error_budget_remaining",
            "Remaining error budget fraction (negative = overspent)",
            ["slo"],
            registry=r,
        )
        self.slo_burn_rate = Gauge(
            "agentops_slo_burn_rate", "Burn rate by window", ["slo", "window"], registry=r
        )
        self.slo_alert_firing = Gauge(
            "agentops_slo_alert_firing",
            "1 while a burn-rate rule fires",
            ["slo", "rule", "severity"],
            registry=r,
        )
        self.alert_transitions = Counter(
            "agentops_slo_alert_transitions_total",
            "Burn-rate alert state changes",
            ["severity", "transition"],
            registry=r,
        )
        self.notifications_failed = Counter(
            "agentops_alert_notifications_failed_total",
            "Alert notifications that could not be delivered",
            registry=r,
        )
        self.recovery_actions = Counter(
            "agentops_recovery_actions_total",
            "Recovery action proposals by action and resulting status",
            ["action", "status"],
            registry=r,
        )
        self.authz_decisions = Counter(
            "agentops_authorization_decisions_total",
            "Agent authorization decisions",
            ["action", "decision"],
            registry=r,
        )
        self.loop_failures = Counter(
            "agentops_background_failures_total",
            "Background loop iterations that raised",
            ["loop"],
            registry=r,
        )
        self.label_overflow = Gauge(
            "agentops_label_overflow",
            "Label values folded into 'other' by the cardinality limiter",
            ["label"],
            registry=r,
        )

    def _label(self, label: str, value: str) -> str:
        admitted = self.limiter.admit(label, value)
        if admitted == OVERFLOW:
            self.label_overflow.labels(label).set(self.limiter.overflowed[label])
        return admitted

    def observe_fact(self, fact: SpanFact) -> None:
        name = self._label("name", fact.name)
        agent = self._label("agent", fact.agent)
        team = self._label("team", fact.team)
        outcome = "error" if fact.is_error else "ok"
        self.spans_ingested.labels(fact.kind.value).inc()
        self.operations.labels(fact.kind.value, name, agent, team, outcome).inc()
        self.operation_duration.labels(fact.kind.value, name).observe(fact.duration_ms / 1000)
        if fact.kind is SpanKind.MODEL:
            model = self._label("model", fact.model or fact.name)
            if fact.input_tokens:
                self.tokens.labels(agent, team, model, "input").inc(fact.input_tokens)
            if fact.output_tokens:
                self.tokens.labels(agent, team, model, "output").inc(fact.output_tokens)
            if fact.cost_usd is not None:
                self.cost.labels(agent, team, model).inc(fact.cost_usd)
            elif fact.input_tokens or fact.output_tokens:
                self.unpriced_calls.labels(model).inc()

    def observe_workflow(self, diagnosis: Diagnosis) -> None:
        workflow = self._label("workflow", diagnosis.workflow)
        team = self._label("team", diagnosis.team)
        failure = diagnosis.failure_class.value if diagnosis.failure_class else "none"
        self.workflow_runs.labels(workflow, team, diagnosis.outcome.value, failure).inc()
        duration_s = max(0, diagnosis.ended_ns - diagnosis.started_ns) / 1e9
        self.workflow_duration.labels(workflow).observe(duration_s)

    def observe_slo(self, status: SloStatus) -> None:
        self.slo_budget_remaining.labels(status.slo_id).set(status.error_budget_remaining)
        for window in status.windows:
            if window.burn_rate is not None:
                self.slo_burn_rate.labels(status.slo_id, window.window).set(window.burn_rate)
        for rule in status.rules:
            self.slo_alert_firing.labels(status.slo_id, rule.rule_id, rule.severity.value).set(
                1 if rule.firing else 0
            )

    def forget_slo(self, slo_id: str) -> None:
        """Drop series of a deleted SLO so dashboards do not show stale values."""
        for gauge in (self.slo_budget_remaining, self.slo_burn_rate, self.slo_alert_firing):
            for labels in list(gauge._metrics):  # prometheus_client keeps children by label tuple
                if labels and labels[0] == slo_id:
                    gauge.remove(*labels)
