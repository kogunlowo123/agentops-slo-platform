"""Typed client for agent-identity-fabric.

Endpoints (github.com/kogunlowo123/agent-identity-fabric):

- ``POST /oauth2/token`` with ``grant_type=urn:ietf:params:oauth:grant-type:jwt-bearer``
  (RFC 7523): the platform's workload token in, a short-lived agent credential out.
- ``POST /v1/authorize``: policy decision for the caller's credential, action and resource.
- ``POST /oauth2/introspect`` (RFC 7662): token status.

Transport failures and 5xx responses raise :class:`UnavailableError`, which
callers treat as deny (fail closed) and surface as 503 so clients retry.
"""

from __future__ import annotations

from typing import Any

import httpx
from agentic_core.domain.classification import DataClassification, Decision
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from agentops_slo_platform.domain.errors import (
    ForbiddenError,
    PlatformError,
    UnauthorizedError,
    UnavailableError,
)

JWT_BEARER = "urn:ietf:params:oauth:grant-type:jwt-bearer"


class AgentCredential(BaseModel):
    model_config = ConfigDict(frozen=True)

    access_token: str
    token_type: str
    expires_in: int = Field(gt=0)
    scope: str = ""


class FabricAuthorizeRequest(BaseModel):
    model_config = ConfigDict(frozen=True)

    action: str
    resource_type: str
    resource_id: str = "*"
    resource_classification: DataClassification = DataClassification.INTERNAL
    resource_attributes: dict[str, Any] = Field(default_factory=dict)
    environment: dict[str, Any] = Field(default_factory=dict)


class FabricDecision(BaseModel):
    model_config = ConfigDict(frozen=True)

    decision: Decision
    agent: str
    action: str
    resource: str
    matched_rules: tuple[str, ...] = ()
    policy_version: str
    policy_digest: str
    reason: str
    delegation_chain: tuple[str, ...] = ()


class Introspection(BaseModel):
    model_config = ConfigDict(frozen=True)

    active: bool
    sub: str | None = None
    scope: str | None = None
    exp: int | None = None
    agent_status: str | None = None


class FabricClient:
    def __init__(self, base_url: str, *, http: httpx.AsyncClient) -> None:
        self._base = base_url.rstrip("/")
        self._http = http

    async def _post(self, path: str, **kwargs: Any) -> httpx.Response:
        try:
            response = await self._http.post(f"{self._base}{path}", **kwargs)
        except httpx.HTTPError as exc:
            raise UnavailableError(f"identity fabric unreachable: {type(exc).__name__}") from exc
        if response.status_code >= 500:
            raise UnavailableError(f"identity fabric returned {response.status_code}")
        return response

    @staticmethod
    def _error(response: httpx.Response) -> str:
        try:
            body = response.json()
        except ValueError:
            return f"HTTP {response.status_code}"
        if isinstance(body, dict):
            return str(body.get("error_description") or body.get("error") or response.status_code)
        return f"HTTP {response.status_code}"

    @staticmethod
    def _parse[M: BaseModel](model: type[M], response: httpx.Response) -> M:
        try:
            return model.model_validate(response.json())
        except (ValueError, ValidationError) as exc:
            raise UnavailableError("identity fabric returned an unexpected response") from exc

    async def token_jwt_bearer(
        self, *, client_id: str, assertion: str, scope: str | None = None
    ) -> AgentCredential:
        data = {"grant_type": JWT_BEARER, "client_id": client_id, "assertion": assertion}
        if scope:
            data["scope"] = scope
        response = await self._post("/oauth2/token", data=data)
        if response.status_code != 200:
            raise UnauthorizedError(f"credential request rejected: {self._error(response)}")
        return self._parse(AgentCredential, response)

    async def authorize(self, token: str, request: FabricAuthorizeRequest) -> FabricDecision:
        response = await self._post(
            "/v1/authorize",
            json=request.model_dump(mode="json"),
            headers={"Authorization": f"Bearer {token}"},
        )
        if response.status_code == 401:
            raise UnauthorizedError(self._error(response))
        if response.status_code == 403:
            raise ForbiddenError(self._error(response))
        if response.status_code != 200:
            raise PlatformError(f"authorization request rejected: {self._error(response)}")
        return self._parse(FabricDecision, response)

    async def introspect(self, token: str) -> Introspection:
        response = await self._post("/oauth2/introspect", data={"token": token})
        if response.status_code != 200:
            raise UnavailableError(f"introspection failed: {self._error(response)}")
        return self._parse(Introspection, response)
