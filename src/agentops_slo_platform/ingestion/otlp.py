"""OTLP/HTTP trace decoding (opentelemetry-proto ``ExportTraceServiceRequest``).

Both encodings from the OTLP specification are accepted:

- ``application/x-protobuf``: binary protobuf.
- ``application/json``: OTLP/JSON, where trace and span IDs are hex strings
  (not the base64 of the generic protobuf JSON mapping). IDs are converted
  and the document is parsed with the protobuf JSON parser, so field-name
  variants (lowerCamelCase and snake_case), int64-as-string and enum integers
  are all handled the same way as the reference implementations.

``Content-Encoding: gzip`` is supported with a hard limit on the inflated
size, so a small compressed body cannot expand into an unbounded allocation.
"""

from __future__ import annotations

import base64
import binascii
import json
import zlib
from typing import Any

from google.protobuf import json_format
from google.protobuf.message import DecodeError
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
    ExportTraceServiceRequest,
    ExportTraceServiceResponse,
)
from opentelemetry.proto.common.v1.common_pb2 import AnyValue, KeyValue

from agentops_slo_platform.domain.errors import (
    PayloadTooLargeError,
    PlatformError,
    UnsupportedMediaTypeError,
)
from agentops_slo_platform.domain.spans import (
    CONTENT_ATTRIBUTES,
    SpanEvent,
    SpanRecord,
    StatusCode,
)

PROTOBUF = "application/x-protobuf"
JSON = "application/json"
_STATUS = {0: StatusCode.UNSET, 1: StatusCode.OK, 2: StatusCode.ERROR}
_ID_FIELDS = ("traceId", "trace_id", "spanId", "span_id", "parentSpanId", "parent_span_id")
MAX_ATTRIBUTE_DEPTH = 4
MAX_STRING = 4096


def media_type(content_type: str | None) -> str:
    value = (content_type or "").split(";", 1)[0].strip().lower()
    if value not in (PROTOBUF, JSON):
        raise UnsupportedMediaTypeError(f"content type must be {PROTOBUF} or {JSON}")
    return value


def inflate(body: bytes, encoding: str | None, *, max_bytes: int) -> bytes:
    value = (encoding or "identity").strip().lower()
    if value == "identity":
        if len(body) > max_bytes:
            raise PayloadTooLargeError(f"body exceeds {max_bytes} bytes")
        return body
    if value != "gzip":
        raise UnsupportedMediaTypeError("content encoding must be gzip or identity")
    decompressor = zlib.decompressobj(wbits=16 + zlib.MAX_WBITS)
    try:
        data = decompressor.decompress(body, max_bytes + 1)
    except zlib.error as exc:
        raise PlatformError("body is not valid gzip") from exc
    if len(data) > max_bytes or decompressor.unconsumed_tail:
        raise PayloadTooLargeError(f"inflated body exceeds {max_bytes} bytes")
    if not decompressor.eof:
        raise PlatformError("truncated gzip body")
    return data


def _hex_to_b64(document: Any) -> Any:
    """Rewrite OTLP/JSON hex IDs to the base64 bytes encoding protobuf JSON expects."""
    if isinstance(document, dict):
        out: dict[str, Any] = {}
        for key, value in document.items():
            if key in _ID_FIELDS and isinstance(value, str):
                try:
                    out[key] = base64.b64encode(bytes.fromhex(value)).decode()
                except ValueError as exc:
                    raise PlatformError(f"{key} must be hex") from exc
            else:
                out[key] = _hex_to_b64(value)
        return out
    if isinstance(document, list):
        return [_hex_to_b64(v) for v in document]
    return document


def parse_request(body: bytes, content_type: str) -> ExportTraceServiceRequest:
    request = ExportTraceServiceRequest()
    if content_type == PROTOBUF:
        try:
            request.ParseFromString(body)
        except DecodeError as exc:
            raise PlatformError("body is not a valid ExportTraceServiceRequest") from exc
        return request
    try:
        document = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PlatformError("body is not valid JSON") from exc
    if not isinstance(document, dict):
        raise PlatformError("OTLP/JSON body must be an object")
    try:
        json_format.ParseDict(_hex_to_b64(document), request, ignore_unknown_fields=True)
    except (json_format.ParseError, binascii.Error, ValueError, TypeError) as exc:
        raise PlatformError(f"invalid OTLP/JSON: {exc}") from exc
    return request


def _value(value: AnyValue, depth: int = 0) -> Any:  # noqa: PLR0911
    field = value.WhichOneof("value")
    if field is None:
        return None
    if field == "string_value":
        return value.string_value[:MAX_STRING]
    if field == "bool_value":
        return value.bool_value
    if field == "int_value":
        return value.int_value
    if field == "double_value":
        return value.double_value
    if field == "bytes_value":
        return value.bytes_value.hex()[:MAX_STRING]
    if depth >= MAX_ATTRIBUTE_DEPTH:
        return None
    if field == "array_value":
        return [_value(v, depth + 1) for v in value.array_value.values]
    return {kv.key: _value(kv.value, depth + 1) for kv in value.kvlist_value.values}


def attributes(items: Any) -> dict[str, Any]:
    """Flatten KeyValue lists, dropping prompt/completion content attributes."""
    result: dict[str, Any] = {}
    for kv in items:
        item: KeyValue = kv
        if item.key in CONTENT_ATTRIBUTES:
            continue
        result[item.key] = _value(item.value)
    return result


def records(request: ExportTraceServiceRequest) -> tuple[list[SpanRecord], list[str]]:
    """Normalized spans plus a reason for every span that was rejected as malformed."""
    spans: list[SpanRecord] = []
    rejected: list[str] = []
    for resource_spans in request.resource_spans:
        resource = attributes(resource_spans.resource.attributes)
        for scope_spans in resource_spans.scope_spans:
            for span in scope_spans.spans:
                trace_id, span_id = span.trace_id.hex(), span.span_id.hex()
                if len(span.trace_id) != 16 or not any(span.trace_id):
                    rejected.append("invalid trace_id")
                    continue
                if len(span.span_id) != 8 or not any(span.span_id):
                    rejected.append("invalid span_id")
                    continue
                if span.parent_span_id and len(span.parent_span_id) != 8:
                    rejected.append("invalid parent_span_id")
                    continue
                if span.end_time_unix_nano < span.start_time_unix_nano:
                    rejected.append("end before start")
                    continue
                parent = span.parent_span_id.hex() if any(span.parent_span_id) else None
                spans.append(
                    SpanRecord(
                        trace_id=trace_id,
                        span_id=span_id,
                        parent_span_id=parent,
                        name=span.name[:256] or "unnamed",
                        start_ns=span.start_time_unix_nano,
                        end_ns=span.end_time_unix_nano,
                        status=_STATUS.get(span.status.code, StatusCode.UNSET),
                        status_message=span.status.message[:MAX_STRING],
                        attributes=attributes(span.attributes),
                        resource=resource,
                        events=tuple(
                            SpanEvent(name=e.name[:128], attributes=attributes(e.attributes))
                            for e in span.events
                            if e.name == "exception"
                        ),
                    )
                )
    return spans, rejected


def response(content_type: str, *, rejected: int, message: str) -> tuple[bytes, str]:
    """ExportTraceServiceResponse in the request's encoding (partial success if any rejected)."""
    reply = ExportTraceServiceResponse()
    if rejected:
        reply.partial_success.rejected_spans = rejected
        reply.partial_success.error_message = message
    if content_type == PROTOBUF:
        return reply.SerializeToString(), PROTOBUF
    return json.dumps(json_format.MessageToDict(reply)).encode(), JSON
