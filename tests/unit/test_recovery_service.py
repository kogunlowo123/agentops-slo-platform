"""Recovery recommendations, policy-gated proposals, four-eyes approval and directives."""

from __future__ import annotations

import pytest
from agentic_core.domain.classification import Decision

from agentops_slo_platform.application.recovery_service import Proposer
from agentops_slo_platform.domain.errors import (
    ConflictError,
    ForbiddenError,
    InvalidStateError,
    NotFoundError,
    PlatformError,
)
from agentops_slo_platform.domain.recovery import (
    ActionStatus,
    ActionTarget,
    ActionType,
    CircuitState,
    ProposeAction,
)
from agentops_slo_platform.domain.spans import SpanKind
from agentops_slo_platform.runtime.container import Container
from tests.conftest import NOW_NS, FakeClock
from tests.factories import fact

SRE = Proposer(id="alice@corp.test", kind="user", roles=frozenset({"agentops-sre"}))
SRE_2 = Proposer(id="bob@corp.test", kind="user", roles=frozenset({"agentops-sre"}))
AGENT = Proposer(id="spiffe://agents.corp.test/agent/claims/triage", kind="agent")


def proposal(action: ActionType, kind: SpanKind = SpanKind.TOOL, **kw: object) -> ProposeAction:
    return ProposeAction.model_validate(
        {
            "action": action,
            "target": ActionTarget(kind=kind, name=str(kw.pop("name", "crm.lookup"))),
            "reason": "crm.lookup failing since 10:05",
            **kw,
        }
    )


async def test_recommendation_from_observed_calls(container: Container) -> None:
    facts = [
        fact(start_ns=NOW_NS - 120 * 10**9, error=i % 2 == 0, http_status=503) for i in range(40)
    ]
    await container.span_store.add_new(facts, ingested_ns=NOW_NS)
    result = await container.recovery.recommend(SpanKind.TOOL, "crm.lookup")
    assert result.stats.calls == 40
    assert result.stats.failures == 20
    assert result.circuit.state is CircuitState.OPEN
    with pytest.raises(PlatformError, match="dependencies"):
        await container.recovery.recommend(SpanKind.AGENT, "claims-triage")


async def test_low_risk_retry_policy_from_agent_is_active(container: Container) -> None:
    action = await container.recovery.propose(
        proposal(ActionType.APPLY_RETRY_POLICY, parameters={"max_attempts": 3}), proposer=AGENT
    )
    assert action.status is ActionStatus.ACTIVE
    assert action.decision is Decision.ALLOW
    assert action.matched_rules == ("allow-agent-retry-policy",)
    assert action.parameters == {
        "max_attempts": 3,
        "initial_backoff_ms": 100,
        "max_backoff_ms": 10_000,
    }
    assert [d.id for d in await container.recovery.directives()] == [action.id]


async def test_aggressive_retries_need_approval(container: Container) -> None:
    action = await container.recovery.propose(
        proposal(ActionType.APPLY_RETRY_POLICY, parameters={"max_attempts": 5}), proposer=AGENT
    )
    assert action.status is ActionStatus.PENDING_APPROVAL
    assert action.matched_rules == ("approve-aggressive-retries",)


async def test_open_circuit_four_eyes(container: Container, clock: FakeClock) -> None:
    action = await container.recovery.propose(
        proposal(ActionType.OPEN_CIRCUIT, parameters={"half_open_after_s": 30}), proposer=SRE
    )
    assert action.status is ActionStatus.PENDING_APPROVAL
    assert await container.recovery.directives() == []
    with pytest.raises(ConflictError, match="already pending_approval"):
        await container.recovery.propose(proposal(ActionType.OPEN_CIRCUIT), proposer=SRE_2)
    with pytest.raises(ForbiddenError, match="proposer"):
        await container.recovery.decide(action.id, approve=True, approver=SRE.id)
    clock.advance(10)
    approved = await container.recovery.decide(
        action.id, approve=True, approver=SRE_2.id, comment="confirmed in dashboard"
    )
    assert approved.status is ActionStatus.ACTIVE
    assert approved.decided_by == SRE_2.id
    assert approved.expires_ns == clock.current + 60 * 60 * 10**9
    assert [d.id for d in await container.recovery.directives()] == [action.id]
    with pytest.raises(InvalidStateError):
        await container.recovery.decide(action.id, approve=False, approver=SRE_2.id)
    clock.advance(3601)
    assert await container.recovery.directives() == []
    assert (await container.recovery.get(action.id)).status is ActionStatus.EXPIRED
    expired = await container.recovery.find(status=ActionStatus.EXPIRED, limit=10)
    assert [a.id for a in expired] == [action.id]


async def test_reject_and_concurrent_decision(container: Container) -> None:
    action = await container.recovery.propose(
        proposal(
            ActionType.RAISE_BUDGET,
            kind=SpanKind.AGENT,
            name="claims-triage",
            parameters={"max_tokens_per_run": 200_000},
        ),
        proposer=SRE,
    )
    rejected = await container.recovery.decide(action.id, approve=False, approver=SRE_2.id)
    assert rejected.status is ActionStatus.REJECTED
    assert [a.id for a in await container.recovery.find(status=ActionStatus.REJECTED, limit=5)] == [
        action.id
    ]
    other = await container.recovery.propose(
        proposal(ActionType.OPEN_CIRCUIT, name="vector.search", kind=SpanKind.MEMORY),
        proposer=SRE,
    )
    stale = await container.recovery._actions.get(other.id)
    await container.recovery.decide(other.id, approve=True, approver=SRE_2.id)
    assert not await container.recovery._actions.replace(
        stale, expected=ActionStatus.PENDING_APPROVAL
    )


async def test_policy_denials_and_validation(container: Container) -> None:
    denied = await container.recovery.propose(
        proposal(ActionType.PAUSE_AGENT, kind=SpanKind.AGENT, name="claims-triage"),
        proposer=AGENT,
    )
    assert denied.status is ActionStatus.DENIED
    assert denied.matched_rules == ("deny-agent-pause-by-agents",)
    with pytest.raises(PlatformError, match="targets one of"):
        await container.recovery.propose(proposal(ActionType.PAUSE_AGENT), proposer=SRE)
    with pytest.raises(PlatformError, match="invalid parameters"):
        await container.recovery.propose(
            proposal(ActionType.APPLY_RETRY_POLICY, parameters={"max_attempts": 9}), proposer=SRE
        )
    with pytest.raises(PlatformError, match="invalid parameters"):
        await container.recovery.propose(
            proposal(ActionType.CLOSE_CIRCUIT, parameters={"x": 1}), proposer=SRE
        )


async def test_sre_closes_circuit_directly_agents_need_approval(container: Container) -> None:
    by_sre = await container.recovery.propose(proposal(ActionType.CLOSE_CIRCUIT), proposer=SRE)
    assert by_sre.status is ActionStatus.ACTIVE
    by_agent = await container.recovery.propose(
        proposal(ActionType.CLOSE_CIRCUIT, name="kb.search"), proposer=AGENT
    )
    assert by_agent.status is ActionStatus.PENDING_APPROVAL
    pending = await container.recovery.find(status=ActionStatus.PENDING_APPROVAL, limit=10)
    assert [a.id for a in pending] == [by_agent.id]
    assert len(await container.recovery.find(status=None, limit=10)) == 2


async def test_unknown_action(container: Container) -> None:
    with pytest.raises(NotFoundError):
        await container.recovery.get("0" * 32)
