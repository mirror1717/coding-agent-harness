"""Immutable domain facts shared by the governed harness."""

from collections.abc import Iterator, Mapping
from enum import Enum
from types import MappingProxyType
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class FrozenMapping(Mapping[str, Any]):
    """A recursively immutable mapping that retains normal mapping access."""

    __slots__ = ("_values",)
    _values: Mapping[str, Any]

    def __init__(self, values: Mapping[str, Any] | None = None) -> None:
        object.__setattr__(
            self,
            "_values",
            MappingProxyType(
                {key: freeze_value(value) for key, value in (values or {}).items()}
            ),
        )

    def __setattr__(self, name: str, value: Any) -> None:
        raise AttributeError("FrozenMapping is immutable")

    def __getitem__(self, key: str) -> Any:
        return self._values[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)


def freeze_value(value: Any) -> Any:
    """Recursively turn mutable containers into immutable value containers."""

    if isinstance(value, Mapping):
        return FrozenMapping(value)
    if isinstance(value, list | tuple):
        return tuple(freeze_value(item) for item in value)
    if isinstance(value, set | frozenset):
        return frozenset(freeze_value(item) for item in value)
    return value


class ActionSource(str, Enum):
    """The actor that originated an action."""

    MODEL = "MODEL"
    VERIFICATION = "VERIFICATION"
    HUMAN_EDIT = "HUMAN_EDIT"


class RuntimeState(str, Enum):
    """High-level runtime phases before a terminal outcome is reached."""

    PREPARING = "PREPARING"
    AWAITING_MODEL = "AWAITING_MODEL"
    NORMALIZING = "NORMALIZING"
    GOVERNING = "GOVERNING"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    EXECUTING = "EXECUTING"
    PROCESSING_FEEDBACK = "PROCESSING_FEEDBACK"
    VERIFYING = "VERIFYING"
    TERMINAL = "TERMINAL"


class TerminalState(str, Enum):
    """Frozen terminal outcomes for a harness run."""

    SUCCESS = "SUCCESS"
    SECURITY_STOP = "SECURITY_STOP"
    HUMAN_ABORTED = "HUMAN_ABORTED"
    CONFIGURATION_ERROR = "CONFIGURATION_ERROR"
    BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"
    NO_PROGRESS = "NO_PROGRESS"
    MODEL_UNAVAILABLE = "MODEL_UNAVAILABLE"
    SANDBOX_FAILURE = "SANDBOX_FAILURE"


class FactModel(BaseModel):
    """Base configuration for immutable, closed-domain facts."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class ActionProposal(FactModel):
    action_id: str = ""
    source: ActionSource = ActionSource.MODEL
    parent_action_id: str | None = None
    type: str = ""
    raw_args: Mapping[str, Any] = Field(default_factory=FrozenMapping)
    workspace_id: str = ""
    round: int = 0

    @field_validator("raw_args")
    @classmethod
    def freeze_raw_args(cls, value: Mapping[str, Any]) -> FrozenMapping:
        return FrozenMapping(value)


class NormalizedAction(ActionProposal):
    normalized_args: Mapping[str, Any] = Field(default_factory=FrozenMapping)

    @field_validator("normalized_args")
    @classmethod
    def freeze_normalized_args(cls, value: Mapping[str, Any]) -> FrozenMapping:
        return FrozenMapping(value)


class ModelDecision(FactModel):
    kind: Literal["action", "finish", "message"] = "message"
    action: ActionProposal | None = None
    message: str | None = None

    @model_validator(mode="after")
    def validate_shape(self) -> Self:
        if self.kind == "action":
            if self.action is None:
                raise ValueError("action decisions require an action")
            if self.message is not None:
                raise ValueError("action decisions cannot include a message")
        elif self.kind == "message":
            if self.message is None:
                raise ValueError("message decisions require a message")
            if self.action is not None:
                raise ValueError("message decisions cannot include an action")
        elif self.action is not None or self.message is not None:
            raise ValueError("finish decisions cannot include an action or message")
        return self


class RawExecutionResult(FactModel):
    execution_id: str = ""
    action_id: str = ""
    exit_code: int | None = None
    stdout_artifact: str | None = None
    stderr_artifact: str | None = None
    report_artifacts: tuple[str, ...] = ()
    duration_seconds: float = 0.0
    outcome: str = ""
    sandbox_meta: Mapping[str, Any] = Field(default_factory=FrozenMapping)

    @field_validator("sandbox_meta")
    @classmethod
    def freeze_sandbox_meta(cls, value: Mapping[str, Any]) -> FrozenMapping:
        return FrozenMapping(value)


class StructuredFeedback(FactModel):
    feedback_id: str = ""
    action_id: str = ""
    category: str = ""
    summary: str = ""
    locations: tuple[str, ...] = ()
    error_signature: str | None = None
    truncated: bool = False
    artifact_refs: tuple[str, ...] = ()


class TaskRequest(FactModel):
    task_id: str = ""
    prompt: str = ""
    workspace_id: str = ""
    policy_version: str = ""
    verification_profile_id: str = ""
    budgets: Mapping[str, Any] = Field(default_factory=FrozenMapping)

    @field_validator("budgets")
    @classmethod
    def freeze_budgets(cls, value: Mapping[str, Any]) -> FrozenMapping:
        return FrozenMapping(value)


class RunResult(FactModel):
    run_id: str = ""
    task_id: str = ""
    state: RuntimeState = RuntimeState.PREPARING
    terminal_state: TerminalState | None = None
    terminal_reason: str | None = None
