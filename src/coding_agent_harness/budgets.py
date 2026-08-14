"""Deterministic accounting for independent runtime budgets."""

from __future__ import annotations

import re
from enum import Enum
from math import isfinite
from typing import Annotated, Protocol

from pydantic import ConfigDict, Field, StrictFloat, StrictInt

from coding_agent_harness.domain import FactModel
from coding_agent_harness.errors import HarnessValidationError


class Clock(Protocol):
    """The monotonic clock surface required by budget accounting."""

    def monotonic(self) -> float: ...


class BudgetKind(str, Enum):
    WALL_CLOCK = "WALL_CLOCK"
    SANDBOX_EXECUTION = "SANDBOX_EXECUTION"
    HITL_WAIT = "HITL_WAIT"
    ROUNDS = "ROUNDS"
    LLM_CALLS = "LLM_CALLS"
    TOOL_CALLS = "TOOL_CALLS"


class BudgetLimits(FactModel):
    """Frozen maxima for one run."""

    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    wall_clock_seconds: Annotated[StrictFloat, Field(ge=0)]
    sandbox_execution_seconds: Annotated[StrictFloat, Field(ge=0)]
    approval_timeout_seconds: Annotated[StrictFloat, Field(ge=0)]
    hitl_wait_seconds: Annotated[StrictFloat, Field(ge=0)]
    max_rounds: Annotated[StrictInt, Field(gt=0)]
    max_llm_calls: Annotated[StrictInt, Field(gt=0)]
    max_tool_calls: Annotated[StrictInt, Field(gt=0)]


