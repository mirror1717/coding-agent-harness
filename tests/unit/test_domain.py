import pytest
from pydantic import ValidationError

from coding_agent_harness.domain import (
    ActionProposal,
    FactModel,
    ModelDecision,
    NormalizedAction,
    RawExecutionResult,
    RunResult,
    StructuredFeedback,
    TaskRequest,
    TerminalState,
)
from coding_agent_harness.errors import (
    HarnessConfigurationError,
    HarnessError,
    HarnessEvidenceError,
    HarnessSecurityError,
    HarnessValidationError,
)


@pytest.mark.parametrize(
    "model",
    [
        ActionProposal,
        NormalizedAction,
        ModelDecision,
        RawExecutionResult,
        StructuredFeedback,
        TaskRequest,
        RunResult,
    ],
)
def test_models_forbid_extra_fields(model: type[FactModel]) -> None:
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        model.model_validate({"unexpected": "field"})


@pytest.mark.parametrize(
    "fact",
    [
        ActionProposal(),
        NormalizedAction(),
        ModelDecision(message="status update"),
        RawExecutionResult(),
        StructuredFeedback(),
        TaskRequest(),
        RunResult(),
    ],
)
def test_fact_models_are_frozen(fact: FactModel) -> None:
    attribute_name = "new_fact"

    with pytest.raises(ValidationError, match="Instance is frozen"):
        setattr(fact, attribute_name, "replacement")


@pytest.mark.parametrize(
    "fact, field_name",
    [
        (ActionProposal(raw_args={"nested": {"value": "original"}}), "raw_args"),
        (
            NormalizedAction(normalized_args={"nested": {"value": "original"}}),
            "normalized_args",
        ),
        (
            RawExecutionResult(sandbox_meta={"nested": {"value": "original"}}),
            "sandbox_meta",
        ),
        (TaskRequest(budgets={"nested": {"value": "original"}}), "budgets"),
    ],
)
def test_fact_model_mappings_are_deeply_immutable(
    fact: FactModel, field_name: str
) -> None:
    mapping = getattr(fact, field_name)

    with pytest.raises(TypeError):
        mapping["nested"]["value"] = "replacement"


@pytest.mark.parametrize(
    "payload",
    [
        {"kind": "action"},
        {"kind": "message"},
        {"kind": "finish", "action": ActionProposal()},
        {"kind": "finish", "message": "done"},
        {"kind": "action", "action": ActionProposal(), "message": "contradiction"},
        {"kind": "message", "action": ActionProposal(), "message": "contradiction"},
    ],
)
def test_model_decision_rejects_invalid_shapes(payload: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        ModelDecision.model_validate(payload)


@pytest.mark.parametrize(
    "decision",
    [
        ModelDecision(kind="action", action=ActionProposal()),
        ModelDecision(kind="message", message="status update"),
        ModelDecision(kind="finish"),
    ],
)
def test_model_decision_accepts_unambiguous_shapes(decision: ModelDecision) -> None:
    assert decision.kind in {"action", "finish", "message"}


@pytest.mark.parametrize(
    ("error_type", "expected_code"),
    [
        (HarnessValidationError, "VALIDATION_ERROR"),
        (HarnessConfigurationError, "CONFIGURATION_ERROR"),
        (HarnessSecurityError, "SECURITY_ERROR"),
        (HarnessEvidenceError, "EVIDENCE_ERROR"),
    ],
)
def test_error_subclasses_have_stable_codes(
    error_type: type[HarnessError], expected_code: str
) -> None:
    error = error_type("problem")

    assert error.code == expected_code
    assert error.message == "problem"
    assert str(error) == "problem"


def test_harness_error_retains_its_code_and_message() -> None:
    error = HarnessError("CUSTOM_ERROR", "problem")

    assert error.code == "CUSTOM_ERROR"
    assert error.message == "problem"
    assert str(error) == "problem"


def test_terminal_state_contains_security_stop() -> None:
    assert {state.value for state in TerminalState} == {
        "SUCCESS",
        "SECURITY_STOP",
        "HUMAN_ABORTED",
        "CONFIGURATION_ERROR",
        "BUDGET_EXHAUSTED",
        "NO_PROGRESS",
        "MODEL_UNAVAILABLE",
        "SANDBOX_FAILURE",
    }
