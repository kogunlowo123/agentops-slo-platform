"""Runtime settings, read from AGENTOPS_* environment variables."""

from __future__ import annotations

from pathlib import Path

from pydantic import Field, HttpUrl, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AGENTOPS_", env_nested_delimiter="__")

    environment: str = "production"
    public_url: HttpUrl = Field(description="External URL of this service (A2A agent card url)")
    database_url: str = Field(description="SQLAlchemy async URL, e.g. postgresql+asyncpg://...")

    # Signed audit chain (agentic-core AuditLog).
    audit_log_path: Path = Path("/var/lib/agentops/audit/audit.jsonl")
    audit_key_file: Path | None = Field(default=None, description="Ed25519 PEM audit signing key")
    dev_ephemeral_keys: bool = Field(
        default=False,
        description="Generate an in-memory audit key at start-up. Refused in production.",
    )

    # Human operators (enterprise OIDC).
    operator_issuer: str
    operator_audience: str
    operator_jwks_url: str
    operator_roles_claim: str = "roles"

    # Agent callers: identity fabric, or local JWKS verification + local policy.
    fabric_url: str | None = Field(default=None, description="agent-identity-fabric base URL")
    fabric_agent_id: str | None = Field(
        default=None, description="This platform's SPIFFE ID registered in the fabric"
    )
    workload_token_file: Path | None = Field(
        default=None, description="Projected service-account token used for jwt-bearer"
    )
    agent_issuer: str | None = None
    agent_audience: str | None = None
    agent_jwks_url: str | None = None
    access_policy_file: Path = Path("policies/agentops-access.yaml")
    decision_cache_ttl_s: float = Field(default=15.0, ge=0, le=300)

    recovery_policy_file: Path = Path("policies/recovery-actions.yaml")
    slo_seed_file: Path | None = None

    # Alert delivery.
    alertmanager_url: str | None = None

    # Ingestion limits.
    max_body_bytes: int = Field(default=8 * 1024 * 1024, ge=1024, le=64 * 1024 * 1024)
    max_inflated_bytes: int = Field(default=32 * 1024 * 1024, ge=1024, le=256 * 1024 * 1024)
    max_spans_per_request: int = Field(default=10_000, ge=1, le=100_000)
    max_future_skew_s: int = Field(default=600, ge=0, le=86_400)
    metric_label_limit: int = Field(default=500, ge=10, le=10_000)

    # Processing.
    retention_days: int = Field(default=35, ge=2, le=400)
    workflow_quiet_s: int = Field(default=30, ge=1, le=3600)
    workflow_max_wait_s: int = Field(default=900, ge=10, le=86_400)
    loop_threshold: int = Field(default=25, ge=2, le=10_000)
    evaluation_interval_s: float = Field(default=60.0, ge=1, le=3600)
    finalize_interval_s: float = Field(default=10.0, ge=1, le=3600)
    purge_interval_s: float = Field(default=3600.0, ge=60, le=86_400)

    mcp_allowed_hosts: list[str] = Field(
        default_factory=list,
        description="Host headers accepted by /mcp (DNS-rebinding protection); empty disables",
    )
    log_level: str = "INFO"
    otlp_endpoint: str | None = None

    @model_validator(mode="after")
    def _consistent(self) -> Settings:
        if self.dev_ephemeral_keys:
            if self.environment == "production":
                raise ValueError("dev_ephemeral_keys cannot be used in production")
        elif self.audit_key_file is None:
            raise ValueError("AGENTOPS_AUDIT_KEY_FILE is required unless dev_ephemeral_keys")
        local = (self.agent_issuer, self.agent_audience, self.agent_jwks_url)
        if self.fabric_url is None and not all(local):
            raise ValueError(
                "agent authorization needs AGENTOPS_FABRIC_URL or all of "
                "AGENTOPS_AGENT_ISSUER, AGENTOPS_AGENT_AUDIENCE, AGENTOPS_AGENT_JWKS_URL"
            )
        if (self.workload_token_file is None) != (self.fabric_agent_id is None):
            raise ValueError(
                "AGENTOPS_FABRIC_AGENT_ID and AGENTOPS_WORKLOAD_TOKEN_FILE go together"
            )
        if self.fabric_agent_id is not None and self.fabric_url is None:
            raise ValueError("AGENTOPS_FABRIC_AGENT_ID requires AGENTOPS_FABRIC_URL")
        if self.workflow_max_wait_s <= self.workflow_quiet_s:
            raise ValueError("workflow_max_wait_s must exceed workflow_quiet_s")
        return self

    @property
    def public_url_str(self) -> str:
        return str(self.public_url).rstrip("/")
