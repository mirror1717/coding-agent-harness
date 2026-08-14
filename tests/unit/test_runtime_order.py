"""Tests for AgentRuntime execution order and governance chain."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from coding_agent_harness.approvals import (
    ApprovalBroker,
    ApprovalDecision,
    ApprovalOutcome,
    ApprovalRequest,
    ApprovalScript,
    ScriptedApprover,
)
from coding_agent_harness.artifacts import RunArtifactStore
from coding_agent_harness.audit import AuditLog
from coding_agent_harness.budgets import BudgetController, BudgetLimits
from coding_agent_harness.domain import (
    ActionProposal,
    ActionSource,
    ModelDecision,
    NormalizedAction,
    RawExecutionResult,
    RuntimeState,
    StructuredFeedback,
    TaskRequest,
    TerminalState,
)
from coding_agent_harness.feedback import FeedbackEngine
from coding_agent_harness.guardrail import EmptySecretDetector, Guardrail, GuardrailDecision, GuardrailReason
from coding_agent_harness.llm import MockLLM
from coding_agent_harness.memory import MemoryStore
from coding_agent_harness.policy import PolicyDecision, PolicyEngine, PolicyResult, RiskLevel
from coding_agent_harness.runtime import AgentRuntime, RuntimeConfig
from coding_agent_harness.termination import TerminalCandidate
from coding_agent_harness.tools import ToolDispatcher, ToolRegistry
from coding_agent_harness.verification import (
    AcceptanceCheck,
    CheckOutcome,
    VerificationProfile,
)


def _make_task() -> TaskRequest:
    return TaskRequest(
        task_id="task-1",
        prompt="fix the test",
        workspace_id="ws-1",
        policy_version="1",
        verification_profile_id="vp-1",
    )


def _make_action_proposal(
    *,
    action_type: str = "shell",
    raw_args: dict | None = None,
    action_id: str = "act-1",
) -> ActionProposal:
    if raw_args is None:
        raw_args = {"argv": ["echo", "hello"], "cwd": "."}
    return ActionProposal(
        action_id=action_id,
        source=ActionSource.MODEL,
        type=action_type,
        raw_args=raw_args,
        workspace_id="ws-1",
        round=1,
    )


def _make_verification_profile() -> VerificationProfile:
    return VerificationProfile(
        profile_id="vp-1",
        name="default",
        finish_condition="pytest passes",
        checks=(
            AcceptanceCheck(
                check_id="pytest",
                action_template=ActionProposal(
                    action_id="",
                    source=ActionSource.VERIFICATION,
                    type="pytest",
                    raw_args={"argv": ["pytest", "-q"], "cwd": "."},
                    workspace_id="ws-1",
                    round=0,
                ),
                required=True,
            ),
        ),
    )


def _make_budget_limits() -> BudgetLimits:
    return BudgetLimits(
        wall_clock_seconds=300.0,
        sandbox_execution_seconds=120.0,
        approval_timeout_seconds=30.0,
        hitl_wait_seconds=60.0,
        max_rounds=10,
        max_llm_calls=20,
        max_tool_calls=20,
    )


class _FakeClock:
    def __init__(self) -> None:
        self._t = 0.0

    def monotonic(self) -> float:
        self._t += 0.1
        return self._t

    def now_utc(self) -> datetime:
        return datetime(2026, 1, 1, tzinfo=UTC)


class _FakeSandbox:
    def __init__(self, exit_code: int = 0, stdout: bytes = b"ok\n") -> None:
        self._exit_code = exit_code
        self._stdout = stdout

    async def execute(self, action: NormalizedAction) -> RawExecutionResult:
        return RawExecutionResult(
            execution_id="exec-1",
            action_id=action.action_id,
            exit_code=self._exit_code,
            stdout_artifact=self._stdout.decode("utf-8", errors="replace"),
            stderr_artifact="",
            report_artifacts=(),
            duration_seconds=0.1,
            outcome="success" if self._exit_code == 0 else "failure",
            sandbox_meta={},
        )

    async def cancel(self, execution_id: str) -> None:
        pass


def _setup_runtime(
    *,
    llm: MockLLM,
    workspace_path: Path,
    sandbox: _FakeSandbox | None = None,
    policy_engine: PolicyEngine | None = None,
    approval_broker: ApprovalBroker | None = None,
) -> AgentRuntime:
    sandbox = sandbox or _FakeSandbox()
    registry = ToolRegistry()
    guardrail = Guardrail(secret_detector=EmptySecretDetector(), credentials_present=False, workspace_id="ws-1")
    policy = policy_engine or PolicyEngine.from_file(Path(__file__).parents[2] / "config" / "default-policy.yaml")
    dispatcher = ToolDispatcher({"shell": sandbox, "pytest": sandbox})
    artifact_store = RunArtifactStore(workspace_path, run_id="run-1")
    feedback = FeedbackEngine()
    memory = MemoryStore(
        workspace_path / "memory.db",
        clock=lambda: datetime(2026, 1, 1, tzinfo=UTC),
        id_factory=lambda: "mem-1",
        secret_detector=EmptySecretDetector(),
    )
    audit = AuditLog(workspace_path / "audit.jsonl", run_id="run-1")
    budget = BudgetController(
        limits=_make_budget_limits(),
        clock=_FakeClock(),
        run_id="run-1",
    )
    vp = _make_verification_profile()
    config = RuntimeConfig(
        workspace_path=workspace_path,
        workspace_id="ws-1",
        run_id="run-1",
    )
    broker = approval_broker or ApprovalBroker(
        approver=ScriptedApprover(ApprovalScript(decisions=[ApprovalDecision.APPROVE_ONCE])),
        clock=lambda: datetime(2026, 1, 1, second=5, tzinfo=UTC),
    )
    return AgentRuntime(
        llm=llm,
        registry=registry,
        guardrail=guardrail,
        policy_engine=policy,
        approval_broker=broker,
        dispatcher=dispatcher,
        artifact_store=artifact_store,
        feedback_engine=feedback,
        memory_store=memory,
        audit_log=audit,
        budget_controller=budget,
        verification_profile=vp,
        config=config,
    )


class TestRuntimeOrder:
    @pytest.mark.asyncio
    async def test_allow_action_uses_exact_order(self, tmp_path: Path) -> None:
        llm = MockLLM([
            ModelDecision(
                kind="action",
                action=_make_action_proposal(
                    raw_args={"argv": ["echo", "hi"], "cwd": "."},
                ),
            ),
            ModelDecision(kind="finish"),
        ])
        runtime = _setup_runtime(llm=llm, workspace_path=tmp_path)
        result = await runtime.run(_make_task())
        assert result.terminal_state is not None

    @pytest.mark.asyncio
    async def test_finish_triggers_verification(self, tmp_path: Path) -> None:
        llm = MockLLM([ModelDecision(kind="finish")])
        runtime = _setup_runtime(
            llm=llm,
            workspace_path=tmp_path,
            sandbox=_FakeSandbox(exit_code=0),
        )
        result = await runtime.run(_make_task())
        assert result.terminal_state is TerminalState.SUCCESS

    @pytest.mark.asyncio
    async def test_verification_failure_does_not_succeed(self, tmp_path: Path) -> None:
        llm = MockLLM([
            ModelDecision(kind="finish"),
            ModelDecision(kind="finish"),
        ])
        runtime = _setup_runtime(
            llm=llm,
            workspace_path=tmp_path,
            sandbox=_FakeSandbox(exit_code=1, stdout=b"FAILED test\n"),
        )
        result = await runtime.run(_make_task())
        assert result.terminal_state is not TerminalState.SUCCESS

    @pytest.mark.asyncio
    async def test_budget_exhaustion_terminates(self, tmp_path: Path) -> None:
        decisions = [ModelDecision(kind="message", message="waiting")] * 100
        llm = MockLLM(decisions)
        runtime = _setup_runtime(llm=llm, workspace_path=tmp_path)
        config = RuntimeConfig(
            workspace_path=tmp_path,
            workspace_id="ws-1",
            run_id="run-1",
            max_rounds=2,
        )
        runtime._config = config
        result = await runtime.run(_make_task())
        assert result.terminal_state is TerminalState.BUDGET_EXHAUSTED


class TestRuntimeFeedback:
    @pytest.mark.asyncio
    async def test_feedback_changes_next_action(self, tmp_path: Path) -> None:
        llm = MockLLM([
            ModelDecision(
                kind="action",
                action=_make_action_proposal(
                    raw_args={"argv": ["pytest", "-q"], "cwd": "."},
                    action_id="act-1",
                ),
            ),
            ModelDecision(
                kind="action",
                action=_make_action_proposal(
                    raw_args={"argv": ["echo", "fixed"], "cwd": "."},
                    action_id="act-2",
                ),
            ),
            ModelDecision(kind="finish"),
        ])
        runtime = _setup_runtime(
            llm=llm,
            workspace_path=tmp_path,
            sandbox=_FakeSandbox(exit_code=1, stdout=b"FAILED test\n"),
        )
        result = await runtime.run(_make_task())
        assert result.terminal_state is not None
