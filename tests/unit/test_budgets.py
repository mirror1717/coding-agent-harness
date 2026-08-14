from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import Any

import pytest

from coding_agent_harness.budgets import (
    BudgetController,
    BudgetKind,
    BudgetLimits,
    BudgetSnapshot,
    NoProgressDetector,
)
from coding_agent_harness.errors import HarnessValidationError


@dataclass
class FakeClock:
    current: float = 100.0

    def monotonic(self) -> float:
        return self.current

    def advance(self, seconds: float) -> None:
        self.current += seconds


def limits(**overrides: Any) -> BudgetLimits:
    values: dict[str, Any] = {
        "wall_clock_seconds": 100.0,
        "sandbox_execution_seconds": 50.0,
        "approval_timeout_seconds": 20.0,
        "hitl_wait_seconds": 40.0,
        "max_rounds": 5,
        "max_llm_calls": 6,
        "max_tool_calls": 7,
    }
    values.update(overrides)
    return BudgetLimits(**values)


@pytest.mark.parametrize("value", [True, "10"])
@pytest.mark.parametrize(
    "field_name",
    [
        "wall_clock_seconds",
        "sandbox_execution_seconds",
        "approval_timeout_seconds",
        "hitl_wait_seconds",
    ],
)
def test_duration_limits_reject_coerced_values(field_name: str, value: object) -> None:
    with pytest.raises(ValueError):
        limits(**{field_name: value})


@pytest.mark.parametrize("value", [True, "2", 2.5, 0, -1])
@pytest.mark.parametrize("field_name", ["max_rounds", "max_llm_calls", "max_tool_calls"])
def test_count_limits_require_strict_positive_integers(
    field_name: str, value: object
) -> None:
    with pytest.raises(ValueError):
        limits(**{field_name: value})


def test_wall_clock_uses_injected_monotonic_clock() -> None:
    clock = FakeClock()
    controller = BudgetController(limits=limits(), clock=clock)

    clock.advance(12.5)

    snapshot = controller.snapshot()
    assert snapshot.wall_clock_used_seconds == 12.5
    assert snapshot.wall_clock_remaining_seconds == 87.5
    assert snapshot.exhausted == ()


def test_snapshot_is_associated_with_run_and_no_progress_state() -> None:
    detector = NoProgressDetector(threshold=2)
    detector.observe(action_fingerprint="same")
    controller = BudgetController(
        run_id="run-123",
        limits=limits(),
        clock=FakeClock(),
        no_progress=detector,
    )

    snapshot = controller.snapshot()
    assert snapshot.run_id == "run-123"
    assert snapshot.no_progress == detector.snapshot()


