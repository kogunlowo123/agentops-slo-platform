"""Full lifecycle through the ASGI app: Task -> Trace -> Metrics -> Cost -> Recovery."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from agentops_slo_platform.runtime.container import Container
from tests.conftest import NOW_NS, FakeClock, FakeIdp, agent_headers, operator_headers
from tests.factories import (
    OtlpSpan,
    agent_span,
    gzipped,
    json_body,
    model_attrs,
    protobuf_body,
    tool_attrs,
)

pytestmark = pytest.mark.e2e

SLO = {
    "id": "crm-availability",
    "name": "CRM lookups succeed",
    "sli": "availability",
    "selector": {"kind": "tool", "name": "crm.lookup"},
    "objective": 0.99,
    "owner": "claims-platform",
}
PROTOBUF = {"content-type": "application/x-protobuf"}


def container_of(client: TestClient) -> Container:
    value: Container = client.app.state.container  # type: ignore[attr-defined]
    return value


def failing_traces(count: int, *, failures: int) -> list[OtlpSpan]:
    spans: list[OtlpSpan] = []
    for i in range(count):
        root = agent_span(
            "claims-triage run",
            NOW_NS - (60 + i) * 10**9,
            duration_ms=3000,
            attributes={"agentops.workflow.name": "claims-intake"},
        )
        failed = i < failures
        tool = root.child(
            "crm",
            attributes=tool_attrs(
                "crm.lookup", **{"http.response.status_code": 503 if failed else 200}
            ),
            error=failed,
            status_message="upstream unavailable" if failed else "",
        )
        llm = root.child("chat", attributes=model_attrs(), duration_ms=800)
        root.error = failed
        spans += [root, tool, llm]
    return spans


def test_platform_lifecycle(  # noqa: PLR0915 - one end-to-end scenario
    client: TestClient, agent_idp: FakeIdp, operator_idp: FakeIdp, clock: FakeClock
) -> None:
    sre = operator_headers(operator_idp, "agentops-sre")
    sre_2 = operator_headers(operator_idp, "agentops-sre", sub="bob@corp.test")
    finops = operator_headers(operator_idp, "agentops-finops")
    viewer = operator_headers(operator_idp, "agentops-viewer")
    agent = agent_headers(agent_idp)

    # Operators define the objective and the price book.
    assert client.put("/v1/slos/crm-availability", json=SLO, headers=sre).status_code == 201
    assert client.put("/v1/slos/crm-availability", json=SLO, headers=sre).status_code == 200
    price = {
        "model": "gpt-4.1",
        "input_per_mtok": 2.0,
        "output_per_mtok": 8.0,
        "effective_from_ns": 0,
    }
    assert client.put("/v1/pricing", json=price, headers=finops).status_code == 200
    assert client.get("/v1/pricing", headers=viewer).json()["items"][0]["model"] == "gpt-4.1"

    # The collector exports traces (protobuf, gzip): 20 workflows, 5 with failing CRM calls.
    spans = failing_traces(20, failures=5)
    body = gzipped(
        protobuf_body(spans, resource={"service.name": "claims", "agentops.team": "claims"})
    )
    response = client.post(
        "/v1/traces", content=body, headers={**agent, **PROTOBUF, "content-encoding": "gzip"}
    )
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/x-protobuf"

    # Workflows are classified once their spans stop arriving.
    clock.advance(31)
    container = container_of(client)
    assert client.portal.call(container.analytics.finalize_due) == 20  # type: ignore[union-attr]
    failed = client.get("/v1/workflows", params={"outcome": "failed"}, headers=viewer).json()
    assert len(failed["items"]) == 5
    diagnosis = client.get(f"/v1/workflows/{failed['items'][0]['trace_id']}", headers=viewer).json()
    assert diagnosis["finalized"] is True
    assert diagnosis["diagnosis"]["failure_class"] == "tool_error"
    assert diagnosis["diagnosis"]["root_cause_name"] == "crm.lookup"

    # Metrics and costs.
    ops = client.get("/v1/operations", params={"kind": "tool"}, headers=viewer).json()
    assert ops["rows"][0]["calls"] == 20
    assert ops["rows"][0]["errors"] == 5
    costs = client.get("/v1/costs", params={"group_by": "team"}, headers=finops).json()
    assert costs["rows"][0]["key"] == "claims"
    assert costs["total_cost_usd"] == pytest.approx(20 * (1000 * 2 + 200 * 8) / 1e6)
    metrics = client.get("/metrics").text
    assert (
        'agentops_operations_total{agent="claims-triage",kind="tool",name="crm.lookup"' in metrics
    )

    # SLO evaluation: 25% errors burn the budget 25x -> alerts fire.
    summary = client.post("/v1/slos:evaluate", headers=sre).json()
    assert summary["fired"] == 4
    status = client.get("/v1/slos/crm-availability/status", headers=viewer).json()
    assert status["health"] == "critical"
    alerts = client.get("/v1/alerts", headers=viewer).json()["items"]
    assert {a["severity"] for a in alerts} == {"page", "ticket"}
    history = client.get(
        "/v1/alerts/history", params={"slo_id": "crm-availability"}, headers=viewer
    ).json()
    assert len(history["items"]) == 4
    rules = client.get("/v1/slos/crm-availability/prometheus-rules", headers=viewer).json()
    assert rules["groups"][1]["rules"][0]["alert"] == "AgentSloErrorBudgetBurn"
    assert [s["id"] for s in client.get("/v1/slos", headers=viewer).json()["items"]] == [
        "crm-availability"
    ]
    assert client.get("/v1/slos/crm-availability", headers=viewer).json()["objective"] == 0.99

    # Recovery: recommendation, proposal, four-eyes approval, directive feed.
    rec = client.get(
        "/v1/recovery/recommendations",
        params={"kind": "tool", "name": "crm.lookup"},
        headers=viewer,
    ).json()
    assert rec["stats"]["failures"] == 5
    proposal = {
        "action": "circuit.open",
        "target": {"kind": "tool", "name": "crm.lookup"},
        "parameters": {"half_open_after_s": 60},
        "reason": "CRM returning 503 for 25% of calls",
    }
    created = client.post("/v1/recovery/actions", json=proposal, headers=sre)
    assert created.status_code == 201
    action = created.json()
    assert action["status"] == "pending_approval"
    url = f"/v1/recovery/actions/{action['id']}"
    assert client.post(f"{url}/approve", json={}, headers=sre).status_code == 403
    approved = client.post(f"{url}/approve", json={"comment": "confirmed"}, headers=sre_2).json()
    assert approved["status"] == "active"
    assert client.get(url, headers=viewer).json()["decided_by"] == "bob@corp.test"
    listed = client.get("/v1/recovery/actions", params={"status": "active"}, headers=viewer)
    assert [a["id"] for a in listed.json()["items"]] == [action["id"]]
    directives = client.get("/v1/recovery/directives", headers=agent).json()
    assert [d["action"] for d in directives["items"]] == ["circuit.open"]

    second = client.post(
        "/v1/recovery/actions",
        json={**proposal, "target": {"kind": "tool", "name": "kb.search"}},
        headers=sre,
    ).json()
    rejected = client.post(
        f"/v1/recovery/actions/{second['id']}/reject", json={}, headers=sre_2
    ).json()
    assert rejected["status"] == "rejected"

    assert client.delete("/v1/slos/crm-availability", headers=sre).status_code == 204
    assert client.get("/v1/slos/crm-availability", headers=viewer).status_code == 404


def test_otlp_json_and_partial_success(client: TestClient, agent_idp: FakeIdp) -> None:
    root = agent_span("run", NOW_NS - 10**9)
    future = OtlpSpan(name="late", start_ns=NOW_NS + 7200 * 10**9)
    response = client.post(
        "/v1/traces",
        content=json_body([root, future]),
        headers={**agent_headers(agent_idp), "content-type": "application/json"},
    )
    assert response.status_code == 200
    assert response.json() == {
        "partialSuccess": {"rejectedSpans": "1", "errorMessage": "timestamp in the future"}
    }
    unsupported = client.post(
        "/v1/traces",
        content=b"x",
        headers={**agent_headers(agent_idp), "content-type": "text/plain"},
    )
    assert unsupported.status_code == 415
    assert unsupported.json()["error"] == "unsupported_media_type"


def test_operator_authentication_and_errors(client: TestClient, operator_idp: FakeIdp) -> None:
    anonymous = client.get("/v1/slos")
    assert anonymous.status_code == 401
    assert anonymous.headers["www-authenticate"] == "Bearer"
    assert anonymous.headers["x-content-type-options"] == "nosniff"
    viewer = operator_headers(operator_idp, "agentops-viewer")
    forbidden = client.put("/v1/slos/crm-availability", json=SLO, headers=viewer)
    assert forbidden.status_code == 403
    mismatch = client.put(
        "/v1/slos/other-slo", json=SLO, headers=operator_headers(operator_idp, "agentops-sre")
    )
    assert mismatch.status_code == 400
    invalid = client.put(
        "/v1/slos/crm-availability",
        json={**SLO, "objective": 2},
        headers=operator_headers(operator_idp, "agentops-sre"),
    )
    assert invalid.status_code == 422
    assert invalid.json()["error"] == "invalid_request"
    traced = client.get("/healthz", headers={"x-correlation-id": "abc-123"})
    assert traced.headers["x-correlation-id"] == "abc-123"
    assert client.get("/readyz").json() == {"status": "ready"}


def mcp(client: TestClient, headers: dict[str, str], method: str, params: Any) -> Any:
    response = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
        headers={
            **headers,
            "accept": "application/json, text/event-stream",
            "content-type": "application/json",
        },
    )
    assert response.status_code == 200, response.text
    return response.json()["result"]


def test_mcp_tools_over_streamable_http(
    client: TestClient, agent_idp: FakeIdp, operator_idp: FakeIdp, clock: FakeClock
) -> None:
    agent = agent_headers(agent_idp)
    sre = operator_headers(operator_idp, "agentops-sre")
    client.put("/v1/slos/crm-availability", json=SLO, headers=sre)
    spans = failing_traces(3, failures=1)
    client.post("/v1/traces", content=protobuf_body(spans), headers={**agent, **PROTOBUF})
    clock.advance(31)
    client.portal.call(container_of(client).analytics.finalize_due)  # type: ignore[union-attr]

    tools = {t["name"]: t for t in mcp(client, agent, "tools/list", {})["tools"]}
    assert set(tools) == {
        "slo_status",
        "firing_alerts",
        "cost_report",
        "workflow_diagnosis",
        "recovery_recommendation",
        "propose_recovery_action",
    }
    assert all("outputSchema" in t for t in tools.values())

    def call(name: str, arguments: dict[str, Any], headers: dict[str, str] = agent) -> Any:
        return mcp(client, headers, "tools/call", {"name": name, "arguments": arguments})

    status = call("slo_status", {"slo_id": "crm-availability"})
    assert status["isError"] is False
    assert status["structuredContent"]["total_events"] == 3
    assert call("firing_alerts", {})["structuredContent"] == {"alerts": []}
    assert call("cost_report", {"group_by": "agent"})["structuredContent"]["group_by"] == "agent"
    trace = spans[0].trace_id
    diag = call("workflow_diagnosis", {"trace_id": trace})["structuredContent"]
    assert diag["diagnosis"]["outcome"] == "failed"
    rec = call("recovery_recommendation", {"kind": "tool", "name": "crm.lookup"})
    assert rec["structuredContent"]["stats"]["calls"] == 3
    proposed = call(
        "propose_recovery_action",
        {
            "action": "retry_policy.apply",
            "target_kind": "tool",
            "target_name": "crm.lookup",
            "reason": "transient 503s recover on retry",
            "parameters": {"max_attempts": 3},
        },
    )["structuredContent"]
    assert proposed["status"] == "active"
    assert proposed["proposed_by"].endswith("otel-collector")

    missing = call("slo_status", {"slo_id": "no-such-slo"})
    assert missing["isError"] is True
    assert "not_found" in missing["content"][0]["text"]
    denied = call("slo_status", {"slo_id": "crm-availability"}, headers={})
    assert denied["isError"] is True
    assert "unauthorized" in denied["content"][0]["text"]


def a2a(client: TestClient, headers: dict[str, str], parts: list[dict[str, Any]]) -> Any:
    return client.post(
        "/a2a",
        json={
            "jsonrpc": "2.0",
            "id": "req-1",
            "method": "message/send",
            "params": {
                "message": {
                    "role": "user",
                    "kind": "message",
                    "messageId": "m-1",
                    "contextId": "ctx-1",
                    "parts": parts,
                }
            },
        },
        headers=headers,
    )


def test_a2a_agent_card_and_skills(
    client: TestClient, agent_idp: FakeIdp, operator_idp: FakeIdp, clock: FakeClock
) -> None:
    card = client.get("/.well-known/agent-card.json").json()
    assert card["url"] == "https://agentops.corp.test/a2a"
    assert card["securitySchemes"]["bearer"]["scheme"] == "bearer"
    assert {s["id"] for s in card["skills"]} == {
        "health-summary",
        "slo-status",
        "cost-report",
        "workflow-diagnosis",
        "recovery-recommendation",
    }
    for field in ("name", "description", "version", "capabilities", "protocolVersion"):
        assert field in card

    agent = agent_headers(agent_idp)
    client.put(
        "/v1/slos/crm-availability",
        json=SLO,
        headers=operator_headers(operator_idp, "agentops-sre"),
    )
    spans = failing_traces(2, failures=1)
    client.post("/v1/traces", content=protobuf_body(spans), headers={**agent, **PROTOBUF})
    clock.advance(31)
    client.portal.call(container_of(client).analytics.finalize_due)  # type: ignore[union-attr]

    def send(parts: list[dict[str, Any]]) -> dict[str, Any]:
        response = a2a(client, agent, parts)
        assert response.status_code == 200
        result: dict[str, Any] = response.json()["result"]
        return result

    health = send([{"kind": "text", "text": "How are the agents doing?"}])
    assert health["status"]["state"] == "completed"
    assert health["contextId"] == "ctx-1"
    assert health["metadata"]["skill"] == "health-summary"
    status = send([{"kind": "data", "data": {"skill": "slo-status", "slo_id": "crm-availability"}}])
    assert status["artifacts"][0]["parts"][0]["data"]["total_events"] == 2
    assert "crm-availability" in status["artifacts"][0]["parts"][1]["text"]
    costs = send([{"kind": "text", "text": "What did we spend on tokens?"}])
    assert costs["metadata"]["skill"] == "cost-report"
    workflow = send([{"kind": "text", "text": f"Why did {spans[0].trace_id} fail?"}])
    assert "Root cause: tool_error" in workflow["artifacts"][0]["parts"][1]["text"]
    rec = send(
        [{"kind": "data", "data": {"skill": "recovery-recommendation", "name": "crm.lookup"}}]
    )
    assert "circuit closed" in rec["artifacts"][0]["parts"][1]["text"]

    failed = send([{"kind": "data", "data": {"skill": "slo-status", "slo_id": "missing"}}])
    assert failed["status"]["state"] == "failed"
    for bad in (
        {"skill": "slo-status"},
        {"skill": "cost-report", "group_by": "planet"},
        {"skill": "cost-report", "hours": 0},
        {"skill": "workflow-diagnosis", "trace_id": "xyz"},
        {"skill": "recovery-recommendation", "kind": "tool"},
    ):
        assert send([{"kind": "data", "data": bad}])["status"]["state"] == "failed"


def test_a2a_protocol_errors(client: TestClient, agent_idp: FakeIdp) -> None:
    agent = agent_headers(agent_idp)
    unknown = a2a(client, agent, [{"kind": "data", "data": {"skill": "launch-rockets"}}]).json()
    assert unknown["error"]["code"] == -32602
    empty = a2a(client, agent, [{"kind": "file", "file": {}}]).json()
    assert empty["error"]["code"] == -32005
    wrong_method = client.post(
        "/a2a", json={"jsonrpc": "2.0", "id": 9, "method": "tasks/cancel"}, headers=agent
    ).json()
    assert wrong_method == {
        "jsonrpc": "2.0",
        "id": 9,
        "error": {"code": -32601, "message": "method 'tasks/cancel' not supported"},
    }
    not_rpc = client.post("/a2a", json={"id": 1}, headers=agent).json()
    assert not_rpc["error"]["code"] == -32600
    bad_params = client.post(
        "/a2a",
        json={"jsonrpc": "2.0", "id": 2, "method": "message/send", "params": {"message": {}}},
        headers=agent,
    ).json()
    assert bad_params["error"]["code"] == -32602
    anonymous = a2a(client, {}, [{"kind": "text", "text": "hello"}])
    assert anonymous.status_code == 401
    narrow = agent_headers(agent_idp, scope="agentops:slo.read")
    forbidden = a2a(client, narrow, [{"kind": "text", "text": "cost please"}])
    assert forbidden.status_code == 403
