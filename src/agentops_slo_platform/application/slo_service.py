"""SLO management, evaluation and burn-rate alert lifecycle."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict

from agentops_slo_platform.domain.errors import PlatformError
from agentops_slo_platform.domain.ports import Auditor, Clock
from agentops_slo_platform.domain.slo import (
    DEFAULT_RULES,
    SliType,
    SloDefinition,
    SloStatus,
    evaluate,
    evaluation_windows,
)
from agentops_slo_platform.infrastructure.database import (
    AlertEvent,
    SqlAlertStore,
    SqlSloStore,
    SqlSpanStore,
    SqlWorkflowStore,
)
from agentops_slo_platform.infrastructure.notifier import AlertmanagerNotifier, AlertNotice
from agentops_slo_platform.observability.metrics import PlatformMetrics
from agentops_slo_platform.observability.rules import rule_groups


class FiringAlert(BaseModel):
    model_config = ConfigDict(frozen=True)

    slo_id: str
    rule_id: str
    severity: str
    since_ns: int


class AlertEventView(BaseModel):
    model_config = ConfigDict(frozen=True)

    slo_id: str
    rule_id: str
    severity: str
    transition: str
    at_ns: int
    long_burn: float | None
    short_burn: float | None


class EvaluationSummary(BaseModel):
    model_config = ConfigDict(frozen=True)

    evaluated: int
    fired: int
    resolved: int
    notified: bool


class SloService:
    def __init__(
        self,
        *,
        slos: SqlSloStore,
        alerts: SqlAlertStore,
        spans: SqlSpanStore,
        workflows: SqlWorkflowStore,
        metrics: PlatformMetrics,
        audit: Auditor,
        clock: Clock,
        notifier: AlertmanagerNotifier | None = None,
    ) -> None:
        self._slos = slos
        self._alerts = alerts
        self._spans = spans
        self._workflows = workflows
        self._metrics = metrics
        self._audit = audit
        self._clock = clock
        self._notifier = notifier

    # ---------------------------------------------------------------- definitions

    async def put(self, slo: SloDefinition, *, actor: str) -> bool:
        created = await self._slos.put(slo, actor=actor, now_ns=self._clock.now_ns())
        await self._audit(
            actor,
            "slo.create" if created else "slo.update",
            f"slo:{slo.id}",
            "success",
            {"objective": slo.objective, "sli": slo.sli.value, "window_days": slo.window_days},
        )
        return created

    async def get(self, slo_id: str) -> SloDefinition:
        return await self._slos.get(slo_id)

    async def all(self) -> list[SloDefinition]:
        return await self._slos.all()

    async def delete(self, slo_id: str, *, actor: str) -> None:
        await self._slos.delete(slo_id)
        self._metrics.forget_slo(slo_id)
        await self._audit(actor, "slo.delete", f"slo:{slo_id}", "success")

    async def seed(self, path: Path) -> int:
        """Create SLOs from a YAML file; existing definitions (possibly edited) are kept."""
        text = await asyncio.to_thread(path.read_text, encoding="utf-8")
        document = yaml.safe_load(text) or {}
        if not isinstance(document, dict) or not isinstance(document.get("slos"), list):
            raise PlatformError(f"{path.name}: expected a top-level 'slos' list")
        existing = {s.id for s in await self._slos.all()}
        created = 0
        for item in document["slos"]:
            slo = SloDefinition.model_validate(item)
            if slo.id not in existing:
                await self.put(slo, actor=f"seed:{path.name}")
                created += 1
        return created

    def prometheus_rules(self, slo: SloDefinition) -> dict[str, Any]:
        return rule_groups(slo)

    # ------------------------------------------------------------------ evaluation

    async def status(self, slo_id: str) -> SloStatus:
        return await self._status(await self._slos.get(slo_id), self._clock.now_ns())

    async def _status(self, slo: SloDefinition, now_ns: int) -> SloStatus:
        windows = evaluation_windows()
        store = self._workflows if slo.sli is SliType.WORKFLOW_SUCCESS else self._spans
        counts, budget = await store.window_counts(slo, windows, now_ns=now_ns)
        status = evaluate(slo, counts, budget, now_ns=now_ns)
        self._metrics.observe_slo(status)
        return status

    async def firing(self) -> list[FiringAlert]:
        severities = {r.id: r.severity.value for r in DEFAULT_RULES}
        return [
            FiringAlert(
                slo_id=slo_id, rule_id=rule_id, severity=severities.get(rule_id, ""), since_ns=since
            )
            for (slo_id, rule_id), since in sorted((await self._alerts.firing()).items())
        ]

    async def history(self, *, slo_id: str | None, limit: int) -> list[AlertEventView]:
        return [
            AlertEventView(**e.__dict__)
            for e in await self._alerts.events(slo_id=slo_id, limit=limit)
        ]

    async def evaluate_all(self) -> EvaluationSummary:
        """Evaluate every SLO, move alert state, and notify Alertmanager."""
        now = self._clock.now_ns()
        firing_before = await self._alerts.firing()
        notices: list[AlertNotice] = []
        fired = resolved = 0
        definitions = await self._slos.all()
        for slo in definitions:
            status = await self._status(slo, now)
            for rule in status.rules:
                key = (slo.id, rule.rule_id)
                was_firing = key in firing_before
                since = firing_before.get(key, now)
                changed = False
                if rule.firing != was_firing:
                    changed = await self._alerts.transition(
                        AlertEvent(
                            slo_id=slo.id,
                            rule_id=rule.rule_id,
                            severity=rule.severity.value,
                            transition="firing" if rule.firing else "resolved",
                            at_ns=now,
                            long_burn=rule.long_burn,
                            short_burn=rule.short_burn,
                        )
                    )
                if changed:
                    transition = "firing" if rule.firing else "resolved"
                    fired += rule.firing
                    resolved += not rule.firing
                    since = now
                    self._metrics.alert_transitions.labels(rule.severity.value, transition).inc()
                    await self._audit(
                        "agentops-slo-evaluator",
                        f"slo.alert.{transition}",
                        f"slo:{slo.id}",
                        "success",
                        {
                            "rule": rule.rule_id,
                            "severity": rule.severity.value,
                            "long_burn": rule.long_burn,
                            "short_burn": rule.short_burn,
                            "factor": rule.factor,
                        },
                    )
                if rule.firing or changed:
                    notices.append(
                        AlertNotice(
                            slo_id=slo.id,
                            slo_name=slo.name,
                            owner=slo.owner,
                            rule_id=rule.rule_id,
                            severity=rule.severity.value,
                            firing=rule.firing,
                            long_burn=rule.long_burn,
                            short_burn=rule.short_burn,
                            factor=rule.factor,
                            since_ns=since,
                        )
                    )
        notified = True
        if self._notifier is not None:
            notified = await self._notifier.send(notices)
        return EvaluationSummary(
            evaluated=len(definitions), fired=fired, resolved=resolved, notified=notified
        )
