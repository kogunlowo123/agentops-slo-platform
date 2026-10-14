"""Builders for OTLP payloads and span facts (synthetic telemetry only)."""

from __future__ import annotations

import base64
import gzip
import itertools
import json
from dataclasses import dataclass, field
from typing import Any

from google.protobuf import json_format
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
from opentelemetry.proto.common.v1.common_pb2 import AnyValue, KeyValue
from opentelemetry.proto.trace.v1.trace_pb2 import Span, Status

from agentops_slo_platform.domain.spans import SpanFact, SpanKind

NS = 1_000_000_000
_ids = itertools.count(1)


def new_trace_id() -> str:
    return f"{next(_ids):032x}"


def new_span_id() -> str:
    return f"{next(_ids):016x}"


def any_value(value: Any) -> AnyValue:  # noqa: PLR0911
    if isinstance(value, bool):
        return AnyValue(bool_value=value)
    if isinstance(value, int):
        return AnyValue(int_value=value)
    if isinstance(value, float):
        return AnyValue(double_value=value)
    if isinstance(value, bytes):
        return AnyValue(bytes_value=value)
    if isinstance(value, list | tuple):
        result = AnyValue()
        result.array_value.values.extend(any_value(v) for v in value)
        return result
    if isinstance(value, dict):
        result = AnyValue()
        result.kvlist_value.values.extend(
            KeyValue(key=k, value=any_value(v)) for k, v in value.items()
        )
        return result
    return AnyValue(string_value=str(value))


@dataclass
class OtlpSpan:
    name: str
    start_ns: int
    duration_ms: float = 100.0
    trace_id: str = field(default_factory=new_trace_id)
    span_id: str = field(default_factory=new_span_id)
    parent_span_id: str | None = None
    attributes: dict[str, Any] = field(default_factory=dict)
    error: bool = False
    status_message: str = ""
    ok: bool = False
    exception: tuple[str, str] | None = None

    @property
    def end_ns(self) -> int:
        return self.start_ns + int(self.duration_ms * 1_000_000)

    def child(self, name: str, **kwargs: Any) -> OtlpSpan:
        kwargs.setdefault("start_ns", self.start_ns + 1_000_000)
        return OtlpSpan(name=name, trace_id=self.trace_id, parent_span_id=self.span_id, **kwargs)


def agent_span(name: str, start_ns: int, *, agent: str = "claims-triage", **kw: Any) -> OtlpSpan:
    attrs = {"gen_ai.operation.name": "invoke_agent", "gen_ai.agent.name": agent}
    attrs.update(kw.pop("attributes", {}))
    return OtlpSpan(name=name, start_ns=start_ns, attributes=attrs, **kw)


def tool_attrs(tool: str, **extra: Any) -> dict[str, Any]:
    return {"gen_ai.operation.name": "execute_tool", "gen_ai.tool.name": tool, **extra}


def model_attrs(
    model: str = "gpt-4.1", input_tokens: int = 1000, output_tokens: int = 200, **extra: Any
) -> dict[str, Any]:
    return {
        "gen_ai.operation.name": "chat",
        "gen_ai.provider.name": "openai",
        "gen_ai.request.model": model,
        "gen_ai.response.model": model,
        "gen_ai.usage.input_tokens": input_tokens,
        "gen_ai.usage.output_tokens": output_tokens,
        **extra,
    }


def request(
    spans: list[OtlpSpan], *, resource: dict[str, Any] | None = None
) -> ExportTraceServiceRequest:
    message = ExportTraceServiceRequest()
    resource_spans = message.resource_spans.add()
    for key, value in (resource or {"service.name": "claims-agents"}).items():
        resource_spans.resource.attributes.append(KeyValue(key=key, value=any_value(value)))
    scope = resource_spans.scope_spans.add()
    scope.scope.name = "agent-sdk"
    for item in spans:
        span = scope.spans.add()
        span.trace_id = bytes.fromhex(item.trace_id)
        span.span_id = bytes.fromhex(item.span_id)
        if item.parent_span_id:
            span.parent_span_id = bytes.fromhex(item.parent_span_id)
        span.name = item.name
        span.kind = Span.SPAN_KIND_INTERNAL
        span.start_time_unix_nano = item.start_ns
        span.end_time_unix_nano = item.end_ns
        for key, value in item.attributes.items():
            span.attributes.append(KeyValue(key=key, value=any_value(value)))
        if item.error:
            span.status.code = Status.STATUS_CODE_ERROR
            span.status.message = item.status_message
        elif item.ok:
            span.status.code = Status.STATUS_CODE_OK
        if item.exception:
            event = span.events.add()
            event.name = "exception"
            event.time_unix_nano = item.end_ns
            event.attributes.append(
                KeyValue(key="exception.type", value=any_value(item.exception[0]))
            )
            event.attributes.append(
                KeyValue(key="exception.message", value=any_value(item.exception[1]))
            )
    return message


def protobuf_body(spans: list[OtlpSpan], **kw: Any) -> bytes:
    return request(spans, **kw).SerializeToString()


def json_body(spans: list[OtlpSpan], **kw: Any) -> bytes:
    """OTLP/JSON with hex IDs, as an OpenTelemetry SDK exporter would send it."""
    document = json_format.MessageToDict(request(spans, **kw))
    for rs in document.get("resourceSpans", []):
        for ss in rs.get("scopeSpans", []):
            for span in ss.get("spans", []):
                for key in ("traceId", "spanId", "parentSpanId"):
                    if key in span:
                        span[key] = base64.b64decode(span[key]).hex()
    return json.dumps(document).encode()


def gzipped(body: bytes) -> bytes:
    return gzip.compress(body)


def fact(
    *,
    kind: SpanKind = SpanKind.TOOL,
    name: str = "crm.lookup",
    trace_id: str | None = None,
    span_id: str | None = None,
    parent: str | None = "f" * 16,
    start_ns: int = 0,
    duration_ms: float = 100.0,
    error: bool = False,
    **extra: Any,
) -> SpanFact:
    values: dict[str, Any] = {
        "trace_id": trace_id or new_trace_id(),
        "span_id": span_id or new_span_id(),
        "parent_span_id": parent,
        "kind": kind,
        "name": name,
        "operation": "execute_tool",
        "agent": "claims-triage",
        "team": "claims",
        "service": "claims-agents",
        "start_ns": start_ns,
        "end_ns": start_ns + int(duration_ms * 1_000_000),
        "is_error": error,
    }
    values.update(extra)
    return SpanFact.model_validate(values)
