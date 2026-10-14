"""Adversarial ingestion tests mapped to the threat model (docs/security/threat-model.md)."""

from __future__ import annotations

import base64
import gzip
import json

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi.testclient import TestClient

from agentops_slo_platform.domain.spans import SpanKind
from agentops_slo_platform.runtime.container import Container
from tests.conftest import AGENT_ISSUER, NOW_NS, FakeClock, FakeIdp, agent_headers, agent_token
from tests.factories import OtlpSpan, agent_span, model_attrs, protobuf_body, tool_attrs

pytestmark = pytest.mark.security

PROTOBUF = {"content-type": "application/x-protobuf"}


def container_of(client: TestClient) -> Container:
    value: Container = client.app.state.container  # type: ignore[attr-defined]
    return value


def export(client: TestClient, headers: dict[str, str], body: bytes, **extra: str) -> int:
    return client.post(
        "/v1/traces", content=body, headers={**PROTOBUF, **headers, **extra}
    ).status_code


def body() -> bytes:
    return protobuf_body([agent_span("run", NOW_NS - 10**9)])


def test_s1_unauthenticated_export_rejected(client: TestClient) -> None:
    response = client.post("/v1/traces", content=body(), headers=PROTOBUF)
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert export(client, {"Authorization": "Basic dXNlcjpwYXNz"}, body()) == 401


def test_s2_token_without_ingest_scope_forbidden(client: TestClient, agent_idp: FakeIdp) -> None:
    reader = agent_headers(agent_idp, scope="agentops:slo.read agentops:cost.read")
    assert export(client, reader, body()) == 403


def test_s3_forged_token_rejected(client: TestClient, agent_idp: FakeIdp) -> None:
    attacker = FakeIdp(AGENT_ISSUER, kid=agent_idp.kid)  # same issuer and kid, different key
    assert export(client, agent_headers(attacker), body()) == 401
    other_audience = agent_idp.mint(sub="x", aud="another-service", scope="agentops:traces.ingest")
    assert export(client, {"Authorization": f"Bearer {other_audience}"}, body()) == 401
    expired = agent_token(agent_idp, ttl=-120)
    assert export(client, {"Authorization": f"Bearer {expired}"}, body()) == 401


def test_s4_alg_none_rejected(client: TestClient, agent_idp: FakeIdp) -> None:
    claims = jwt.decode(agent_token(agent_idp), options={"verify_signature": False})

    def b64(obj: object) -> str:
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()

    unsigned = f"{b64({'alg': 'none', 'kid': agent_idp.kid})}.{b64(claims)}."
    assert export(client, {"Authorization": f"Bearer {unsigned}"}, body()) == 401
    hs256 = jwt.encode(
        claims,
        "guessable-shared-secret-of-32-bytes!",
        algorithm="HS256",
        headers={"kid": agent_idp.kid},
    )
    assert export(client, {"Authorization": f"Bearer {hs256}"}, body()) == 401


def test_s5_gzip_bomb_rejected(client: TestClient, agent_idp: FakeIdp) -> None:
    bomb = gzip.compress(b"\0" * (40 * 1024 * 1024))  # inflates past the 32 MiB limit
    assert len(bomb) < 100_000
    status = export(client, agent_headers(agent_idp), bomb, **{"content-encoding": "gzip"})
    assert status == 413


def test_s6_oversized_body_rejected(client: TestClient, agent_idp: FakeIdp) -> None:
    oversized = b"\0" * (8 * 1024 * 1024 + 1)
    assert export(client, agent_headers(agent_idp), oversized) == 413

    # Without a trustworthy Content-Length the streamed size is enforced as well.
    def chunks():  # type: ignore[no-untyped-def]
        for _ in range(9):
            yield b"\0" * (1024 * 1024)

    response = client.post(
        "/v1/traces", content=chunks(), headers={**PROTOBUF, **agent_headers(agent_idp)}
    )
    assert response.status_code == 413


def test_s7_secrets_and_pii_scrubbed_from_error_text(
    client: TestClient, agent_idp: FakeIdp
) -> None:
    span = OtlpSpan(
        name="tool",
        start_ns=NOW_NS - 10**9,
        attributes=tool_attrs("payments.charge"),
        error=True,
        status_message=(
            "card 4111 1111 1111 1111 declined; ssn 123-45-6789; "
            "token AKIAABCDEFGHIJKLMNOP; contact jane.doe@corp.test"
        ),
        exception=("PaymentError", "Authorization: Bearer ghp_" + "a" * 36),
    )
    assert export(client, agent_headers(agent_idp), protobuf_body([span])) == 200
    (stored,) = client.portal.call(container_of(client).span_store.trace, span.trace_id)  # type: ignore[union-attr]
    for leaked in ("4111", "123-45-6789", "AKIA", "jane.doe"):
        assert leaked not in (stored.error_message or "")
    redactions = container_of(client).metrics.registry.get_sample_value("agentops_redactions_total")
    assert redactions is not None
    assert redactions >= 4


