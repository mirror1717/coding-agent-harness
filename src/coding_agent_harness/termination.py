"""Central selection of a single terminal outcome."""

from collections.abc import Iterable
from typing import Self

from pydantic import StrictBool, model_validator

from coding_agent_harness.domain import FactModel, TerminalState
from coding_agent_harness.errors import HarnessValidationError


class TerminalCandidate(FactModel):
    """A terminal fact offered to the centralized arbiter."""

    state: TerminalState
    reason: str = ""
    verified_success: StrictBool = False

    @model_validator(mode="after")
    def validate_verified_success_scope(self) -> Self:
        if self.verified_success and self.state is not TerminalState.SUCCESS:
            raise ValueError("verified_success is valid only for SUCCESS")
        return self


_TERMINAL_PRIORITY = {
    TerminalState.SECURITY_STOP: 7,
    TerminalState.HUMAN_ABORTED: 6,
    TerminalState.CONFIGURATION_ERROR: 5,
    TerminalState.MODEL_UNAVAILABLE: 4,
    TerminalState.SANDBOX_FAILURE: 3,
    TerminalState.BUDGET_EXHAUSTED: 2,
    TerminalState.NO_PROGRESS: 1,
    TerminalState.SUCCESS: 0,
}


def choose_terminal(candidates: Iterable[TerminalCandidate]) -> TerminalCandidate:
    """Choose one outcome using the explicit, total terminal priority table."""

    offered = tuple(candidates)
    if not offered:
        raise HarnessValidationError("at least one terminal candidate is required")

    failures = tuple(
        candidate for candidate in offered if candidate.state is not TerminalState.SUCCESS
    )
    if failures:
        winning_state = max(
            (candidate.state for candidate in failures),
            key=_TERMINAL_PRIORITY.__getitem__,
        )
        return _stable_candidate(
            candidate for candidate in failures if candidate.state is winning_state
        )

    verified = tuple(candidate for candidate in offered if candidate.verified_success)
    if not verified:
        raise HarnessValidationError("SUCCESS requires a verified success candidate")
    return _stable_candidate(verified)


def _stable_candidate(candidates: Iterable[TerminalCandidate]) -> TerminalCandidate:
    return min(
        candidates,
        key=lambda candidate: (candidate.reason, candidate.verified_success),
    )