class NoProgressSnapshot(FactModel):
    """An immutable, audit-ready view of independent no-progress state."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    consecutive_count: Annotated[StrictInt, Field(ge=0)]
    action_consecutive_count: Annotated[StrictInt, Field(ge=0)]
    error_consecutive_count: Annotated[StrictInt, Field(ge=0)]
    threshold: Annotated[StrictInt, Field(gt=0)]
    exhausted: bool


class BudgetSnapshot(FactModel):
    """An immutable view of usage and remaining budgets."""

    model_config = ConfigDict(
        extra="forbid", frozen=True, allow_inf_nan=False, strict=True
    )

    run_id: str
    limits: BudgetLimits
    no_progress: NoProgressSnapshot
    wall_clock_used_seconds: Annotated[StrictFloat, Field(ge=0)]
    sandbox_execution_used_seconds: Annotated[StrictFloat, Field(ge=0)]
    hitl_wait_used_seconds: Annotated[StrictFloat, Field(ge=0)]
    rounds_used: Annotated[StrictInt, Field(ge=0)]
    llm_calls_used: Annotated[StrictInt, Field(ge=0)]
    tool_calls_used: Annotated[StrictInt, Field(ge=0)]
    wall_clock_remaining_seconds: Annotated[StrictFloat, Field(ge=0)]
    sandbox_execution_remaining_seconds: Annotated[StrictFloat, Field(ge=0)]
    hitl_wait_remaining_seconds: Annotated[StrictFloat, Field(ge=0)]
    rounds_remaining: Annotated[StrictInt, Field(ge=0)]
    llm_calls_remaining: Annotated[StrictInt, Field(ge=0)]
    tool_calls_remaining: Annotated[StrictInt, Field(ge=0)]
    exhausted: tuple[BudgetKind, ...]


class BudgetController:
    """Own monotonic accounting without deciding runtime state transitions."""

    def __init__(
        self,
        *,
        limits: BudgetLimits,
        clock: Clock,
        run_id: str = "",
        no_progress: NoProgressDetector | None = None,
    ) -> None:
        self._run_id = run_id
        self._no_progress = no_progress or NoProgressDetector(threshold=1)
        self._limits = limits
        self._clock = clock
        self._started_at = _read_clock(clock)
        self._last_clock = self._started_at
        self._sandbox_seconds = 0.0
        self._hitl_seconds = 0.0
        self._rounds = 0
        self._llm_calls = 0
        self._tool_calls = 0

    def record_sandbox_execution(self, duration_seconds: float) -> None:
        self._sandbox_seconds = _accumulate_duration(
            self._sandbox_seconds, duration_seconds
        )

    def record_approval_wait(self, duration_seconds: float) -> None:
        self._hitl_seconds = _accumulate_duration(self._hitl_seconds, duration_seconds)

    def record_round(self) -> None:
        self._rounds += 1

    def record_llm_call(self) -> None:
        self._llm_calls += 1

    def record_tool_call(self) -> None:
        self._tool_calls += 1

    def approval_allowance_seconds(self) -> float:
        """Return the maximum safe duration of the next approval wait."""

        snapshot = self.snapshot()
        return min(
            self._limits.approval_timeout_seconds,
            snapshot.wall_clock_remaining_seconds,
            snapshot.hitl_wait_remaining_seconds,
        )

    def approval_timed_out(self, wait_seconds: float) -> bool:
        return (
            _validated_duration(wait_seconds)
            >= self._limits.approval_timeout_seconds
        )

    def tighten_limits(self, limits: BudgetLimits) -> None:
        """Replace limits only when no maximum is raised."""

        for field_name in BudgetLimits.model_fields:
            if getattr(limits, field_name) > getattr(self._limits, field_name):
                raise HarnessValidationError(
                    f"cannot raise running budget limit: {field_name}"
                )
        self._limits = limits

    def snapshot(self) -> BudgetSnapshot:
        current_clock = _read_clock(self._clock)
        if current_clock < self._last_clock:
            raise HarnessValidationError("monotonic clock moved backward")
        wall_used = current_clock - self._started_at
        if not _is_finite_number(wall_used):
            raise HarnessValidationError("wall-clock elapsed duration must be finite")
        self._last_clock = current_clock
        remaining = {
            BudgetKind.WALL_CLOCK: max(
                0.0, self._limits.wall_clock_seconds - wall_used
            ),
            BudgetKind.SANDBOX_EXECUTION: max(
                0.0,
                self._limits.sandbox_execution_seconds - self._sandbox_seconds,
            ),
            BudgetKind.HITL_WAIT: max(
                0.0, self._limits.hitl_wait_seconds - self._hitl_seconds
            ),
            BudgetKind.ROUNDS: max(0, self._limits.max_rounds - self._rounds),
            BudgetKind.LLM_CALLS: max(
                0, self._limits.max_llm_calls - self._llm_calls
            ),
            BudgetKind.TOOL_CALLS: max(
                0, self._limits.max_tool_calls - self._tool_calls
            ),
        }
        exhausted = tuple(
            kind for kind in BudgetKind if remaining[kind] <= 0
        )
        return BudgetSnapshot(
            run_id=self._run_id,
            limits=self._limits,
            no_progress=self._no_progress.snapshot(),
            wall_clock_used_seconds=wall_used,
            sandbox_execution_used_seconds=self._sandbox_seconds,
            hitl_wait_used_seconds=self._hitl_seconds,
            rounds_used=self._rounds,
            llm_calls_used=self._llm_calls,
            tool_calls_used=self._tool_calls,
            wall_clock_remaining_seconds=float(remaining[BudgetKind.WALL_CLOCK]),
            sandbox_execution_remaining_seconds=float(
                remaining[BudgetKind.SANDBOX_EXECUTION]
            ),
            hitl_wait_remaining_seconds=float(remaining[BudgetKind.HITL_WAIT]),
            rounds_remaining=int(remaining[BudgetKind.ROUNDS]),
            llm_calls_remaining=int(remaining[BudgetKind.LLM_CALLS]),
            tool_calls_remaining=int(remaining[BudgetKind.TOOL_CALLS]),
            exhausted=exhausted,
        )


class NoProgressDetector:
    """Detect consecutive repetition of action or normalized error signals."""

    def __init__(self, *, threshold: int) -> None:
        if isinstance(threshold, bool) or not isinstance(threshold, int) or threshold <= 0:
            raise HarnessValidationError(
                "no-progress threshold must be a positive integer"
            )
        self._threshold = threshold
        self._last_signals: dict[str, str | None] = {"action": None, "error": None}
        self._counts: dict[str, int] = {"action": 0, "error": 0}

    def observe(
        self,
        *,
        action_fingerprint: str | None = None,
        error_signature: str | None = None,
    ) -> bool:
        supplied = (action_fingerprint is not None) + (error_signature is not None)
        if supplied != 1:
            raise HarnessValidationError(
                "exactly one no-progress signal must be supplied"
            )
        if action_fingerprint is not None and not isinstance(
            action_fingerprint, str
        ):
            raise HarnessValidationError("action fingerprint must be a string")
        if error_signature is not None and not isinstance(error_signature, str):
            raise HarnessValidationError("error signature must be a string")
        if action_fingerprint is not None:
            kind, value = "action", action_fingerprint
        else:
            kind = "error"
            value = _normalize_error_signature(error_signature or "")

        if value == self._last_signals[kind]:
            self._counts[kind] += 1
        else:
            self._last_signals[kind] = value
            self._counts[kind] = 1
        return self._counts[kind] >= self._threshold

    def snapshot(self) -> NoProgressSnapshot:
        consecutive_count = max(self._counts.values())
        return NoProgressSnapshot(
            consecutive_count=consecutive_count,
            action_consecutive_count=self._counts["action"],
            error_consecutive_count=self._counts["error"],
            threshold=self._threshold,
            exhausted=consecutive_count >= self._threshold,
        )


def _validated_duration(duration_seconds: object) -> float:
    if isinstance(duration_seconds, bool) or not isinstance(
        duration_seconds, int | float
    ):
        raise HarnessValidationError("duration must be a number")
    if not _is_finite_number(duration_seconds):
        raise HarnessValidationError("duration must be finite")
    if duration_seconds < 0:
        raise HarnessValidationError("duration must be non-negative")
    return duration_seconds


def _accumulate_duration(current: float, additional: object) -> float:
    result = current + _validated_duration(additional)
    if not _is_finite_number(result):
        raise HarnessValidationError("accumulated duration must remain finite")
    return result


def _read_clock(clock: Clock) -> float:
    value = clock.monotonic()
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise HarnessValidationError("clock value must be a number")
    if not _is_finite_number(value):
        raise HarnessValidationError("clock value must be finite")
    return value


def _is_finite_number(value: float) -> bool:
    try:
        return isfinite(value)
    except OverflowError:
        return False


def _normalize_error_signature(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip()).casefold()
