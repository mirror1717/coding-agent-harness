"""AgentRuntime: the sole flow coordinator for the governed harness."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from coding_agent_harness.approvals import (
    ApprovalBroker,
    ApprovalDecision,
    ApprovalOutcome,
    ApprovalRequest,
)
from coding_agent_harness.artifacts import RunArtifactStore
from coding_agent_harness.audit import AuditLog
from coding_agent_harness.budgets import BudgetController
from coding_agent_harness.canonical import approval_fingerprint
from coding_agent_harness.domain import (
    ActionProposal,
    ModelDecision,
    NormalizedAction,
    RunResult,
    RuntimeState,
    StructuredFeedback,
    TaskRequest,
    TerminalState,
)
from coding_agent_harness.errors import (
    HarnessError,
    HarnessSecurityError,
    HarnessValidationError,
)
from coding_agent_harness.feedback import FeedbackEngine
from coding_agent_harness.guardrail import Guardrail, GuardrailDecision
from coding_agent_harness.llm import LLM, AgentContext, BudgetSummary
from coding_agent_harness.memory import MemoryStore
from coding_agent_harness.policy import PolicyDecision, PolicyEngine, PolicyResult
from coding_agent_harness.termination import TerminalCandidate, choose_terminal
from coding_agent_harness.tools import ToolDispatcher, ToolRegistry
from coding_agent_harness.verification import (
    CheckOutcome,
    CheckResult,
    VerificationProfile,
    create_verification_actions,
    evaluate_verification_results,
)


@dataclass
class RuntimeConfig:
    """Configuration for the AgentRuntime."""
    workspace_path: Path
    workspace_id: str
    run_id: str = ""
    max_rounds: int = 20
    max_llm_calls: int = 100
    max_tool_calls: int = 100


class AgentRuntime:
    """The sole flow coordinator. All components are injected; none call each other."""

    def __init__(
        self,
        *,
        llm: LLM,
        registry: ToolRegistry,
        guardrail: Guardrail,
        policy_engine: PolicyEngine,
        approval_broker: ApprovalBroker,
        dispatcher: ToolDispatcher,
        artifact_store: RunArtifactStore,
        feedback_engine: FeedbackEngine,
        memory_store: MemoryStore,
        audit_log: AuditLog,
        budget_controller: BudgetController,
        verification_profile: VerificationProfile,
        config: RuntimeConfig,
    ) -> None:
        self._llm = llm
        self._registry = registry
        self._guardrail = guardrail
        self._policy = policy_engine
        self._approval = approval_broker
        self._dispatcher = dispatcher
        self._artifacts = artifact_store
        self._feedback = feedback_engine
        self._memory = memory_store
        self._audit = audit_log
        self._budget = budget_controller
        self._verification = verification_profile
        self._config = config
        self._round = 0
        self._state = RuntimeState.PREPARING
        self._last_feedback: StructuredFeedback | None = None
        self._terminal: TerminalState | None = None
        self._terminal_reason: str = ""

    @property
    def state(self) -> RuntimeState:
        return self._state

    @property
    def terminal_state(self) -> TerminalState | None:
        return self._terminal

    async def run(self, task: TaskRequest) -> RunResult:
        """Run the main loop until a terminal state is reached."""
        self._state = RuntimeState.AWAITING_MODEL
        candidates: list[TerminalCandidate] = []

        while self._terminal is None:
            self._round += 1
            self._budget.record_round()
            snapshot = self._budget.snapshot()
            if snapshot.exhausted:
                candidates.append(TerminalCandidate(
                    state=TerminalState.BUDGET_EXHAUSTED,
                    reason=f"budgets exhausted: {snapshot.exhausted}",
                ))
                break

            if self._round > self._config.max_rounds:
                candidates.append(TerminalCandidate(
                    state=TerminalState.BUDGET_EXHAUSTED,
                    reason="max rounds exceeded",
                ))
                break

            try:
                decision = await self._call_llm(task, snapshot)
            except HarnessError as e:
                candidates.append(TerminalCandidate(
                    state=TerminalState.MODEL_UNAVAILABLE,
                    reason=f"LLM error: {e.code}",
                ))
                break

            if decision.kind == "finish":
                self._state = RuntimeState.VERIFYING
                success = await self._run_verification(task)
                if success:
                    candidates.append(TerminalCandidate(
                        state=TerminalState.SUCCESS,
                        reason="all required checks passed",
                        verified_success=True,
                    ))
                    break
                else:
                    self._state = RuntimeState.AWAITING_MODEL
                    continue
            elif decision.kind == "message":
                self._state = RuntimeState.AWAITING_MODEL
                continue
            else:
                action = decision.action
                if action is None:
                    continue
                outcome = await self._process_action(action, task)
                if outcome == "security_stop":
                    candidates.append(TerminalCandidate(
                        state=TerminalState.SECURITY_STOP,
                        reason="security boundary violation",
                    ))
                    break

        if not candidates:
            candidates.append(TerminalCandidate(
                state=TerminalState.BUDGET_EXHAUSTED,
                reason="loop exited without terminal state",
            ))

        winner = choose_terminal(candidates)
        self._terminal = winner.state
        self._terminal_reason = winner.reason
        self._state = RuntimeState.TERMINAL

        return RunResult(
            run_id=self._config.run_id,
            task_id=task.task_id,
            state=RuntimeState.TERMINAL,
            terminal_state=self._terminal,
            terminal_reason=self._terminal_reason,
        )

    async def _call_llm(self, task: TaskRequest, snapshot: Any) -> ModelDecision:
        self._budget.record_llm_call()
        self._state = RuntimeState.AWAITING_MODEL
        context = AgentContext(
            task=task,
            feedback=(self._last_feedback,) if self._last_feedback else (),
            budget=BudgetSummary(
                rounds_remaining=snapshot.rounds_remaining,
                llm_calls_remaining=snapshot.llm_calls_remaining,
                tool_calls_remaining=snapshot.tool_calls_remaining,
                wall_clock_remaining_seconds=snapshot.wall_clock_remaining_seconds,
                sandbox_execution_remaining_seconds=snapshot.sandbox_execution_remaining_seconds,
                hitl_wait_remaining_seconds=snapshot.hitl_wait_remaining_seconds,
                exhausted=snapshot.exhausted,
            ),
            round=self._round,
        )
        return await self._llm.decide(context)

    async def _process_action(
        self, proposal: ActionProposal, task: TaskRequest
    ) -> str:
        """Process one action through the full governance chain. Returns outcome."""
        self._state = RuntimeState.NORMALIZING

        try:
            normalized = self._registry.normalize(proposal, self._config.workspace_path)
        except HarnessValidationError as e:
            self._last_feedback = StructuredFeedback(
                action_id=proposal.action_id,
                category="validation",
                summary=f"validation error: {e.message}",
            )
            return "continue"

        self._state = RuntimeState.GOVERNING

        try:
            guardrail_result = self._guardrail.check(normalized)
        except HarnessSecurityError:
            return "security_stop"

        if guardrail_result.decision is not GuardrailDecision.PASS:
            self._last_feedback = StructuredFeedback(
                action_id=normalized.action_id,
                category="guardrail",
                summary=f"guardrail denied: {guardrail_result.reason.value}",
            )
            return "continue"

        policy_result = self._policy.evaluate(normalized)

        if policy_result.decision is PolicyDecision.DENY:
            self._last_feedback = StructuredFeedback(
                action_id=normalized.action_id,
                category="policy",
                summary=f"policy denied: {policy_result.reason}",
            )
            return "continue"

        if policy_result.decision is PolicyDecision.REQUIRE_APPROVAL:
            self._state = RuntimeState.AWAITING_APPROVAL
            outcome = await self._request_approval(normalized, policy_result)
            if outcome.decision is ApprovalDecision.REJECT:
                self._last_feedback = StructuredFeedback(
                    action_id=normalized.action_id,
                    category="approval",
                    summary=f"approval rejected: {outcome.reason}",
                )
                return "continue"
            if outcome.replacement_proposal is not None:
                return await self._process_action(outcome.replacement_proposal, task)

        self._state = RuntimeState.EXECUTING
        self._budget.record_tool_call()
        try:
            result = await self._dispatcher.dispatch(normalized)
        except HarnessSecurityError:
            return "security_stop"
        except HarnessError as e:
            self._last_feedback = StructuredFeedback(
                action_id=normalized.action_id,
                category="execution",
                summary=f"execution error: {e.code}",
            )
            return "continue"

        self._state = RuntimeState.PROCESSING_FEEDBACK
        stdout_bytes = b""
        stderr_bytes = b""
        if result.stdout_artifact:
            stdout_bytes = result.stdout_artifact.encode("utf-8")
        if result.stderr_artifact:
            stderr_bytes = result.stderr_artifact.encode("utf-8")

        feedback = self._feedback.parse(
            action_id=normalized.action_id,
            action_type=normalized.type,
            exit_code=result.exit_code,
            stdout=stdout_bytes,
            stderr=stderr_bytes,
        )
        self._last_feedback = feedback

        return "continue"

    async def _request_approval(
        self, action: NormalizedAction, policy_result: PolicyResult
    ) -> ApprovalOutcome:
        fingerprint = approval_fingerprint(action, version=1)
        now = datetime.now(tz=UTC)
        timeout = self._budget.approval_allowance_seconds()
        request = ApprovalRequest(
            request_id=f"req-{action.action_id}",
            action=action,
            policy_result=policy_result,
            fingerprint_version=1,
            fingerprint=fingerprint,
            requested_at=now,
            expires_at=datetime.fromtimestamp(
                now.timestamp() + max(timeout, 1), tz=UTC
            ),
        )
        outcome = await self._approval.decide(request)
        self._budget.record_approval_wait(
            (datetime.now(tz=UTC) - now).total_seconds()
        )
        return outcome

    async def _run_verification(self, task: TaskRequest) -> bool:
        """Run verification checks. Returns True if all required checks pass."""
        actions = create_verification_actions(
            self._verification,
            workspace_id=self._config.workspace_id,
            round=self._round,
        )

        results: list[CheckResult] = []
        for i, proposal in enumerate(actions):
            check = self._verification.checks[i]
            try:
                normalized = self._registry.normalize(
                    proposal, self._config.workspace_path
                )
                guardrail_result = self._guardrail.check(normalized)
                if guardrail_result.decision is not GuardrailDecision.PASS:
                    results.append(CheckResult(
                        check_id=check.check_id,
                        action_id=normalized.action_id,
                        required=check.required,
                        outcome=CheckOutcome.FAIL,
                    ))
                    continue

                policy_result = self._policy.evaluate(normalized)
                if policy_result.decision is PolicyDecision.DENY:
                    results.append(CheckResult(
                        check_id=check.check_id,
                        action_id=normalized.action_id,
                        required=check.required,
                        outcome=CheckOutcome.FAIL,
                    ))
                    continue

                self._budget.record_tool_call()
                exec_result = await self._dispatcher.dispatch(normalized)
                outcome = CheckOutcome.PASS if exec_result.exit_code == 0 else CheckOutcome.FAIL
                results.append(CheckResult(
                    check_id=check.check_id,
                    action_id=normalized.action_id,
                    required=check.required,
                    outcome=outcome,
                ))
            except HarnessError:
                results.append(CheckResult(
                    check_id=check.check_id,
                    action_id="",
                    required=check.required,
                    outcome=CheckOutcome.ERROR,
                ))

        verification = evaluate_verification_results(
            self._verification, results=tuple(results)
        )
        return verification.success_candidate
