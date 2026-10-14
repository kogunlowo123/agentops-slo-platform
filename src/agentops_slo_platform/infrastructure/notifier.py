"""Alert delivery to Prometheus Alertmanager (``POST /api/v2/alerts``).

Alertmanager expects clients to re-send active alerts periodically and
resolves them itself once ``endsAt`` passes. The platform therefore re-posts
every firing burn-rate alert on each evaluation with ``endsAt`` a few
intervals ahead, and posts resolved alerts once with ``endsAt = now``. A
failed delivery is retried naturally by the next cycle.

When the identity fabric is configured, requests carry the platform's own
short-lived agent credential obtained with the RFC 7523 jwt-bearer grant
from its projected workload token, so an authorizing gateway in front of
Alertmanager can verify who is raising alerts.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

import httpx
from agentic_core.observability import get_logger
from pydantic import BaseModel, ConfigDict

from agentops_slo_platform.domain.errors import PlatformError
from agentops_slo_platform.infrastructure.fabric_client import FabricClient
from agentops_slo_platform.observability.metrics import PlatformMetrics

log = get_logger("agentops_slo_platform.notifier")


class Clock(Protocol):
    def now_ns(self) -> int: ...


class CredentialProvider:
    """Caches the platform's fabric credential and renews it before expiry."""

    def __init__(
        self,
        client: FabricClient,
        *,
        agent_id: str,
        workload_token_file: Path,
        clock: Clock,
        scope: str | None = None,
        renew_before_s: int = 60,
    ) -> None:
        self._client = client
        self._agent_id = agent_id
        self._token_file = workload_token_file
        self._clock = clock
        self._scope = scope
        self._renew_ns = renew_before_s * 1_000_000_000
        self._token: str | None = None
        self._expires_ns = 0
        self._lock = asyncio.Lock()

    async def bearer(self) -> str:
        async with self._lock:
            now = self._clock.now_ns()
            if self._token is None or now >= self._expires_ns - self._renew_ns:
                assertion = (await asyncio.to_thread(self._token_file.read_text)).strip()
                credential = await self._client.token_jwt_bearer(
                    client_id=self._agent_id, assertion=assertion, scope=self._scope
                )
                self._token = credential.access_token
                self._expires_ns = now + credential.expires_in * 1_000_000_000
            return self._token


class AlertNotice(BaseModel):
    model_config = ConfigDict(frozen=True)

    slo_id: str
    slo_name: str
    owner: str
    rule_id: str
    severity: str
    firing: bool
    long_burn: float | None
    short_burn: float | None
    factor: float
    since_ns: int


class AlertmanagerNotifier:
    def __init__(
        self,
        url: str,
        *,
        http: httpx.AsyncClient,
        metrics: PlatformMetrics,
        public_url: str,
        clock: Clock,
        resend_horizon_s: int,
        credentials: CredentialProvider | None = None,
    ) -> None:
        self._url = url.rstrip("/") + "/api/v2/alerts"
        self._http = http
        self._metrics = metrics
        self._public = public_url.rstrip("/")
        self._clock = clock
        self._horizon_ns = resend_horizon_s * 1_000_000_000
        self._credentials = credentials

    @staticmethod
    def _iso(ns: int) -> str:
        return datetime.fromtimestamp(ns / 1e9, tz=UTC).isoformat().replace("+00:00", "Z")

    def _payload(self, notice: AlertNotice, now_ns: int) -> dict[str, Any]:
        burn = "n/a" if notice.long_burn is None else f"{notice.long_burn:.2f}"
        return {
            "labels": {
                "alertname": "AgentSloErrorBudgetBurn",
                "slo": notice.slo_id,
                "rule": notice.rule_id,
                "severity": notice.severity,
                "team": notice.owner,
                "source": "agentops-slo-platform",
            },
            "annotations": {
                "summary": f"{notice.slo_name}: error budget burning at {burn}x",
                "description": (
                    f"Rule {notice.rule_id} requires {notice.factor:.2f}x in both windows; "
                    f"long={notice.long_burn}, short={notice.short_burn}."
                ),
                "runbook_url": "docs/runbooks/slo-burn-rate.md",
            },
            "startsAt": self._iso(notice.since_ns or now_ns),
            "endsAt": self._iso(now_ns + self._horizon_ns if notice.firing else now_ns),
            "generatorURL": f"{self._public}/v1/slos/{notice.slo_id}/status",
        }

    async def send(self, notices: list[AlertNotice]) -> bool:
        if not notices:
            return True
        now = self._clock.now_ns()
        headers: dict[str, str] = {}
        try:
            if self._credentials is not None:
                headers["Authorization"] = f"Bearer {await self._credentials.bearer()}"
            response = await self._http.post(
                self._url, json=[self._payload(n, now) for n in notices], headers=headers
            )
            response.raise_for_status()
        except (httpx.HTTPError, PlatformError, OSError) as exc:
            self._metrics.notifications_failed.inc(len(notices))
            log.warning("alert_delivery_failed", error=type(exc).__name__, alerts=len(notices))
            return False
        return True
