"""Agent span model and classification by OpenTelemetry GenAI semantic conventions.

An OTLP span is normalized into a :class:`SpanRecord` (wire-neutral, attributes
flattened) and then into a :class:`SpanFact`, the row the platform stores and
computes SLIs, costs and workflow outcomes from. Prompt and completion content
is never carried into a fact.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

# GenAI semantic convention attribute names (OpenTelemetry semconv gen_ai.*).
OPERATION = "gen_ai.operation.name"
AGENT_NAME = "gen_ai.agent.name"
AGENT_ID = "gen_ai.agent.id"
TOOL_NAME = "gen_ai.tool.name"
PROVIDER = "gen_ai.provider.name"
LEGACY_SYSTEM = "gen_ai.system"
REQUEST_MODEL = "gen_ai.request.model"
RESPONSE_MODEL = "gen_ai.response.model"
INPUT_TOKENS = "gen_ai.usage.input_tokens"
OUTPUT_TOKENS = "gen_ai.usage.output_tokens"
LEGACY_INPUT_TOKENS = "gen_ai.usage.prompt_tokens"
LEGACY_OUTPUT_TOKENS = "gen_ai.usage.completion_tokens"
CACHED_INPUT_TOKENS = "gen_ai.usage.cache_read.input_tokens"
FINISH_REASONS = "gen_ai.response.finish_reasons"
ERROR_TYPE = "error.type"
HTTP_STATUS = "http.response.status_code"
RESEND_COUNT = "http.request.resend_count"
DB_SYSTEM = "db.system.name"
LEGACY_DB_SYSTEM = "db.system"
DB_OPERATION = "db.operation.name"
SERVICE_NAME = "service.name"

# Platform attributes for what the GenAI conventions do not (yet) cover.
TEAM = "agentops.team"
WORKFLOW = "agentops.workflow.name"
KIND_OVERRIDE = "agentops.span.kind"
RETRY_ATTEMPT = "agentops.retry.attempt"

# Content-bearing attributes; dropped at ingestion, never persisted.
CONTENT_ATTRIBUTES = frozenset(
    {
        "gen_ai.prompt",
        "gen_ai.completion",
        "gen_ai.input.messages",
        "gen_ai.output.messages",
        "gen_ai.system_instructions",
        "gen_ai.tool.call.arguments",
        "gen_ai.tool.call.result",
    }
)

MODEL_OPERATIONS = frozenset({"chat", "text_completion", "generate_content", "embeddings"})
AGENT_OPERATIONS = frozenset({"invoke_agent", "create_agent"})
TOOL_OPERATIONS = frozenset({"execute_tool"})
MEMORY_OPERATIONS = frozenset({"search_memory", "update_memory", "create_memory", "delete_memory"})
WORKFLOW_OPERATIONS = frozenset({"invoke_workflow"})
VECTOR_STORES = frozenset(
    {"pinecone", "qdrant", "weaviate", "milvus", "chroma", "pgvector", "vertex_ai_vector_search"}
)

UNATTRIBUTED = "unattributed"
_LABEL_SAFE = re.compile(r"[^\x20-\x7e]")
MAX_LABEL_LENGTH = 120


class SpanKind(StrEnum):
    AGENT = "agent"
    TOOL = "tool"
    MODEL = "model"
    MEMORY = "memory"
    WORKFLOW = "workflow"
    OTHER = "other"


class StatusCode(StrEnum):
    UNSET = "unset"
    OK = "ok"
    ERROR = "error"


class SpanEvent(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str
    attributes: dict[str, Any] = Field(default_factory=dict)


class SpanRecord(BaseModel):
    """One OTLP span, wire-format independent. IDs are lowercase hex."""

    model_config = ConfigDict(frozen=True)

    trace_id: str
    span_id: str
    parent_span_id: str | None
    name: str
    start_ns: int
    end_ns: int
    status: StatusCode
    status_message: str = ""
    attributes: dict[str, Any] = Field(default_factory=dict)
    resource: dict[str, Any] = Field(default_factory=dict)
    events: tuple[SpanEvent, ...] = ()

    def attr(self, *names: str) -> Any:
        """First present attribute among ``names``: span attributes, then resource."""
        for name in names:
            if name in self.attributes:
                return self.attributes[name]
        for name in names:
            if name in self.resource:
                return self.resource[name]
        return None

    @property
    def is_root(self) -> bool:
        return self.parent_span_id is None


class SpanFact(BaseModel):
    """What the platform keeps about a span. No prompt or completion content."""

    model_config = ConfigDict(frozen=True)

    trace_id: str
    span_id: str
    parent_span_id: str | None
    kind: SpanKind
    name: str = Field(description="Tool, model, memory operation, agent or workflow name")
    operation: str
    agent: str
    team: str
    service: str
    provider: str | None = None
    model: str | None = None
    start_ns: int
    end_ns: int
    is_error: bool
    error_type: str | None = None
    error_message: str | None = None
    http_status: int | None = None
    finish_reasons: tuple[str, ...] = ()
    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0
    cost_usd: float | None = None
    retry_attempt: int | None = None
    workflow: str | None = None
    ingested_by: str = ""

    @property
    def duration_ms(self) -> float:
        return max(0, self.end_ns - self.start_ns) / 1_000_000

    @property
    def is_root(self) -> bool:
        return self.parent_span_id is None


def label_safe(value: object, *, default: str = UNATTRIBUTED) -> str:
    """Printable ASCII, bounded length; safe for metric labels and storage keys."""
    if value is None:
        return default
    text = _LABEL_SAFE.sub("_", str(value)).strip()
    return text[:MAX_LABEL_LENGTH] if text else default


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def _as_strings(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, list | tuple):
        return tuple(str(v) for v in value)
    return (str(value),)


def classify_kind(span: SpanRecord) -> SpanKind:  # noqa: PLR0911
    """Map a span to an agent-operations kind using GenAI semantic conventions."""
    override = span.attributes.get(KIND_OVERRIDE)
    if isinstance(override, str) and override in SpanKind.__members__.values():
        return SpanKind(override)
    operation = span.attributes.get(OPERATION)
    if operation in AGENT_OPERATIONS:
        return SpanKind.AGENT
    if operation in TOOL_OPERATIONS:
        return SpanKind.TOOL
    if operation in MODEL_OPERATIONS:
        return SpanKind.MODEL
    if operation in MEMORY_OPERATIONS:
        return SpanKind.MEMORY
    db_system = span.attributes.get(DB_SYSTEM, span.attributes.get(LEGACY_DB_SYSTEM))
    if isinstance(db_system, str) and db_system.lower() in VECTOR_STORES:
        return SpanKind.MEMORY
    if operation in WORKFLOW_OPERATIONS or (span.is_root and operation is None):
        return SpanKind.WORKFLOW
    return SpanKind.OTHER


def subject_name(span: SpanRecord, kind: SpanKind) -> str:
    """The dependency or unit a span measures: tool, model, memory op, agent, workflow."""
    match kind:
        case SpanKind.TOOL:
            return label_safe(span.attr(TOOL_NAME), default=label_safe(span.name))
        case SpanKind.MODEL:
            return label_safe(span.attr(RESPONSE_MODEL, REQUEST_MODEL), default="unknown-model")
        case SpanKind.MEMORY:
            return label_safe(span.attr(OPERATION, DB_OPERATION), default=label_safe(span.name))
        case SpanKind.AGENT:
            return label_safe(span.attr(AGENT_NAME, AGENT_ID), default=label_safe(span.name))
        case SpanKind.WORKFLOW:
            return label_safe(span.attr(WORKFLOW), default=label_safe(span.name))
    return label_safe(span.name)


def exception_details(span: SpanRecord) -> tuple[str | None, str | None]:
    """``exception.type``/``exception.message`` from the span's exception events."""
    for event in span.events:
        if event.name == "exception":
            exc_type = event.attributes.get("exception.type")
            message = event.attributes.get("exception.message")
            return (
                str(exc_type) if exc_type is not None else None,
                str(message) if message is not None else None,
            )
    return None, None


