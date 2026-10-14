"""HTTP request and response bodies not already defined in the domain."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from agentops_slo_platform.application.slo_service import AlertEventView, FiringAlert
from agentops_slo_platform.domain.costs import PriceEntry
from agentops_slo_platform.domain.recovery import RecoveryAction
from agentops_slo_platform.domain.slo import SloDefinition
from agentops_slo_platform.domain.workflows import Diagnosis


class SloList(BaseModel):
    items: list[SloDefinition]


class FiringAlertList(BaseModel):
    items: list[FiringAlert]


class AlertHistory(BaseModel):
    items: list[AlertEventView]


class PriceList(BaseModel):
    items: list[PriceEntry]


class WorkflowList(BaseModel):
    items: list[Diagnosis]


class RecoveryActionList(BaseModel):
    items: list[RecoveryAction]


class Directives(BaseModel):
    items: list[RecoveryAction]
    generated_at_ns: int


class Decision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    comment: str = Field(default="", max_length=1000)


class OtlpPartialSuccess(BaseModel):
    """OTLP ExportTraceServiceResponse in JSON form (protobuf for protobuf requests)."""

    model_config = ConfigDict(populate_by_name=True)

    partial_success: dict[str, str | int] | None = Field(default=None, alias="partialSuccess")


class OAuthError(BaseModel):
    error: str
    error_description: str
