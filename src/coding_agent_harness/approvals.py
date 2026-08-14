"""Approval broker with fingerprint-bound minimal authorization."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum
from typing import Protocol, runtime_checkable

from coding_agent_harness.canonical import approval_fingerprint
from coding_agent_harness.domain import ActionProposal, ActionSource, NormalizedAction
from coding_agent_harness.policy import PolicyResult


class ApprovalDecision(str, Enum):
    """Typed outcomes an approver can return."""

    APPROVE_ONCE = "approve_once"
    REJECT = "reject"
    EDIT_AND_EXECUTE = "edit_and_execute"


class ApprovalRequest:
    """Immutable request presented to an approver."""

    __slots__ = (
        "action",
        "expires_at",
        "fingerprint",
        "fingerprint_version",
        "policy_result",
        "request_id",
        "requested_at",
    )

    action: NormalizedAction
    expires_at: datetime
    fingerprint: str
    fingerprint_version: int
    policy_result: PolicyResult
    request_id: str
    requested_at: datetime

    def __init__(
        self,
        *,
        request_id: str,
        action: NormalizedAction,
        policy_result: PolicyResult,
        fingerprint_version: int,
        fingerprint: str,
        requested_at: datetime,
        expires_at: datetime,
    ) -> None:
        object.__setattr__(self, "request_id", request_id)
        object.__setattr__(self, "action", action)
        object.__setattr__(self, "policy_result", policy_result)
        object.__setattr__(self, "fingerprint_version", fingerprint_version)
        object.__setattr__(self, "fingerprint", fingerprint)
        object.__setattr__(self, "requested_at", requested_at)
        object.__setattr__(self, "expires_at", expires_at)

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("ApprovalRequest is immutable")

    def __delattr__(self, name: str) -> None:
        raise AttributeError("ApprovalRequest is immutable")


class ApprovalOutcome:
    """Immutable result of an approval decision."""

    __slots__ = (
        "actor_meta",
        "decided_at",
        "decision",
        "fingerprint",
        "reason",
        "replacement_action_id",
        "replacement_proposal",
        "wait_duration_seconds",
    )

    actor_meta: dict[str, str]
    decided_at: datetime
    decision: ApprovalDecision
    fingerprint: str
    reason: str
    replacement_action_id: str | None
    replacement_proposal: ActionProposal | None
    wait_duration_seconds: float | None

    def __init__(
        self,
        *,
        decision: ApprovalDecision,
        fingerprint: str,
        decided_at: datetime,
        actor_meta: dict[str, str] | None = None,
        reason: str = "",
        replacement_action_id: str | None = None,
        replacement_proposal: ActionProposal | None = None,
        wait_duration_seconds: float | None = None,
    ) -> None:
        object.__setattr__(self, "decision", decision)
        object.__setattr__(self, "fingerprint", fingerprint)
        object.__setattr__(self, "decided_at", decided_at)
        object.__setattr__(self, "actor_meta", dict(actor_meta) if actor_meta else {})
        object.__setattr__(self, "reason", reason)
        object.__setattr__(self, "replacement_action_id", replacement_action_id)
        object.__setattr__(self, "replacement_proposal", replacement_proposal)
        object.__setattr__(self, "wait_duration_seconds", wait_duration_seconds)

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("ApprovalOutcome is immutable")

    def __delattr__(self, name: str) -> None:
        raise AttributeError("ApprovalOutcome is immutable")


@runtime_checkable
class ApprovalPort(Protocol):
    """Protocol for collecting human approval decisions."""

    async def decide(self, request: ApprovalRequest) -> ApprovalOutcome: ...


class ApprovalScript:
    """Deterministic script for testing approval flows."""

    def __init__(
        self,
        *,
        decisions: list[ApprovalDecision],
        edited_args: dict[str, object] | None = None,
    ) -> None:
        self._decisions = list(decisions)
        self._edited_args = edited_args or {}

    def next_decision(self) -> ApprovalDecision | None:
        if not self._decisions:
            return None
        return self._decisions.pop(0)

    @property
    def edited_args(self) -> dict[str, object]:
        return dict(self._edited_args)


class ScriptedApprover:
    """Approver that replays a deterministic script."""

    def __init__(self, script: ApprovalScript) -> None:
        self._script = script

    async def decide(self, request: ApprovalRequest) -> ApprovalOutcome:
        decision = self._script.next_decision()
        if decision is None:
            return ApprovalOutcome(
                decision=ApprovalDecision.REJECT,
                fingerprint=request.fingerprint,
                decided_at=request.requested_at,
                reason="approval script exhausted",
            )

        if decision is ApprovalDecision.EDIT_AND_EXECUTE:
            new_id = f"edit-{request.action.action_id}-{id(self._script)}"
            proposal = ActionProposal(
                action_id=new_id,
                source=ActionSource.HUMAN_EDIT,
                parent_action_id=request.action.action_id,
                type=request.action.type,
                raw_args=self._script.edited_args,
                workspace_id=request.action.workspace_id,
                round=request.action.round,
            )
            return ApprovalOutcome(
                decision=ApprovalDecision.EDIT_AND_EXECUTE,
                fingerprint=request.fingerprint,
                decided_at=request.requested_at,
                reason="human edited action parameters",
                replacement_action_id=new_id,
                replacement_proposal=proposal,
            )

        return ApprovalOutcome(
            decision=decision,
            fingerprint=request.fingerprint,
            decided_at=request.requested_at,
            reason="approved" if decision is ApprovalDecision.APPROVE_ONCE else "rejected",
        )


def _now_utc() -> datetime:
    return datetime.now(tz=UTC)


class ApprovalBroker:
    """Broker that validates fingerprints and delegates to an approver."""

    def __init__(
        self,
        *,
        approver: ApprovalPort,
        clock: typing_Callable[[], datetime] | None = None,
    ) -> None:
        self._approver = approver
        self._clock = clock or (lambda: datetime.now(tz=UTC))

    async def decide(self, request: ApprovalRequest) -> ApprovalOutcome:
        now = self._clock()
        if now >= request.expires_at:
            wait = (now - request.requested_at).total_seconds()
            return ApprovalOutcome(
                decision=ApprovalDecision.REJECT,
                fingerprint=request.fingerprint,
                decided_at=now,
                reason="approval request expired (timeout)",
                wait_duration_seconds=max(0.0, wait),
            )

        actual_fingerprint = approval_fingerprint(
            request.action, version=request.fingerprint_version
        )
        if actual_fingerprint != request.fingerprint:
            return ApprovalOutcome(
                decision=ApprovalDecision.REJECT,
                fingerprint=request.fingerprint,
                decided_at=now,
                reason="fingerprint mismatch: approval not bound to this action",
                wait_duration_seconds=(now - request.requested_at).total_seconds(),
            )

        outcome = await self._approver.decide(request)

        if (
            outcome.decision is ApprovalDecision.APPROVE_ONCE
            and outcome.fingerprint != request.fingerprint
        ):
                return ApprovalOutcome(
                    decision=ApprovalDecision.REJECT,
                    fingerprint=request.fingerprint,
                    decided_at=now,
                    reason="fingerprint mismatch: approval not bound to this action",
                    wait_duration_seconds=(now - request.requested_at).total_seconds(),
                )

        return outcome


from collections.abc import Callable as typing_Callable