def is_error(span: SpanRecord) -> bool:
    if span.status is StatusCode.ERROR:
        return True
    if span.status is StatusCode.OK:
        return False
    return span.attributes.get(ERROR_TYPE) is not None


def token_counts(span: SpanRecord) -> tuple[int, int, int]:
    """(input, output, cached input) tokens, accepting current and legacy names."""
    input_tokens = _as_int(span.attributes.get(INPUT_TOKENS))
    if input_tokens is None:
        input_tokens = _as_int(span.attributes.get(LEGACY_INPUT_TOKENS))
    output_tokens = _as_int(span.attributes.get(OUTPUT_TOKENS))
    if output_tokens is None:
        output_tokens = _as_int(span.attributes.get(LEGACY_OUTPUT_TOKENS))
    cached = _as_int(span.attributes.get(CACHED_INPUT_TOKENS))
    return max(0, input_tokens or 0), max(0, output_tokens or 0), max(0, cached or 0)


def http_status(span: SpanRecord) -> int | None:
    return _as_int(span.attributes.get(HTTP_STATUS))


def retry_attempt(span: SpanRecord) -> int | None:
    value = _as_int(span.attributes.get(RETRY_ATTEMPT))
    return value if value is not None else _as_int(span.attributes.get(RESEND_COUNT))


def finish_reasons(span: SpanRecord) -> tuple[str, ...]:
    return _as_strings(span.attributes.get(FINISH_REASONS))


def ancestry_attribute(
    span_id: str | None,
    parents: Mapping[str, str | None],
    values: Mapping[str, str],
    *,
    max_depth: int = 64,
) -> str | None:
    """Walk parent links until an ancestor carries a value (bounded, cycle-safe)."""
    seen: set[str] = set()
    current = span_id
    for _ in range(max_depth):
        if current is None or current in seen:
            return None
        seen.add(current)
        if current in values:
            return values[current]
        current = parents.get(current)
    return None


def trace_ids(spans: Sequence[SpanRecord]) -> list[str]:
    return sorted({s.trace_id for s in spans})