def test_s8_prompt_content_attributes_never_stored(client: TestClient, agent_idp: FakeIdp) -> None:
    secret_prompt = "Patient MRN 8812734 reports chest pain"
    root = agent_span("run", NOW_NS - 10**9)
    llm = root.child(
        "chat",
        attributes=model_attrs(
            **{
                "gen_ai.input.messages": secret_prompt,
                "gen_ai.output.messages": secret_prompt,
                "gen_ai.prompt": secret_prompt,
                "gen_ai.system_instructions": secret_prompt,
            }
        ),
    )
    assert export(client, agent_headers(agent_idp), protobuf_body([root, llm])) == 200
    container = container_of(client)
    stored = client.portal.call(container.span_store.trace, root.trace_id)  # type: ignore[union-attr]
    assert all("8812734" not in fact.model_dump_json() for fact in stored)
    database = container.settings.database_url.removeprefix("sqlite+aiosqlite:///")
    if container.settings.database_url.startswith("sqlite"):
        with open(database, "rb") as handle:
            assert b"8812734" not in handle.read()


def test_s9_label_cardinality_flood_is_capped(client: TestClient, agent_idp: FakeIdp) -> None:
    container = container_of(client)
    container.metrics.limiter._limit = 20
    flood = [
        OtlpSpan(name="t", start_ns=NOW_NS - 10**9, attributes=tool_attrs(f"random-tool-{i}"))
        for i in range(200)
    ]
    assert export(client, agent_headers(agent_idp), protobuf_body(flood)) == 200
    exposition = client.get("/metrics").text
    names = {
        line.split('name="')[1].split('"')[0]
        for line in exposition.splitlines()
        if line.startswith("agentops_operations_total{")
    }
    assert len(names) <= 21
    assert "other" in names
    assert 'agentops_label_overflow{label="name"}' in exposition


def test_s10_future_timestamps_cannot_poison_windows(
    client: TestClient, agent_idp: FakeIdp, clock: FakeClock
) -> None:
    future = [
        OtlpSpan(
            name="t",
            start_ns=NOW_NS + 3 * 3600 * 10**9,
            attributes=tool_attrs("crm.lookup"),
            error=True,
        )
        for _ in range(50)
    ]
    response = client.post(
        "/v1/traces",
        content=protobuf_body(future),
        headers={**PROTOBUF, **agent_headers(agent_idp)},
    )
    assert response.status_code == 200
    container = container_of(client)
    clock.advance(4 * 3600)  # even once "now" catches up, nothing was stored
    rows = client.portal.call(  # type: ignore[union-attr]
        lambda: container.span_store.calls(
            SpanKind.TOOL, "crm.lookup", since_ns=0, until_ns=clock.current
        )
    )
    assert rows == []


def test_s11_duplicate_export_not_double_counted(client: TestClient, agent_idp: FakeIdp) -> None:
    payload = protobuf_body(
        [agent_span("run", NOW_NS - 10**9, attributes={"gen_ai.agent.name": "a"})]
    )
    for _ in range(3):
        assert export(client, agent_headers(agent_idp), payload) == 200
    exposition = client.get("/metrics").text
    assert 'agentops_spans_ingested_total{kind="agent"} 1.0' in exposition
    assert "agentops_spans_duplicate_total 2.0" in exposition


def test_s12_spoofed_agent_names_keep_ingest_provenance(
    client: TestClient, agent_idp: FakeIdp
) -> None:
    """A caller can claim any gen_ai.agent.name, but provenance records who sent it."""
    impostor = "spiffe://agents.corp.test/agent/sandbox/impostor"
    root = agent_span("run", NOW_NS - 10**9, agent="payments-agent")
    assert export(client, agent_headers(agent_idp, sub=impostor), protobuf_body([root])) == 200
    (stored,) = client.portal.call(container_of(client).span_store.trace, root.trace_id)  # type: ignore[union-attr]
    assert stored.agent == "payments-agent"
    assert stored.ingested_by == impostor


def test_s13_key_confusion_between_issuers(client: TestClient, operator_idp: FakeIdp) -> None:
    """An operator (human) token is not an agent credential."""
    token = operator_idp.mint(sub="alice@corp.test", aud="agentops", scope="agentops:traces.ingest")
    assert export(client, {"Authorization": f"Bearer {token}"}, body()) == 401
    rsa_like = ec.generate_private_key(ec.SECP384R1())
    es384 = jwt.encode({"sub": "x"}, rsa_like, algorithm="ES384", headers={"kid": "ag-1"})
    assert export(client, {"Authorization": f"Bearer {es384}"}, body()) == 401
