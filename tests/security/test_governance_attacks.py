"""Adversarial governance tests: roles, four eyes, excessive agency, protocol auth, audit."""

from __future__ import annotations

from typing import Any

import pytest
from agentic_core.audit import verify_chain
from fastapi.testclient import TestClient

from agentops_slo_platform.runtime.container import Container
from tests.conftest import FakeIdp, agent_headers, operator_headers

pytestmark = pytest.mark.security

CIRCUIT = {
    "action": "circuit.open",
    "target": {"kind": "tool", "name": "crm.lookup"},
    "reason": "CRM failing for 30 minutes",
}


def container_of(client: TestClient) -> Container:
    value: Container = client.app.state.container  # type: ignore[attr-defined]
    return value


def mcp_call(client: TestClient, headers: dict[str, str], name: str, args: dict[str, Any]) -> Any:
    response = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": args},
        },
        headers={
            **headers,
            "accept": "application/json, text/event-stream",
            "content-type": "application/json",
        },
    )
    return response.json()["result"]


def test_g1_operator_without_role_forbidden(client: TestClient, operator_idp: FakeIdp) -> None:
    nobody = operator_headers(operator_idp)  # authenticated, no AgentOps role
    viewer = operator_headers(operator_idp, "agentops-viewer")
    finops = operator_headers(operator_idp, "agentops-finops")
    assert client.get("/v1/slos", headers=nobody).status_code == 403
    assert client.post("/v1/recovery/actions", json=CIRCUIT, headers=viewer).status_code == 403
    assert client.post("/v1/slos:evaluate", headers=finops).status_code == 403
    price = {"model": "m", "input_per_mtok": 0, "output_per_mtok": 0, "effective_from_ns": 0}
    assert client.put("/v1/pricing", json=price, headers=viewer).status_code == 403
    # Tokens from an untrusted issuer are rejected outright.
    stranger = FakeIdp(operator_idp.issuer, kid=operator_idp.kid)
    assert (
        client.get("/v1/slos", headers=operator_headers(stranger, "agentops-sre")).status_code
        == 401
    )


def test_g2_proposer_cannot_self_approve(client: TestClient, operator_idp: FakeIdp) -> None:
    alice = operator_headers(operator_idp, "agentops-sre", sub="alice@corp.test")
    action = client.post("/v1/recovery/actions", json=CIRCUIT, headers=alice).json()
    response = client.post(f"/v1/recovery/actions/{action['id']}/approve", json={}, headers=alice)
    assert response.status_code == 403
    assert "proposer" in response.json()["error_description"]


def test_g3_agent_cannot_pause_agents(client: TestClient, agent_idp: FakeIdp) -> None:
    result = mcp_call(
        client,
        agent_headers(agent_idp),
        "propose_recovery_action",
        {
            "action": "agent.pause",
            "target_kind": "agent",
            "target_name": "payments-agent",
            "reason": "it looks slow to me",
        },
    )
    assert result["structuredContent"]["status"] == "denied"
    assert result["structuredContent"]["matched_rules"] == ["deny-agent-pause-by-agents"]


def test_g4_high_risk_action_never_auto_applied(
    client: TestClient, operator_idp: FakeIdp, agent_idp: FakeIdp
) -> None:
    sre = operator_headers(operator_idp, "agentops-sre")
    for proposal in (
        CIRCUIT,
        {
            "action": "budget.raise",
            "target": {"kind": "agent", "name": "claims-triage"},
            "parameters": {"max_tokens_per_run": 2_000_000},
            "reason": "large claims batch",
        },
        {
            "action": "agent.pause",
            "target": {"kind": "agent", "name": "claims-triage"},
            "reason": "runaway loop",
        },
    ):
        created = client.post("/v1/recovery/actions", json=proposal, headers=sre).json()
        assert created["status"] == "pending_approval", proposal["action"]
    directives = client.get("/v1/recovery/directives", headers=agent_headers(agent_idp)).json()
    assert directives["items"] == []


def test_g5_mcp_tool_without_credentials_denied(client: TestClient, agent_idp: FakeIdp) -> None:
    anonymous = mcp_call(client, {}, "firing_alerts", {})
    assert anonymous["isError"] is True
    reader = agent_headers(agent_idp, scope="agentops:slo.read")
    proposing = mcp_call(
        client,
        reader,
        "propose_recovery_action",
        {
            "action": "retry_policy.apply",
            "target_kind": "tool",
            "target_name": "crm.lookup",
            "reason": "please let me",
        },
    )
    assert proposing["isError"] is True
    assert "forbidden" in proposing["content"][0]["text"]


def test_g6_a2a_without_credentials_unauthorized(client: TestClient) -> None:
    response = client.post(
        "/a2a",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "message/send",
            "params": {
                "message": {
                    "role": "user",
                    "messageId": "m",
                    "parts": [{"kind": "text", "text": "show costs"}],
                }
            },
        },
    )
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


def test_g7_audit_chain_verifies_after_operations(
    client: TestClient, operator_idp: FakeIdp, agent_idp: FakeIdp
) -> None:
    sre = operator_headers(operator_idp, "agentops-sre")
    sre_2 = operator_headers(operator_idp, "agentops-sre", sub="bob@corp.test")
    slo = {
        "id": "crm-availability",
        "name": "CRM lookups succeed",
        "sli": "availability",
        "objective": 0.99,
        "owner": "claims",
    }
    client.put("/v1/slos/crm-availability", json=slo, headers=sre)
    action = client.post("/v1/recovery/actions", json=CIRCUIT, headers=sre).json()
    client.post(f"/v1/recovery/actions/{action['id']}/approve", json={}, headers=sre_2)
    client.get("/v1/recovery/directives", headers=agent_headers(agent_idp))
    mcp_call(client, {}, "firing_alerts", {})

    container = container_of(client)
    log = container.audit_log

    async def events() -> list[Any]:
        return [e async for e in log._sink.read()]

    recorded = client.portal.call(events)  # type: ignore[union-attr]
    public = log._signer.public_key()
    assert verify_chain(recorded, {log._key_id: public}) == len(recorded)
    actions = [e.action for e in recorded]
    for expected in (
        "slo.create",
        "recovery.propose.circuit.open",
        "recovery.approve",
        "agentops:directives.read",
    ):
        assert expected in actions
    # The anonymous MCP probe is counted but does not write to the evidence chain.
    assert "agentops:slo.read" not in actions
    assert (
        container.metrics.registry.get_sample_value(
            "agentops_authorization_decisions_total",
            {"action": "agentops:slo.read", "decision": "missing_credentials"},
        )
        == 1.0
    )
    approve = next(e for e in recorded if e.action == "recovery.approve")
    assert approve.actor == "bob@corp.test"
    assert approve.correlation_id
    for event in recorded:  # credentials never enter the evidence trail
        assert "eyJ" not in event.model_dump_json()


def test_g8_delegated_agent_cannot_propose_recovery(client: TestClient, agent_idp: FakeIdp) -> None:
    delegated = agent_headers(agent_idp, act={"sub": "spiffe://agents.corp.test/agent/x/y"})
    result = mcp_call(
        client,
        delegated,
        "propose_recovery_action",
        {
            "action": "retry_policy.apply",
            "target_kind": "tool",
            "target_name": "crm.lookup",
            "reason": "acting for someone else",
        },
    )
    assert result["isError"] is True
    assert "forbidden" in result["content"][0]["text"]
