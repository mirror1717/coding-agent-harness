"""Core domain models for the Governed Coding Agent Harness.

All public Pydantic fact models use ConfigDict(extra="forbid", frozen=True).
"""
from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict


class ActionSource(str, Enum):
    """Source of an Action proposal."""
    MODEL = "MODEL"
    VERIFICATION = "VERIFICATION"
    HUMAN_EDIT = "HUMAN_EDIT"


class RuntimeState(str, Enum):
    """Agent runtime states."""
    INIT = "INIT"
    RUNNING = "RUNNING"
    WAITING_APPROVAL = "WAITING_APPROVAL"
    EXECUTING = "EXECUTING"
    FEEDBACK = "FEEDBACK"
    VERIFYING = "VERIFYING"
    TERMINATED = "TERMINATED"


class TerminalState(str, Enum):
    """Terminal states for a run. SECURITY_STOP has highest priority."""
    SUCCESS = "SUCCESS"
    BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"
    MODEL_UNAVAILABLE = "MODEL_UNAVAILABLE"
    SANDBOX_FAILURE = "SANDBOX_FAILURE"
    CONFIGURATION_ERROR = "CONFIGURATION_ERROR"
    NO_PROGRESS = "NO_PROGRESS"
    HUMAN_ABORTED = "HUMAN_ABORTED"
    SECURITY_STOP = "SECURITY_STOP"


class ActionProposal(BaseModel):
    """Raw action proposal from model, human, or verification system."""
    model_config = ConfigDict(extra="forbid", frozen=True)

    action_id: str
    source: ActionSource
    parent_action_id: str | None = None
    type: str
    raw_args: dict[str, Any]
    workspace_id: str
    round: int


class NormalizedAction(BaseModel):
    """Normalized and validated action after ToolRegistry processing."""
    model_config = ConfigDict(extra="forbid", frozen=True)

    action_id: str
    source: ActionSource
    parent_action_id: str | None = None
    type: str
    normalized_args: dict[str, Any]
    workspace_id: str
    round: int


class ModelDecision(BaseModel):
    """Decision returned by LLM."""
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: str  # "action", "finish", or "message"
    action: ActionProposal | None = None
    message: str | None = None


class RawExecutionResult(BaseModel):
    """Raw result from Docker sandbox execution."""
    model_config = ConfigDict(extra="forbid", frozen=True)

    execution_id: str
    action_id: str
    exit_code: int
    stdout: bytes
    stderr: bytes
    duration: float
    outcome: str  # "success", "timeout", "oom", "error"
    sandbox_meta: dict[str, Any]


class StructuredFeedback(BaseModel):
    """Structured feedback parsed from execution results."""
    model_config = ConfigDict(extra="forbid", frozen=True)

    feedback_id: str
    action_id: str
    category: str
    summary: str
    locations: list[dict[str, Any]]
    error_signature: str | None = None
    truncated: bool = False
    artifact_refs: list[dict[str, Any]]


class TaskRequest(BaseModel):
    """Task request from user."""
    model_config = ConfigDict(extra="forbid", frozen=True)

    task_id: str
    prompt: str
    workspace_path: str
    policy_path: str
    verification_profile_path: str
    budget_overrides: dict[str, Any] | None = None


class RunResult(BaseModel):
    """Final result of a run."""
    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: str
    task_id: str
    terminal_state: TerminalState
    audit_log_path: str
    summary: str
