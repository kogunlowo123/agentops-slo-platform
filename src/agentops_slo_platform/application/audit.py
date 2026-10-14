"""Adapter from the Auditor port to the agentic-core signed audit chain."""

from __future__ import annotations

from typing import Any

from agentic_core.audit import AuditLog
from agentic_core.observability import correlation_id


class ChainAuditor:
    def __init__(self, log: AuditLog) -> None:
        self._log = log

    async def __call__(
        self,
        actor: str,
        action: str,
        resource: str,
        outcome: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        await self._log.record(
            actor=actor,
            action=action,
            resource=resource,
            outcome=outcome,
            correlation_id=correlation_id(),
            details=details or {},
        )
