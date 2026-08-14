from pathlib import Path

import pytest
from pydantic import ValidationError

from coding_agent_harness.domain import (
    ActionProposal,
    ActionSource,
    NormalizedAction,
    StructuredFeedback,
)
from coding_agent_harness.errors import HarnessConfigurationError
from coding_agent_harness.tools import ToolRegistry
from coding_agent_harness.verification import (
    AcceptanceCheck,
    CheckOutcome,
    CheckResult,
    VerificationProfile,
    create_verification_actions,
    evaluate_verification_results,
    load_verification_profile,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = REPO_ROOT / "config" / "default-verification.yaml"


def pytest_check(
    *,
    check_id: str = "pytest",
    required: bool = True,
    argv: list[str] | None = None,
    action_type: str = "pytest",
) -> AcceptanceCheck:
    return AcceptanceCheck(
        check_id=check_id,
        action_template=ActionProposal(
            type=action_type,
            raw_args={"argv": argv or ["pytest", "-q"]},
        ),
        required=required,
    )


def make_profile(
    checks: list[AcceptanceCheck],
    *,
    profile_id: str = "test-profile",
    name: str = "Test profile",
    finish_condition: str = "all_required_pass",
) -> VerificationProfile:
    return VerificationProfile(
        profile_id=profile_id,
        name=name,
        finish_condition=finish_condition,
        checks=tuple(checks),
    )


@pytest.mark.parametrize(
    "model_cls,valid_kwargs",
    [
        (VerificationProfile, None),
        (AcceptanceCheck, None),
        (CheckResult, None),
    ],
)
def test_fact_models_forbid_extra_fields(
    model_cls: type, valid_kwargs: dict[str, object] | None
) -> None:
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        model_cls.model_validate({"unexpected": "field"})


def test_profile_rejects_when_no_required_check() -> None:
    optional_only = pytest_check(required=False, check_id="optional-lint")

    with pytest.raises(ValidationError, match="at least one required"):
        make_profile([optional_only])


def test_profile_accepts_required_and_optional_checks() -> None:
    required = pytest_check(check_id="pytest", required=True)
    optional = pytest_check(check_id="lint", required=False)

    profile = make_profile([required, optional])

    assert len(profile.checks) == 2
    assert profile.checks[0].check_id == "pytest"
    assert profile.checks[1].check_id == "lint"


def test_profile_is_frozen() -> None:
    profile = make_profile([pytest_check()])

    with pytest.raises(ValidationError, match="Instance is frozen"):
        profile.name = "changed"  # type: ignore[misc]


def test_acceptance_check_is_frozen() -> None:
    check = pytest_check()

    with pytest.raises(ValidationError, match="Instance is frozen"):
        check.required = False  # type: ignore[misc]


def test_check_result_is_frozen() -> None:
    result = CheckResult(
        check_id="pytest",
        action_id="action-1",
        required=True,
        outcome=CheckOutcome.PASS,
    )

    with pytest.raises(ValidationError, match="Instance is frozen"):
        result.outcome = CheckOutcome.FAIL  # type: ignore[misc]


def test_default_profile_has_required_pytest_check() -> None:
    profile = load_verification_profile(DEFAULT_CONFIG)

    required = [c for c in profile.checks if c.required]
    assert len(required) >= 1

    pytest_checks = [c for c in required if c.action_template.type == "pytest"]
    assert len(pytest_checks) == 1

    check = pytest_checks[0]
    assert tuple(check.action_template.raw_args["argv"]) == ("pytest", "-q")
    assert check.timeout_seconds >= 1


def test_load_profile_rejects_invalid_yaml_syntax(tmp_path: Path) -> None:
    config = tmp_path / "bad.yaml"
    config.write_text("profile_id: [unclosed\n")

    with pytest.raises(HarnessConfigurationError):
        load_verification_profile(config)


def test_load_profile_rejects_profile_without_required_check(
    tmp_path: Path,
) -> None:
    config = tmp_path / "no-required.yaml"
    config.write_text(
        "profile_id: bad\n"
        "name: Bad\n"
        "finish_condition: none\n"
        "checks:\n"
        "  - check_id: optional\n"
        "    required: false\n"
        "    action_template:\n"
        "      type: pytest\n"
        "      raw_args:\n"
        "        argv: [pytest, -q]\n"
    )

    with pytest.raises(HarnessConfigurationError, match="at least one required"):
        load_verification_profile(config)


def test_create_verification_actions_sets_source_to_verification() -> None:
    profile = make_profile([pytest_check()])

    actions = create_verification_actions(
        profile, workspace_id="workspace-1", round=3
    )

    assert len(actions) == 1
    action = actions[0]
    assert action.source is ActionSource.VERIFICATION
    assert action.type == "pytest"
    assert action.workspace_id == "workspace-1"
    assert action.round == 3


def test_create_verification_actions_produces_one_per_check_in_order() -> None:
    checks = [
        pytest_check(check_id="pytest", required=True),
        pytest_check(check_id="lint", required=False, argv=["ruff", "check", "."]),
    ]
    profile = make_profile(checks)

    actions = create_verification_actions(
        profile, workspace_id="workspace-1", round=0
    )

    assert len(actions) == 2
    assert actions[0].type == "pytest"
    assert tuple(actions[0].raw_args["argv"]) == ("pytest", "-q")
    assert actions[1].type == "pytest"
    assert tuple(actions[1].raw_args["argv"]) == ("ruff", "check", ".")


def test_create_verification_actions_returns_proposals_not_executed() -> None:
    profile = make_profile([pytest_check()])

    actions = create_verification_actions(
        profile, workspace_id="workspace-1", round=0
    )

    assert all(isinstance(a, ActionProposal) for a in actions)
    assert not any(isinstance(a, NormalizedAction) for a in actions)


def test_required_check_missing_blocks_success_candidate() -> None:
    profile = make_profile([pytest_check(check_id="pytest", required=True)])

    outcome = evaluate_verification_results(profile, results=())

    assert not outcome.success_candidate
    assert "pytest" in outcome.blocking_check_ids


def test_required_check_failed_blocks_success_candidate() -> None:
    profile = make_profile([pytest_check(check_id="pytest", required=True)])
    results = (
        CheckResult(
            check_id="pytest",
            action_id="action-1",
            required=True,
            outcome=CheckOutcome.FAIL,
        ),
    )

    outcome = evaluate_verification_results(profile, results=results)

    assert not outcome.success_candidate
    assert "pytest" in outcome.blocking_check_ids


def test_required_check_error_blocks_success_candidate() -> None:
    profile = make_profile([pytest_check(check_id="pytest", required=True)])
    results = (
        CheckResult(
            check_id="pytest",
            action_id="action-1",
            required=True,
            outcome=CheckOutcome.ERROR,
        ),
    )

    outcome = evaluate_verification_results(profile, results=results)

    assert not outcome.success_candidate
    assert "pytest" in outcome.blocking_check_ids


def test_required_check_passed_produces_success_candidate() -> None:
    profile = make_profile([pytest_check(check_id="pytest", required=True)])
    results = (
        CheckResult(
            check_id="pytest",
            action_id="action-1",
            required=True,
            outcome=CheckOutcome.PASS,
        ),
    )

    outcome = evaluate_verification_results(profile, results=results)

    assert outcome.success_candidate
    assert outcome.blocking_check_ids == ()
    assert outcome.warnings == ()


def test_optional_check_failure_produces_warning_only() -> None:
    checks = [
        pytest_check(check_id="pytest", required=True),
        pytest_check(check_id="lint", required=False, argv=["ruff", "check", "."]),
    ]
    profile = make_profile(checks)
    results = (
        CheckResult(
            check_id="pytest",
            action_id="action-1",
            required=True,
            outcome=CheckOutcome.PASS,
        ),
        CheckResult(
            check_id="lint",
            action_id="action-2",
            required=False,
            outcome=CheckOutcome.FAIL,
        ),
    )

    outcome = evaluate_verification_results(profile, results=results)

    assert outcome.success_candidate
    assert outcome.blocking_check_ids == ()
    assert len(outcome.warnings) == 1
    assert "lint" in outcome.warnings[0]


def test_optional_check_missing_produces_warning_only() -> None:
    checks = [
        pytest_check(check_id="pytest", required=True),
        pytest_check(check_id="lint", required=False, argv=["ruff", "check", "."]),
    ]
    profile = make_profile(checks)
    results = (
        CheckResult(
            check_id="pytest",
            action_id="action-1",
            required=True,
            outcome=CheckOutcome.PASS,
        ),
    )

    outcome = evaluate_verification_results(profile, results=results)

    assert outcome.success_candidate
    assert outcome.blocking_check_ids == ()
    assert len(outcome.warnings) == 1
    assert "lint" in outcome.warnings[0]


def test_all_required_and_optional_pass_produces_success_without_warnings() -> None:
    checks = [
        pytest_check(check_id="pytest", required=True),
        pytest_check(check_id="lint", required=False, argv=["ruff", "check", "."]),
    ]
    profile = make_profile(checks)
    results = (
        CheckResult(
            check_id="pytest",
            action_id="action-1",
            required=True,
            outcome=CheckOutcome.PASS,
        ),
        CheckResult(
            check_id="lint",
            action_id="action-2",
            required=False,
            outcome=CheckOutcome.PASS,
        ),
    )

    outcome = evaluate_verification_results(profile, results=results)

    assert outcome.success_candidate
    assert outcome.warnings == ()
    assert outcome.blocking_check_ids == ()


def test_check_result_carries_optional_feedback() -> None:
    feedback = StructuredFeedback(
        feedback_id="fb-1",
        action_id="action-1",
        category="pytest",
        summary="1 failed",
    )
    result = CheckResult(
        check_id="pytest",
        action_id="action-1",
        required=True,
        outcome=CheckOutcome.FAIL,
        feedback=feedback,
    )

    assert result.feedback is not None
    assert result.feedback.summary == "1 failed"


def test_generated_action_normalizes_through_tool_registry(
    tmp_path: Path,
) -> None:
    profile = make_profile([pytest_check()])
    actions = create_verification_actions(
        profile, workspace_id="workspace-1", round=0
    )

    registry = ToolRegistry()
    normalized = registry.normalize(actions[0], tmp_path)

    assert normalized.source is ActionSource.VERIFICATION
    assert normalized.type == "pytest"
    assert tuple(normalized.normalized_args["argv"]) == ("pytest", "-q")
