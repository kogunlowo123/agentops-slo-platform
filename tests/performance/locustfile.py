"""Load profile for a deployed platform.

Run against a staging deployment, never production:

    AGENTOPS_LOAD_AGENT_TOKEN=<collector credential with agentops:traces.ingest> \
    AGENTOPS_LOAD_OPERATOR_TOKEN=<operator token with agentops-viewer> \
    locust -f tests/performance/locustfile.py --host https://<staging host> \
           --users 50 --spawn-rate 10 --run-time 10m --headless

The mix reflects production: collectors export batches continuously, agent
runtimes poll the directive feed, and dashboards read SLO status and costs.
Each exported batch carries 100 traces of 4 spans with fresh IDs, timestamped
now, so the write path and workflow finalization are exercised for real.
"""

from __future__ import annotations

import os
import secrets
import time

from locust import HttpUser, between, task
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
from opentelemetry.proto.common.v1.common_pb2 import AnyValue, KeyValue

AGENT_TOKEN = os.environ.get("AGENTOPS_LOAD_AGENT_TOKEN", "")
OPERATOR_TOKEN = os.environ.get("AGENTOPS_LOAD_OPERATOR_TOKEN", "")


def _kv(key: str, value: str | int) -> KeyValue:
    any_value = (
        AnyValue(int_value=value) if isinstance(value, int) else AnyValue(string_value=value)
    )
    return KeyValue(key=key, value=any_value)


def export_batch(traces: int = 100) -> bytes:
    request = ExportTraceServiceRequest()
    resource_spans = request.resource_spans.add()
    resource_spans.resource.attributes.extend(
        [_kv("service.name", "load-agents"), _kv("agentops.team", "load-test")]
    )
    scope = resource_spans.scope_spans.add()
    now = time.time_ns()
    for _ in range(traces):
        trace_id = secrets.token_bytes(16)
        root_id = secrets.token_bytes(8)
        root = scope.spans.add()
        root.trace_id, root.span_id, root.name = trace_id, root_id, "load agent run"
        root.start_time_unix_nano, root.end_time_unix_nano = now - 2_000_000_000, now
        root.attributes.extend(
            [_kv("gen_ai.operation.name", "invoke_agent"), _kv("gen_ai.agent.name", "load-agent")]
        )
        for operation, extra in (
            ("execute_tool", [_kv("gen_ai.tool.name", "load.lookup")]),
            ("execute_tool", [_kv("gen_ai.tool.name", "load.search")]),
            (
                "chat",
                [
                    _kv("gen_ai.request.model", "load-model"),
                    _kv("gen_ai.usage.input_tokens", 800),
                    _kv("gen_ai.usage.output_tokens", 150),
                ],
            ),
        ):
            child = scope.spans.add()
            child.trace_id, child.span_id = trace_id, secrets.token_bytes(8)
            child.parent_span_id, child.name = root_id, operation
            child.start_time_unix_nano, child.end_time_unix_nano = now - 1_500_000_000, now - 10**9
            child.attributes.extend([_kv("gen_ai.operation.name", operation), *extra])
    return request.SerializeToString()


class CollectorExport(HttpUser):
    wait_time = between(0.5, 1.5)

    @task(10)
    def export(self) -> None:
        self.client.post(
            "/v1/traces",
            data=export_batch(),
            headers={
                "Authorization": f"Bearer {AGENT_TOKEN}",
                "Content-Type": "application/x-protobuf",
            },
            name="/v1/traces",
        )

    @task(3)
    def directives(self) -> None:
        self.client.get(
            "/v1/recovery/directives",
            headers={"Authorization": f"Bearer {AGENT_TOKEN}"},
            name="/v1/recovery/directives",
        )


class Dashboard(HttpUser):
    wait_time = between(2, 5)

    @task(3)
    def slo_status(self) -> None:
        self.client.get(
            "/v1/slos/tool-call-availability/status",
            headers={"Authorization": f"Bearer {OPERATOR_TOKEN}"},
            name="/v1/slos/{id}/status",
        )

    @task(1)
    def costs(self) -> None:
        self.client.get(
            "/v1/costs?group_by=team",
            headers={"Authorization": f"Bearer {OPERATOR_TOKEN}"},
            name="/v1/costs",
        )
