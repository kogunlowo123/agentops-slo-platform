"""Token pricing and cost attribution.

Prices are operator configuration, never constants: contracted rates differ
by enterprise and change over time. Each price is effective-dated, and a
span is priced with the rate in force when it started, so re-pricing never
rewrites history. Model calls with no matching price are counted as
unpriced rather than silently costed at zero.
"""

from __future__ import annotations

import bisect
from collections import defaultdict
from collections.abc import Iterable
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class PriceEntry(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    model: str = Field(min_length=1, max_length=120, description="gen_ai response/request model")
    provider: str | None = Field(default=None, max_length=120)
    input_per_mtok: float = Field(ge=0, description="USD per million input tokens")
    output_per_mtok: float = Field(ge=0, description="USD per million output tokens")
    cached_input_per_mtok: float | None = Field(
        default=None, ge=0, description="USD per million cache-read input tokens"
    )
    effective_from_ns: int = Field(ge=0, description="Unix epoch nanoseconds")

    def cost(self, input_tokens: int, output_tokens: int, cached_input_tokens: int = 0) -> float:
        cached = min(cached_input_tokens, input_tokens)
        cached_rate = (
            self.cached_input_per_mtok
            if self.cached_input_per_mtok is not None
            else self.input_per_mtok
        )
        return (
            (input_tokens - cached) * self.input_per_mtok
            + cached * cached_rate
            + output_tokens * self.output_per_mtok
        ) / 1_000_000


class PriceBook:
    """Effective-dated lookup: the latest price whose start is not after the span."""

    def __init__(self, entries: Iterable[PriceEntry] = ()) -> None:
        by_model: dict[str, list[PriceEntry]] = defaultdict(list)
        for entry in entries:
            by_model[entry.model].append(entry)
        self._entries = {
            m: sorted(v, key=lambda e: e.effective_from_ns) for m, v in by_model.items()
        }
        self._starts = {m: [e.effective_from_ns for e in v] for m, v in self._entries.items()}

    def price(self, model: str | None, at_ns: int) -> PriceEntry | None:
        if model is None or model not in self._entries:
            return None
        index = bisect.bisect_right(self._starts[model], at_ns) - 1
        return self._entries[model][index] if index >= 0 else None

    def entries(self) -> list[PriceEntry]:
        return [e for model in sorted(self._entries) for e in self._entries[model]]


class CostGrouping(StrEnum):
    AGENT = "agent"
    TEAM = "team"
    MODEL = "model"
    WORKFLOW = "workflow"


class CostRow(BaseModel):
    model_config = ConfigDict(frozen=True)

    key: str
    model_calls: int
    input_tokens: int
    output_tokens: int
    cached_input_tokens: int
    cost_usd: float
    unpriced_calls: int = Field(description="Model calls with tokens but no applicable price")


class CostReport(BaseModel):
    model_config = ConfigDict(frozen=True)

    group_by: CostGrouping
    start_ns: int
    end_ns: int
    currency: str = "USD"
    total_cost_usd: float
    rows: tuple[CostRow, ...]