@pytest.mark.parametrize("clock_value", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_clock_at_initialization_fails_closed(clock_value: float) -> None:
    with pytest.raises(HarnessValidationError, match="clock.*finite"):
        BudgetController(limits=limits(), clock=FakeClock(clock_value))


@pytest.mark.parametrize("clock_value", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_clock_during_snapshot_fails_closed(clock_value: float) -> None:
    clock = FakeClock()
    controller = BudgetController(limits=limits(), clock=clock)
    clock.current = clock_value

    with pytest.raises(HarnessValidationError, match="clock.*finite"):
        controller.snapshot()


def test_monotonic_clock_rollback_fails_closed_and_budget_never_recovers() -> None:
    clock = FakeClock()
    controller = BudgetController(limits=limits(), clock=clock)
    clock.advance(10.0)
    assert controller.snapshot().wall_clock_used_seconds == 10.0

    clock.current = 105.0
    with pytest.raises(HarnessValidationError, match="clock.*backward"):
        controller.snapshot()


def test_wall_elapsed_overflow_fails_before_last_clock_is_updated() -> None:
    clock = FakeClock(-sys.float_info.max)
    controller = BudgetController(
        limits=limits(wall_clock_seconds=sys.float_info.max),
        clock=clock,
    )
    clock.current = sys.float_info.max

    with pytest.raises(HarnessValidationError, match="elapsed.*finite"):
        controller.snapshot()

    clock.current = -sys.float_info.max / 2
    assert controller.snapshot().wall_clock_used_seconds == sys.float_info.max / 2


def test_huge_integer_clock_at_initialization_is_stably_rejected() -> None:
    with pytest.raises(HarnessValidationError, match="clock.*finite"):
        BudgetController(limits=limits(), clock=FakeClock(10**10000))  # type: ignore[arg-type]


def test_huge_integer_clock_during_snapshot_does_not_change_state() -> None:
    clock = FakeClock()
    controller = BudgetController(limits=limits(), clock=clock)
    clock.current = 10**10000

    with pytest.raises(HarnessValidationError, match="clock.*finite"):
        controller.snapshot()

    clock.current = 101.0
    assert controller.snapshot().wall_clock_used_seconds == 1.0


@pytest.mark.parametrize("clock_value", [float("nan"), float("inf"), float("-inf")])
def test_budget_snapshot_rejects_non_finite_float_fields(clock_value: float) -> None:
    with pytest.raises(ValueError):
        BudgetSnapshot(
            run_id="run-123",
            limits=limits(),
            no_progress=NoProgressDetector(threshold=2).snapshot(),
            wall_clock_used_seconds=clock_value,
            sandbox_execution_used_seconds=0.0,
            hitl_wait_used_seconds=0.0,
            rounds_used=0,
            llm_calls_used=0,
            tool_calls_used=0,
            wall_clock_remaining_seconds=10.0,
            sandbox_execution_remaining_seconds=10.0,
            hitl_wait_remaining_seconds=10.0,
            rounds_remaining=1,
            llm_calls_remaining=1,
            tool_calls_remaining=1,
            exhausted=(),
        )


@pytest.mark.parametrize(
    ("field_name", "negative_value"),
    [
        ("wall_clock_used_seconds", -0.1),
        ("sandbox_execution_used_seconds", -0.1),
        ("hitl_wait_used_seconds", -0.1),
        ("rounds_used", -1),
        ("llm_calls_used", -1),
        ("tool_calls_used", -1),
        ("wall_clock_remaining_seconds", -0.1),
        ("sandbox_execution_remaining_seconds", -0.1),
        ("hitl_wait_remaining_seconds", -0.1),
        ("rounds_remaining", -1),
        ("llm_calls_remaining", -1),
        ("tool_calls_remaining", -1),
    ],
)
def test_budget_snapshot_rejects_negative_usage_and_remaining_fields(
    field_name: str, negative_value: float
) -> None:
    snapshot = BudgetController(limits=limits(), clock=FakeClock()).snapshot()
    values = snapshot.model_dump()
    values[field_name] = negative_value

    with pytest.raises(ValueError):
        BudgetSnapshot.model_validate(values)


def test_sandbox_execution_is_independent_of_wall_clock() -> None:
    clock = FakeClock()
    controller = BudgetController(limits=limits(), clock=clock)

    clock.advance(10.0)
    controller.record_sandbox_execution(4.5)

    snapshot = controller.snapshot()
    assert snapshot.wall_clock_used_seconds == 10.0
    assert snapshot.sandbox_execution_used_seconds == 4.5
    assert snapshot.sandbox_execution_remaining_seconds == 45.5


def test_approval_wait_consumes_wall_and_hitl_but_not_sandbox() -> None:
    clock = FakeClock()
    controller = BudgetController(limits=limits(), clock=clock)

    clock.advance(7.0)
    controller.record_approval_wait(7.0)

    snapshot = controller.snapshot()
    assert snapshot.wall_clock_used_seconds == 7.0
    assert snapshot.hitl_wait_used_seconds == 7.0
    assert snapshot.sandbox_execution_used_seconds == 0.0
    assert controller.approval_allowance_seconds() == 20.0


def test_approval_allowance_respects_each_independent_time_limit() -> None:
    clock = FakeClock()
    controller = BudgetController(
        limits=limits(
            wall_clock_seconds=15.0,
            approval_timeout_seconds=20.0,
            hitl_wait_seconds=12.0,
        ),
        clock=clock,
    )
    clock.advance(5.0)
    controller.record_approval_wait(3.0)

    assert controller.approval_allowance_seconds() == 9.0
    assert controller.approval_timed_out(20.0) is True
    assert controller.approval_timed_out(19.999) is False


def test_round_llm_and_tool_call_budgets_change_independently() -> None:
    controller = BudgetController(limits=limits(), clock=FakeClock())

    controller.record_round()
    controller.record_llm_call()
    controller.record_tool_call()
    controller.record_tool_call()

    snapshot = controller.snapshot()
    assert snapshot.rounds_used == 1
    assert snapshot.llm_calls_used == 1
    assert snapshot.tool_calls_used == 2
    assert snapshot.rounds_remaining == 4
    assert snapshot.llm_calls_remaining == 5
    assert snapshot.tool_calls_remaining == 5


@pytest.mark.parametrize(
    ("overrides", "consume", "expected"),
    [
        ({"wall_clock_seconds": 2.0}, "wall", BudgetKind.WALL_CLOCK),
        (
            {"sandbox_execution_seconds": 2.0},
            "sandbox",
            BudgetKind.SANDBOX_EXECUTION,
        ),
        ({"hitl_wait_seconds": 2.0}, "hitl", BudgetKind.HITL_WAIT),
        ({"max_rounds": 1}, "round", BudgetKind.ROUNDS),
        ({"max_llm_calls": 1}, "llm", BudgetKind.LLM_CALLS),
        ({"max_tool_calls": 1}, "tool", BudgetKind.TOOL_CALLS),
    ],
)
def test_each_cumulative_budget_reports_its_own_exhaustion(
    overrides: dict[str, Any], consume: str, expected: BudgetKind
) -> None:
    clock = FakeClock()
    controller = BudgetController(limits=limits(**overrides), clock=clock)

    if consume == "wall":
        clock.advance(2.0)
    elif consume == "sandbox":
        controller.record_sandbox_execution(2.0)
    elif consume == "hitl":
        controller.record_approval_wait(2.0)
    elif consume == "round":
        controller.record_round()
    elif consume == "llm":
        controller.record_llm_call()
    else:
        controller.record_tool_call()

    assert controller.snapshot().exhausted == (expected,)


def test_limits_can_be_tightened_but_not_raised_during_run() -> None:
    controller = BudgetController(limits=limits(), clock=FakeClock())

    controller.tighten_limits(limits(max_rounds=3, max_llm_calls=4))
    assert controller.snapshot().limits.max_rounds == 3
    assert controller.snapshot().limits.max_llm_calls == 4

    with pytest.raises(HarnessValidationError, match="cannot raise"):
        controller.tighten_limits(limits(max_rounds=4, max_llm_calls=4))


def test_negative_consumption_is_rejected_without_changing_usage() -> None:
    controller = BudgetController(limits=limits(), clock=FakeClock())

    with pytest.raises(HarnessValidationError, match="non-negative"):
        controller.record_sandbox_execution(-1.0)
    with pytest.raises(HarnessValidationError, match="non-negative"):
        controller.record_approval_wait(-1.0)

    snapshot = controller.snapshot()
    assert snapshot.sandbox_execution_used_seconds == 0.0
    assert snapshot.hitl_wait_used_seconds == 0.0


@pytest.mark.parametrize("duration", [True, "1.5"])
@pytest.mark.parametrize("recorder_name", ["record_sandbox_execution", "record_approval_wait"])
def test_runtime_durations_reject_coerced_values(
    recorder_name: str, duration: object
) -> None:
    controller = BudgetController(limits=limits(), clock=FakeClock())

    with pytest.raises(HarnessValidationError, match="number"):
        getattr(controller, recorder_name)(duration)


@pytest.mark.parametrize("recorder_name", ["record_sandbox_execution", "record_approval_wait"])
def test_huge_integer_duration_is_stably_rejected_without_state_change(
    recorder_name: str,
) -> None:
    controller = BudgetController(limits=limits(), clock=FakeClock())

    with pytest.raises(HarnessValidationError, match="duration.*finite"):
        getattr(controller, recorder_name)(10**10000)

    snapshot = controller.snapshot()
    assert snapshot.sandbox_execution_used_seconds == 0.0
    assert snapshot.hitl_wait_used_seconds == 0.0


def test_huge_integer_approval_timeout_measurement_is_stably_rejected() -> None:
    controller = BudgetController(limits=limits(), clock=FakeClock())

    with pytest.raises(HarnessValidationError, match="duration.*finite"):
        controller.approval_timed_out(10**10000)


@pytest.mark.parametrize("duration", [float("nan"), float("inf"), float("-inf")])
@pytest.mark.parametrize("recorder_name", ["record_sandbox_execution", "record_approval_wait"])
def test_non_finite_consumption_is_rejected_without_polluting_snapshot(
    recorder_name: str, duration: float
) -> None:
    controller = BudgetController(limits=limits(), clock=FakeClock())

    recorder = getattr(controller, recorder_name)
    with pytest.raises(HarnessValidationError, match="finite"):
        recorder(duration)

    snapshot = controller.snapshot()
    assert snapshot.sandbox_execution_used_seconds == 0.0
    assert snapshot.hitl_wait_used_seconds == 0.0


@pytest.mark.parametrize(
    ("recorder_name", "snapshot_field"),
    [
        ("record_sandbox_execution", "sandbox_execution_used_seconds"),
        ("record_approval_wait", "hitl_wait_used_seconds"),
    ],
)
def test_duration_accumulation_overflow_fails_without_polluting_state(
    recorder_name: str, snapshot_field: str
) -> None:
    controller = BudgetController(
        limits=limits(
            sandbox_execution_seconds=sys.float_info.max,
            hitl_wait_seconds=sys.float_info.max,
        ),
        clock=FakeClock(),
    )
    recorder = getattr(controller, recorder_name)
    recorder(sys.float_info.max)

    with pytest.raises(HarnessValidationError, match="accumulated.*finite"):
        recorder(sys.float_info.max)

    assert getattr(controller.snapshot(), snapshot_field) == sys.float_info.max


@pytest.mark.parametrize("duration", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_approval_timeout_measurement_is_rejected(duration: float) -> None:
    controller = BudgetController(limits=limits(), clock=FakeClock())

    with pytest.raises(HarnessValidationError, match="finite"):
        controller.approval_timed_out(duration)


def test_no_progress_detects_repeated_action_fingerprint() -> None:
    detector = NoProgressDetector(threshold=3)

    assert detector.observe(action_fingerprint="abc") is False
    assert detector.observe(action_fingerprint="abc") is False
    assert detector.observe(action_fingerprint="abc") is True
    assert detector.snapshot().consecutive_count == 3
    assert detector.snapshot().threshold == 3
    assert detector.snapshot().exhausted is True


@pytest.mark.parametrize(
    "threshold",
    [
        float("nan"),
        float("inf"),
        float("-inf"),
        2.5,
        True,
        False,
        0,
        -1,
    ],
)
def test_no_progress_threshold_requires_a_positive_integer(threshold: object) -> None:
    with pytest.raises(HarnessValidationError, match="positive integer"):
        NoProgressDetector(threshold=threshold)  # type: ignore[arg-type]


def test_no_progress_treats_normalized_error_signatures_as_equivalent() -> None:
    detector = NoProgressDetector(threshold=2)

    assert detector.observe(error_signature="  AssertionError:   expected 1  ") is False
    assert detector.observe(error_signature="assertionerror: expected 1") is True


def test_action_and_error_repetition_are_counted_independently() -> None:
    detector = NoProgressDetector(threshold=2)

    assert detector.observe(action_fingerprint="same-action") is False
    assert detector.observe(error_signature="same error") is False
    assert detector.observe(action_fingerprint="same-action") is True
    snapshot = detector.snapshot()
    assert snapshot.action_consecutive_count == 2
    assert snapshot.error_consecutive_count == 1
    assert detector.observe(error_signature="same error") is True


@pytest.mark.parametrize("invalid", [True, False, 1, 1.5])
@pytest.mark.parametrize("signal_kind", ["action", "error"])
def test_no_progress_signals_require_strings_before_state_changes(
    signal_kind: str, invalid: object
) -> None:
    detector = NoProgressDetector(threshold=2)
    valid_kwargs = (
        {"action_fingerprint": "stable"}
        if signal_kind == "action"
        else {"error_signature": "stable"}
    )
    invalid_kwargs = (
        {"action_fingerprint": invalid}
        if signal_kind == "action"
        else {"error_signature": invalid}
    )
    assert detector.observe(**valid_kwargs) is False

    with pytest.raises(HarnessValidationError, match="string"):
        detector.observe(**invalid_kwargs)  # type: ignore[arg-type]

    assert detector.observe(**valid_kwargs) is True


def test_progress_change_resets_consecutive_no_progress_count() -> None:
    detector = NoProgressDetector(threshold=2)

    assert detector.observe(action_fingerprint="first") is False
    assert detector.observe(action_fingerprint="second") is False
    assert detector.observe(action_fingerprint="second") is True
    assert detector.snapshot().consecutive_count == 2
