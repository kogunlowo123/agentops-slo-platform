"""Ports the application layer depends on. Adapters live in infrastructure."""

from __future__ import annotations

from typing import Any, Protocol


class Clock(Protocol):
    """Injected time source (Unix epoch nanoseconds); tests drive it explicitly."""

    def now_ns(self) -> int: ...


class Auditor(Protocol):
    async def __call__(
        self,
        actor: str,
        action: str,
        resource: str,
        outcome: str,
        details: dict[str, Any] | None = None,
    ) -> None: ...
