"""VerificationProfile and system action generation for acceptance checks."""

from __future__ import annotations

from collections.abc import Mapping
from enum import Enum
from pathlib import Path
from typing import Any, Self

import yaml  # type: ignore[import-untyped]
from pydantic import Field, model_validator

from coding_agent_harness.domain import (
    ActionProposal,
    ActionSource,
    FactModel,
    FrozenMapping,
    StructuredFeedback,
)
from coding_agent_harness.errors import HarnessConfigurationError

MAX_CHECK_TIMEOUT_SECONDS = 3600


class CheckOutcome(str, Enum):
    """The outcome of a single acceptance check execution."""

    PASS = "PASS"
    FAIL = "FAIL"
    MISSING = "MISSING"
    ERROR = "ERROR"


class AcceptanceCheck(FactModel):
    """A single acceptance check instantiated as a source=VERIFICATION action."""

    check_id: str
    action_template: ActionProposal
    required: bool
    timeout_seconds: int = Field(default=300, ge=1, le=MAX_CHECK_TIMEOUT_SECONDS)
    resource_limits: Mapping[str, Any] = Field(default_factory=FrozenMapping)

    @model_validator(mode="after")
    def freeze_resource_limits(self) -> Self:
        object.__setattr__(
            self, "resource_limits", FrozenMapping(self.resource_limits)
        )
        return self


class VerificationProfile(FactModel):
    """A strict schema describing the required and optional acceptance checks."""

    profile_id: str
    name: str
    finish_condition: str
    checks: tuple[AcceptanceCheck, ...]

    @model_validator(mode="after")
    def require_at_least_one_required_check(self) -> Self:
        if not any(check.required for check in self.checks):
            raise ValueError(
                "verification profile must declare at least one required check"
            )
        return self


class CheckResult(FactModel):
    """The immutable outcome of one acceptance check execution."""

    check_id: str
    action_id: str
    required: bool
    outcome: CheckOutcome
    feedback: StructuredFeedback | None = None


class VerificationOutcome(FactModel):
    """The evaluated result of a verification profile against check results."""

    profile_id: str
    results: tuple[CheckResult, ...]
    success_candidate: bool
    warnings: tuple[str, ...]
    blocking_check_ids: tuple[str, ...]


def create_verification_actions(
    profile: VerificationProfile,
    *,
    workspace_id: str,
    round: int,
) -> tuple[ActionProposal, ...]:
    """Create source=VERIFICATION action proposals for each acceptance check.

    The proposals are not normalized, governed, or executed here. The runtime
    is responsible for sending them through ToolRegistry, Guardrail, Policy,
    approval, and the sandbox like any other action.
    """

    actions: list[ActionProposal] = []
    for check in profile.checks:
        template = check.action_template
        actions.append(
            ActionProposal(
                action_id="",
                source=ActionSource.VERIFICATION,
                parent_action_id=None,
                type=template.type,
                raw_args=template.raw_args,
                workspace_id=workspace_id,
                round=round,
            )
        )
    return tuple(actions)


def evaluate_verification_results(
    profile: VerificationProfile,
    *,
    results: tuple[CheckResult, ...],
) -> VerificationOutcome:
    """Evaluate check results against the profile to determine success candidacy.

    A missing or failed required check blocks the success candidate. An
    optional check that is missing or did not pass only produces a warning.
    """

    results_by_id: dict[str, CheckResult] = {
        result.check_id: result for result in results
    }
    blocking: list[str] = []
    warnings: list[str] = []
    for check in profile.checks:
        result = results_by_id.get(check.check_id)
        if result is None:
            if check.required:
                blocking.append(check.check_id)
            else:
                warnings.append(
                    f"optional check {check.check_id!r} has no result"
                )
            continue
        if result.outcome is not CheckOutcome.PASS:
            if check.required:
                blocking.append(check.check_id)
            else:
                warnings.append(
                    f"optional check {check.check_id!r} "
                    f"did not pass: {result.outcome.value}"
                )
    return VerificationOutcome(
        profile_id=profile.profile_id,
        results=results,
        success_candidate=not blocking,
        warnings=tuple(warnings),
        blocking_check_ids=tuple(blocking),
    )


def load_verification_profile(path: Path | str) -> VerificationProfile:
    """Load and validate a verification profile from a YAML file."""

    try:
        with open(path, "rb") as stream:
            data = yaml.safe_load(stream)
    except yaml.YAMLError as error:
        raise HarnessConfigurationError(
            f"verification profile is not valid YAML: {error}"
        ) from error
    except OSError as error:
        raise HarnessConfigurationError(
            f"verification profile could not be read: {error}"
        ) from error
    try:
        return VerificationProfile.model_validate(data)
    except Exception as error:
        message = _format_validation_error(error)
        raise HarnessConfigurationError(message) from error


def _format_validation_error(error: BaseException) -> str:
    message = str(error)
    if not message:
        return "verification profile failed validation"
    return message
