"""OTLP/HTTP decoding: encodings, compression limits, normalization and validation."""

from __future__ import annotations

import gzip
import json

import pytest
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceResponse

from agentops_slo_platform.domain.errors import (
    PayloadTooLargeError,
    PlatformError,
    UnsupportedMediaTypeError,
)
from agentops_slo_platform.domain.spans import StatusCode
from agentops_slo_platform.ingestion import otlp
from tests.factories import OtlpSpan, json_body, protobuf_body, request


def test_media_types() -> None:
    assert otlp.media_type("application/x-protobuf") == otlp.PROTOBUF
    assert otlp.media_type("Application/JSON; charset=utf-8") == otlp.JSON
    with pytest.raises(UnsupportedMediaTypeError):
        otlp.media_type("text/plain")
    with pytest.raises(UnsupportedMediaTypeError):
        otlp.media_type(None)


def test_inflate_identity_and_gzip() -> None:
    assert otlp.inflate(b"abc", None, max_bytes=10) == b"abc"
    assert otlp.inflate(gzip.compress(b"abc"), "gzip", max_bytes=10) == b"abc"
    with pytest.raises(PayloadTooLargeError):
        otlp.inflate(b"x" * 11, "identity", max_bytes=10)
    with pytest.raises(UnsupportedMediaTypeError):
        otlp.inflate(b"abc", "br", max_bytes=10)
    with pytest.raises(PlatformError, match="not valid gzip"):
        otlp.inflate(b"not gzip at all", "gzip", max_bytes=10)
    with pytest.raises(PlatformError, match="truncated"):
        otlp.inflate(gzip.compress(b"abcdef")[:-8], "gzip", max_bytes=100)


def test_inflate_stops_at_limit_without_inflating_everything() -> None:
    bomb = gzip.compress(b"\0" * 10_000_000)
    assert len(bomb) < 20_000
    with pytest.raises(PayloadTooLargeError):
        otlp.inflate(bomb, "gzip", max_bytes=1_000_000)


def test_protobuf_and_json_decode_to_the_same_records() -> None:
    root = OtlpSpan(
        name="run",
        start_ns=1_000,
        attributes={"gen_ai.operation.name": "invoke_agent", "flag": True, "ratio": 0.5},
        ok=True,
    )
    child = root.child(
        "tool",
        attributes={
            "gen_ai.tool.name": "crm",
            "tags": ["a", 2],
            "nested": {"k": "v"},
            "raw": b"\x01\x02",
            "gen_ai.input.messages": "patient said ...",
        },
        error=True,
        status_message="boom",
        exception=("ValueError", "bad input"),
    )
    from_pb, rejected_pb = otlp.records(
        otlp.parse_request(protobuf_body([root, child]), otlp.PROTOBUF)
    )
    from_json, rejected_json = otlp.records(otlp.parse_request(json_body([root, child]), otlp.JSON))
    assert rejected_pb == rejected_json == []
    assert from_pb == from_json
    parent, tool = from_pb
    assert parent.is_root
    assert parent.status is StatusCode.OK
    assert parent.attributes == {
        "gen_ai.operation.name": "invoke_agent",
        "flag": True,
        "ratio": 0.5,
    }
    assert tool.parent_span_id == root.span_id
    assert tool.status is StatusCode.ERROR
    assert tool.attributes["tags"] == ["a", 2]
    assert tool.attributes["nested"] == {"k": "v"}
    assert tool.attributes["raw"] == "0102"
    assert "gen_ai.input.messages" not in tool.attributes  # content never retained
    assert tool.events[0].attributes["exception.type"] == "ValueError"
    assert parent.resource == {"service.name": "claims-agents"}


def test_json_accepts_snake_case_and_numeric_strings() -> None:
    body = {
        "resource_spans": [
            {
                "scope_spans": [
                    {
                        "spans": [
                            {
                                "trace_id": "0af7651916cd43dd8448eb211c80319c",
                                "span_id": "b7ad6b7169203331",
                                "name": "x",
                                "start_time_unix_nano": "1000",
                                "end_time_unix_nano": 2000,
                                "status": {"code": 2},
                                "unknownField": 1,
                            }
                        ]
                    }
                ]
            }
        ]
    }
    spans, _ = otlp.records(otlp.parse_request(json.dumps(body).encode(), otlp.JSON))
    assert spans[0].end_ns == 2000
    assert spans[0].status is StatusCode.ERROR


@pytest.mark.parametrize(
    ("body", "message"),
    [
        (b"\xff\xfe", "valid JSON"),
        (b"[1, 2]", "must be an object"),
        (b'{"resourceSpans": [{"scopeSpans": [{"spans": [{"traceId": "zz"}]}]}]}', "hex"),
        (b'{"resourceSpans": "nope"}', "invalid OTLP/JSON"),
    ],
)
def test_invalid_json(body: bytes, message: str) -> None:
    with pytest.raises(PlatformError, match=message):
        otlp.parse_request(body, otlp.JSON)


def test_invalid_protobuf() -> None:
    with pytest.raises(PlatformError, match="ExportTraceServiceRequest"):
        otlp.parse_request(b"\xff\xff\xff\xff", otlp.PROTOBUF)


def test_malformed_spans_are_rejected_individually() -> None:
    good = OtlpSpan(name="ok", start_ns=10)
    message = request([good])
    spans = message.resource_spans[0].scope_spans[0].spans
    zero_trace = spans.add()
    zero_trace.CopyFrom(spans[0])
    zero_trace.trace_id = bytes(16)
    short_span = spans.add()
    short_span.CopyFrom(spans[0])
    short_span.span_id = b"\x01"
    bad_parent = spans.add()
    bad_parent.CopyFrom(spans[0])
    bad_parent.parent_span_id = b"\x01\x02"
    backwards = spans.add()
    backwards.CopyFrom(spans[0])
    backwards.end_time_unix_nano = 1
    deep = spans.add()
    deep.CopyFrom(spans[0])
    deep.name = ""
    value = deep.attributes.add()
    value.key = "deep"
    level = value.value
    for _ in range(6):
        level = level.array_value.values.add()
    level.string_value = "bottom"
    records, rejected = otlp.records(message)
    assert sorted(rejected) == [
        "end before start",
        "invalid parent_span_id",
        "invalid span_id",
        "invalid trace_id",
    ]
    assert len(records) == 2
    assert records[1].name == "unnamed"
    assert json.dumps(records[1].attributes["deep"]).count("[") <= 4


def test_response_encodings() -> None:
    body, media = otlp.response(otlp.PROTOBUF, rejected=2, message="bad ids")
    reply = ExportTraceServiceResponse()
    reply.ParseFromString(body)
    assert media == otlp.PROTOBUF
    assert reply.partial_success.rejected_spans == 2
    body, media = otlp.response(otlp.JSON, rejected=0, message="")
    assert media == otlp.JSON
    assert json.loads(body) == {}
