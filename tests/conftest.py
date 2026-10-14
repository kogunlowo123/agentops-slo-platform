"""Shared fixtures: in-process identity providers, a platform wired to SQLite, and an app."""

from __future__ import annotations

import os
import time
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi.testclient import TestClient

from agentops_slo_platform.api.app import create_app
from agentops_slo_platform.config import Settings
from agentops_slo_platform.runtime.container import Container, build_container
from agentops_slo_platform.security.jwks import StaticJwks

ROOT = Path(__file__).resolve().parents[1]
OPERATOR_ISSUER = "https://login.corp.test/tenant"
AGENT_ISSUER = "https://identity.corp.test"
# A fixed, realistic "now": 2026-10-14T12:00:00Z.
NOW_NS = 1_792_065_600 * 1_000_000_000

ALL_AGENT_SCOPES = (
    "agentops:traces.ingest agentops:slo.read agentops:cost.read agentops:workflow.read "
    "agentops:recovery.read agentops:recovery.propose agentops:directives.read"
)


@dataclass
class FakeIdp:
    """Minimal OIDC issuer: ES256 keypair plus JWKS."""

    issuer: str
    kid: str = "k1"
    key: ec.EllipticCurvePrivateKey = field(
        default_factory=lambda: ec.generate_private_key(ec.SECP256R1())
    )

    def jwks(self) -> dict[str, Any]:
        jwk = jwt.algorithms.ECAlgorithm.to_jwk(self.key.public_key(), as_dict=True)
        jwk.update({"kid": self.kid, "alg": "ES256", "use": "sig"})
        return {"keys": [jwk]}

    def mint(self, *, sub: str, aud: str, ttl: int = 300, **extra: Any) -> str:
        now = int(time.time())
        claims = {"iss": self.issuer, "sub": sub, "aud": aud, "iat": now, "exp": now + ttl}
        claims.update(extra)
        return jwt.encode(claims, self.key, algorithm="ES256", headers={"kid": self.kid})


@dataclass
class FakeClock:
    current: int = NOW_NS

    def now_ns(self) -> int:
        return self.current

    def advance(self, seconds: float) -> None:
        self.current += int(seconds * 1_000_000_000)


@pytest.fixture
def operator_idp() -> FakeIdp:
    return FakeIdp(OPERATOR_ISSUER, kid="op-1")


@pytest.fixture
def agent_idp() -> FakeIdp:
    return FakeIdp(AGENT_ISSUER, kid="ag-1")


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


def make_settings(tmp_path: Path, **overrides: Any) -> Settings:
    db = os.environ.get("AGENTOPS_TEST_DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'a.db'}")
    values: dict[str, Any] = {
        "environment": "test",
        "public_url": "https://agentops.corp.test",
        "database_url": db,
        "dev_ephemeral_keys": True,
        "audit_log_path": tmp_path / "audit.jsonl",
        "operator_issuer": OPERATOR_ISSUER,
        "operator_audience": "agentops",
        "operator_jwks_url": "https://login.corp.test/jwks",
        "agent_issuer": AGENT_ISSUER,
        "agent_audience": "agentops",
        "agent_jwks_url": "https://identity.corp.test/.well-known/jwks.json",
        "access_policy_file": ROOT / "policies" / "agentops-access.yaml",
        "recovery_policy_file": ROOT / "policies" / "recovery-actions.yaml",
        "decision_cache_ttl_s": 0,
    }
    values.update(overrides)
    return Settings(**values)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return make_settings(tmp_path)


@pytest.fixture
async def container(
    settings: Settings, operator_idp: FakeIdp, agent_idp: FakeIdp, clock: FakeClock
) -> AsyncIterator[Container]:
    built = await build_container(
        settings,
        operator_resolver=StaticJwks(operator_idp.jwks()),
        agent_resolver=StaticJwks(agent_idp.jwks()),
        clock=clock,
    )
    yield built
    await built.aclose()


def agent_token(
    idp: FakeIdp,
    *,
    sub: str = "spiffe://agents.corp.test/agent/observability/otel-collector",
    scope: str = ALL_AGENT_SCOPES,
    **extra: Any,
) -> str:
    """A credential shaped like a fabric-issued one (risk tier and classification ceiling)."""
    extra.setdefault("risk", 2)
    extra.setdefault("max_cls", 2)
    return idp.mint(sub=sub, aud="agentops", scope=scope, **extra)


def agent_headers(idp: FakeIdp, **kw: Any) -> dict[str, str]:
    return {"Authorization": f"Bearer {agent_token(idp, **kw)}"}


def operator_headers(idp: FakeIdp, *roles: str, sub: str = "alice@corp.test") -> dict[str, str]:
    token = idp.mint(sub=sub, aud="agentops", roles=list(roles))
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def client(
    settings: Settings, operator_idp: FakeIdp, agent_idp: FakeIdp, clock: FakeClock
) -> Iterator[TestClient]:
    """App whose container is built inside the app's own event loop by its lifespan."""

    async def factory() -> Container:
        return await build_container(
            settings,
            operator_resolver=StaticJwks(operator_idp.jwks()),
            agent_resolver=StaticJwks(agent_idp.jwks()),
            clock=clock,
        )

    with TestClient(create_app(factory, background=False)) as test_client:
        yield test_client
