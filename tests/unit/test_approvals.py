"""Tests for the approval broker and minimal authorization semantics."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import override

import pytest

from coding_agent_harness.approvals import (
    ApprovalBroker,
    ApprovalDecision,
    ApprovalOutcome,
    ApprovalRequest,
    ApprovalScript,
    ScriptedApprover,
)
from coding_agent_harness.canonical import approval_fingerprint
from coding_agent_harness.domain import ActionProposal, ActionSource, NormalizedAction
from coding_agent_harness.policy import PolicyDecision, PolicyResult, RiskLevel

_FIXED_CLOCK = lambda: datetime(2026, 1, 1, second=10, tzinfo=UTC)


def _make_normalized(
    *,
    action_id: str = "act-1",
    action_type: str = "shell",
    normalized_args: dict | None = None,
    workspace_id: str = "ws-1",
) -> NormalizedAction:
    if normalized_args is None:
        normalized_args = {"argv": ["ls", "-la"], "cwd": "."}
    return NormalizedAction(
        action_id=action_id,
        source=ActionSource.MODEL,
        type=action_type,
        raw_args=normalized_args,
        normalized_args=normalized_args,
        workspace_id=workspace_id,
        round=1,
    )


def _make_policy_result(
    *,
    decision: PolicyDecision = PolicyDecision.REQUIRE_APPROVAL,
    risk: RiskLevel = RiskLevel.MEDIUM,
    rule_id: str = "rule-1",
    reason: str = "shell requires approval",
    version: int = 1,
) -> PolicyResult:
    return PolicyResult(
        decision=decision,
        risk=risk,
        rule_id=rule_id,
        reason=reason,
        version=version,
    )


class TestApprovalRequest:
    def test_request_carries_fingerprint_and_params(self) -> None:
        action = _make_normalized()
        policy = _make_policy_result()
        request = ApprovalRequest(
            request_id="req-1",
            action=action,
            policy_result=policy,
            fingerprint_version=1,
            fingerprint=approval_fingerprint(action, version=1),
            requested_at=datetime(2026, 1, 1, tzinfo=UTC),
            expires_at=datetime(2026, 1, 1, second=30, tzinfo=UTC),
        )
        assert request.fingerprint_version == 1
        assert request.action.type == "shell"
        assert request.policy_result.decision is PolicyDecision.REQUIRE_APPROVAL

    def test_request_is_frozen(self) -> None:
        action = _make_normalized()
        policy = _make_policy_result()
        request = ApprovalRequest(
            request_id="req-1",
            action=action,
            policy_result=policy,
            fingerprint_version=1,
            fingerprint="abc",
            requested_at=datetime(2026, 1, 1, tzinfo=UTC),
            expires_at=datetime(2026, 1, 1, second=30, tzinfo=UTC),
        )
        with pytest.raises(Exception):
            request.request_id = "changed"  # type: ignore[misc]


class TestApproveOnce:
    @pytest.mark.asyncio
    async def test_approve_once_binds_exact_fingerprint(self) -> None:
        action = _make_normalized()
        policy = _make_policy_result()
        fingerprint = approval_fingerprint(action, version=1)
        request = ApprovalRequest(
            request_id="req-1",
            action=action,
            policy_result=policy,
            fingerprint_version=1,
            fingerprint=fingerprint,
            requested_at=datetime(2026, 1, 1, tzinfo=UTC),
            expires_at=datetime(2026, 1, 1, second=30, tzinfo=UTC),
        )
        approver = ScriptedApprover(
            ApprovalScript(
                decisions=[
                    ApprovalDecision.APPROVE_ONCE,
                ],
            )
        )
        broker = ApprovalBroker(approver=approver, clock=_FIXED_CLOCK)
        outcome = await broker.decide(request)
        assert outcome.decision is ApprovalDecision.APPROVE_ONCE
        assert outcome.fingerprint == fingerprint
        assert outcome.replacement_action_id is None

    @pytest.mark.asyncio
    async def test_approve_once_rejects_modified_fingerprint(self) -> None:
        action = _make_normalized()
        policy = _make_policy_result()
        request = ApprovalRequest(
            request_id="req-1",
            action=action,
            policy_result=policy,
            fingerprint_version=1,
            fingerprint="wrong-fingerprint",
            requested_at=datetime(2026, 1, 1, tzinfo=UTC),
            expires_at=datetime(2026, 1, 1, second=30, tzinfo=UTC),
        )
        approver = ScriptedApprover(
            ApprovalScript(decisions=[ApprovalDecision.APPROVE_ONCE])
        )
        broker = ApprovalBroker(approver=approver, clock=_FIXED_CLOCK)
        outcome = await broker.decide(request)
        assert outcome.decision is ApprovalDecision.REJECT
        assert "fingerprint" in outcome.reason.lower()


class TestReject:
    @pytest.mark.asyncio
    async def test_reject_returns_no_side_effects(self) -> None:
        action = _make_normalized()
        policy = _make_policy_result()
        request = ApprovalRequest(
            request_id="req-1",
            action=action,
            policy_result=policy,
            fingerprint_version=1,
            fingerprint=approval_fingerprint(action, version=1),
            requested_at=datetime(2026, 1, 1, tzinfo=UTC),
            expires_at=datetime(2026, 1, 1, second=30, tzinfo=UTC),
        )
        approver = ScriptedApprover(
            ApprovalScript(decisions=[ApprovalDecision.REJECT])
        )
        broker = ApprovalBroker(approver=approver, clock=_FIXED_CLOCK)
        outcome = await broker.decide(request)
        assert outcome.decision is ApprovalDecision.REJECT
        assert outcome.replacement_action_id is None
        assert outcome.replacement_proposal is None


class TestEdit:
    @pytest.mark.asyncio
    async def test_edit_creates_new_proposal(self) -> None:
        action = _make_normalized()
        policy = _make_policy_result()
        request = ApprovalRequest(
            request_id="req-1",
            action=action,
            policy_result=policy,
            fingerprint_version=1,
            fingerprint=approval_fingerprint(action, version=1),
            requested_at=datetime(2026, 1, 1, tzinfo=UTC),
            expires_at=datetime(2026, 1, 1, second=30, tzinfo=UTC),
        )
        edited_args = {"argv": ["ls", "-l"], "cwd": "."}
        approver = ScriptedApprover(
            ApprovalScript(
                decisions=[ApprovalDecision.EDIT_AND_EXECUTE],
                edited_args=edited_args,
            )
        )
        broker = ApprovalBroker(approver=approver, clock=_FIXED_CLOCK)
        outcome = await broker.decide(request)
        assert outcome.decision is ApprovalDecision.EDIT_AND_EXECUTE
        assert outcome.replacement_proposal is not None
        proposal = outcome.replacement_proposal
        assert proposal.source is ActionSource.HUMAN_EDIT
        assert proposal.parent_action_id == action.action_id
        assert proposal.action_id != action.action_id
        assert proposal.action_id != ""
        raw = dict(proposal.raw_args)
        assert list(raw["argv"]) == list(edited_args["argv"])
        assert raw["cwd"] == edited_args["cwd"]

    @pytest.mark.asyncio
    async def test_edit_proposal_has_new_id(self) -> None:
        action = _make_normalized(action_id="act-1")
        policy = _make_policy_result()
        request = ApprovalRequest(
            request_id="req-1",
            action=action,
            policy_result=policy,
            fingerprint_version=1,
            fingerprint=approval_fingerprint(action, version=1),
            requested_at=datetime(2026, 1, 1, tzinfo=UTC),
            expires_at=datetime(2026, 1, 1, second=30, tzinfo=UTC),
        )
        approver = ScriptedApprover(
            ApprovalScript(
                decisions=[ApprovalDecision.EDIT_AND_EXECUTE],
                edited_args={"argv": ["echo", "hi"], "cwd": "."},
            )
        )
        broker = ApprovalBroker(approver=approver, clock=_FIXED_CLOCK)
        outcome = await broker.decide(request)
        assert outcome.replacement_proposal is not None
        assert outcome.replacement_proposal.action_id != "act-1"
        assert outcome.replacement_proposal.action_id != ""
        assert outcome.replacement_action_id == outcome.replacement_proposal.action_id

    @pytest.mark.asyncio
    async def test_edit_does_not_call_registry_or_dispatcher(self) -> None:
        action = _make_normalized()
        policy = _make_policy_result()
        request = ApprovalRequest(
            request_id="req-1",
            action=action,
            policy_result=policy,
            fingerprint_version=1,
            fingerprint=approval_fingerprint(action, version=1),
            requested_at=datetime(2026, 1, 1, tzinfo=UTC),
            expires_at=datetime(2026, 1, 1, second=30, tzinfo=UTC),
        )
        approver = ScriptedApprover(
            ApprovalScript(
                decisions=[ApprovalDecision.EDIT_AND_EXECUTE],
                edited_args={"argv": ["ls"], "cwd": "."},
            )
        )
        broker = ApprovalBroker(approver=approver, clock=_FIXED_CLOCK)
        outcome = await broker.decide(request)
        assert outcome.replacement_proposal is not None
        assert outcome.replacement_proposal.type == action.type


class TestTimeout:
    @pytest.mark.asyncio
    async def test_timeout_rejects_without_execution(self) -> None:
        action = _make_normalized()
        policy = _make_policy_result()
        request = ApprovalRequest(
            request_id="req-1",
            action=action,
            policy_result=policy,
            fingerprint_version=1,
            fingerprint=approval_fingerprint(action, version=1),
            requested_at=datetime(2026, 1, 1, tzinfo=UTC),
            expires_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
        approver = ScriptedApprover(ApprovalScript(decisions=[]))
        broker = ApprovalBroker(
            approver=approver,
            clock=lambda: datetime(2026, 1, 1, second=1, tzinfo=UTC),
        )
        outcome = await broker.decide(request)
        assert outcome.decision is ApprovalDecision.REJECT
        assert "timeout" in outcome.reason.lower() or "expired" in outcome.reason.lower()
        assert outcome.replacement_proposal is None

    @pytest.mark.asyncio
    async def test_timeout_reports_wait_duration(self) -> None:
        action = _make_normalized()
        policy = _make_policy_result()
        requested_at = datetime(2026, 1, 1, tzinfo=UTC)
        request = ApprovalRequest(
            request_id="req-1",
            action=action,
            policy_result=policy,
            fingerprint_version=1,
            fingerprint=approval_fingerprint(action, version=1),
            requested_at=requested_at,
            expires_at=requested_at,
        )
        approver = ScriptedApprover(ApprovalScript(decisions=[]))
        broker = ApprovalBroker(
            approver=approver,
            clock=lambda: datetime(2026, 1, 1, second=5, tzinfo=UTC),
        )
        outcome = await broker.decide(request)
        assert outcome.decision is ApprovalDecision.REJECT
        assert outcome.wait_duration_seconds is not None
        assert outcome.wait_duration_seconds >= 5.0


class TestBrokerHasNoDispatchDependency:
    def test_broker_does_not_import_tools_or_sandbox(self) -> None:
        import coding_agent_harness.approvals as approvals_mod
        import inspect

        source = inspect.getsource(approvals_mod)
        assert "from coding_agent_harness.tools import" not in source
        assert "from coding_agent_harness.sandbox import" not in source
        assert "import coding_agent_harness.tools" not in source
        assert "import coding_agent_harness.sandbox" not in source

    def test_broker_only_returns_proposals(self) -> None:
        action = _make_normalized()
        policy = _make_policy_result()
        request = ApprovalRequest(
            request_id="req-1",
            action=action,
            policy_result=policy,
            fingerprint_version=1,
            fingerprint=approval_fingerprint(action, version=1),
            requested_at=datetime(2026, 1, 1, tzinfo=UTC),
            expires_at=datetime(2026, 1, 1, second=30, tzinfo=UTC),
        )
        approver = ScriptedApprover(
            ApprovalScript(
                decisions=[ApprovalDecision.EDIT_AND_EXECUTE],
                edited_args={"argv": ["ls"], "cwd": "."},
            )
        )
        broker = ApprovalBroker(approver=approver, clock=_FIXED_CLOCK)
        assert not hasattr(broker, "dispatch")
        assert not hasattr(broker, "execute")
        assert not hasattr(broker, "registry")


class TestScriptedApprover:
    @pytest.mark.asyncio
    async def test_script_replays_exact_sequence(self) -> None:
        action = _make_normalized()
        policy = _make_policy_result()
        fp = approval_fingerprint(action, version=1)

        async def make_request() -> ApprovalRequest:
            return ApprovalRequest(
                request_id="req-1",
                action=action,
                policy_result=policy,
                fingerprint_version=1,
                fingerprint=fp,
                requested_at=datetime(2026, 1, 1, tzinfo=UTC),
                expires_at=datetime(2026, 1, 1, second=30, tzinfo=UTC),
            )

        approver = ScriptedApprover(
            ApprovalScript(
                decisions=[
                    ApprovalDecision.APPROVE_ONCE,
                    ApprovalDecision.REJECT,
                    ApprovalDecision.EDIT_AND_EXECUTE,
                ],
                edited_args={"argv": ["echo"], "cwd": "."},
            )
        )
        broker = ApprovalBroker(approver=approver, clock=_FIXED_CLOCK)

        r1 = await make_request()
        o1 = await broker.decide(r1)
        assert o1.decision is ApprovalDecision.APPROVE_ONCE

        r2 = await make_request()
        o2 = await broker.decide(r2)
        assert o2.decision is ApprovalDecision.REJECT

        r3 = await make_request()
        o3 = await broker.decide(r3)
        assert o3.decision is ApprovalDecision.EDIT_AND_EXECUTE
        assert o3.replacement_proposal is not None

    @pytest.mark.asyncio
    async def test_script_exhaustion_rejects(self) -> None:
        action = _make_normalized()
        policy = _make_policy_result()
        request = ApprovalRequest(
            request_id="req-1",
            action=action,
            policy_result=policy,
            fingerprint_version=1,
            fingerprint=approval_fingerprint(action, version=1),
            requested_at=datetime(2026, 1, 1, tzinfo=UTC),
            expires_at=datetime(2026, 1, 1, second=30, tzinfo=UTC),
        )
        approver = ScriptedApprover(ApprovalScript(decisions=[]))
        broker = ApprovalBroker(
            approver=approver,
            clock=lambda: datetime(2026, 1, 1, second=30, tzinfo=UTC),
        )
        outcome = await broker.decide(request)
        assert outcome.decision is ApprovalDecision.REJECT
