"""Property-based API contract test: no generated input may cause a 5xx."""

from __future__ import annotations

from typing import Any

import pytest
import schemathesis
from fastapi.testclient import TestClient
from hypothesis import HealthCheck, assume, settings

from tests.conftest import FakeIdp, agent_headers, operator_headers

pytestmark = pytest.mark.e2e


@pytest.fixture
def api_schema(client: TestClient) -> Any:
    return schemathesis.openapi.from_dict(client.get("/openapi.json").json())


schema = schemathesis.pytest.from_fixture("api_schema")


@schema.parametrize()
@settings(
    max_examples=25,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
)
def test_no_server_errors(case: Any, client: TestClient) -> None:
    # Header values must be transmittable over HTTP/1.1 at all.
    assume(all(str(v).isascii() for v in (case.headers or {}).values()))
    kwargs = case.as_transport_kwargs(base_url="")
    response = client.request(
        kwargs["method"],
        kwargs["url"],
        params=kwargs.get("params"),
        headers=kwargs.get("headers"),
        json=kwargs.get("json"),
        data=kwargs.get("data"),
        content=kwargs.get("content"),
    )
    assert response.status_code < 500, (case.method, case.path, response.text)


@schema.parametrize()
@settings(
    max_examples=25,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
)
def test_no_server_errors_with_credentials(
    case: Any, client: TestClient, operator_idp: FakeIdp, agent_idp: FakeIdp
) -> None:
    """Same property past authentication: operators with every role, agents with every scope."""
    assume(all(str(v).isascii() for v in (case.headers or {}).values()))
    kwargs = case.as_transport_kwargs(base_url="")
    path = str(kwargs["url"])
    credentials = (
        agent_headers(agent_idp)
        if path.startswith(("/v1/traces", "/v1/recovery/directives", "/a2a"))
        else operator_headers(operator_idp, "agentops-sre", "agentops-finops", "agentops-viewer")
    )
    response = client.request(
        kwargs["method"],
        kwargs["url"],
        params=kwargs.get("params"),
        headers={**(kwargs.get("headers") or {}), **credentials},
        json=kwargs.get("json"),
        data=kwargs.get("data"),
        content=kwargs.get("content"),
    )
    assert response.status_code < 500, (case.method, case.path, response.text)
