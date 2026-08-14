from itertools import combinations, permutations

import pytest
from pydantic import ValidationError

from coding_agent_harness.domain import TerminalState
from coding_agent_harness.errors import HarnessValidationError
from coding_agent_harness.termination import TerminalCandidate, choose_terminal


def test_security_stop_overrides_all_simultaneous_candidates() -> None:
    candidates = tuple(
        TerminalCandidate(
            state=state,
            reason=state.value,
            verified_success=state is TerminalState.SUCCESS,
        )
        for state in TerminalState
    )

    assert {candidate.state for candidate in candidates} == set(TerminalState)
    for ordering in permutations(candidates):
        assert choose_terminal(ordering).state is TerminalState.SECURITY_STOP


@pytest.mark.parametrize("reason", ["HARD_BOUNDARY", "AUDIT_INTEGRITY"])
def test_security_reason_cannot_be_overridden(reason: str) -> None:
    candidates = (
        TerminalCandidate(
            state=TerminalState.SECURITY_STOP,
            reason=reason,
        ),
        TerminalCandidate(state=TerminalState.BUDGET_EXHAUSTED),
    )

    chosen = choose_terminal(candidates)
    assert chosen.state is TerminalState.SECURITY_STOP
    assert chosen.reason == reason


def test_multiple_security_reasons_use_stable_tie_break_for_all_permutations() -> None:
    candidates = (
        TerminalCandidate(state=TerminalState.SECURITY_STOP, reason="z-boundary"),
        TerminalCandidate(state=TerminalState.SECURITY_STOP, reason="a-audit"),
    )

    for ordering in permutations(candidates):
        assert choose_terminal(ordering).reason == "a-audit"


def test_mixed_candidates_choose_same_security_fact_for_all_permutations() -> None:
    candidates = (
        TerminalCandidate(state=TerminalState.SECURITY_STOP, reason="z-boundary"),
        TerminalCandidate(state=TerminalState.SECURITY_STOP, reason="a-audit"),
        TerminalCandidate(state=TerminalState.BUDGET_EXHAUSTED, reason="budget"),
        TerminalCandidate(
            state=TerminalState.SUCCESS,
            reason="verified",
            verified_success=True,
        ),
    )

    for ordering in permutations(candidates):
        chosen = choose_terminal(ordering)
        assert chosen.state is TerminalState.SECURITY_STOP
        assert chosen.reason == "a-audit"


def test_unverified_success_candidate_is_rejected() -> None:
    with pytest.raises(HarnessValidationError, match="verified"):
        choose_terminal(
            (TerminalCandidate(state=TerminalState.SUCCESS, verified_success=False),)
        )


@pytest.mark.parametrize("value", ["yes", "true", 1])
def test_verified_success_requires_a_strict_boolean(value: object) -> None:
    with pytest.raises(ValidationError):
        TerminalCandidate(
            state=TerminalState.SUCCESS,
            verified_success=value,  # type: ignore[arg-type]
        )


@pytest.mark.parametrize(
    "state", [state for state in TerminalState if state is not TerminalState.SUCCESS]
)
def test_verified_success_flag_is_forbidden_on_non_success_state(
    state: TerminalState,
) -> None:
    with pytest.raises(ValidationError):
        TerminalCandidate(state=state, verified_success=True)


def test_security_stop_still_wins_over_an_unverified_success_claim() -> None:
    candidates = (
        TerminalCandidate(state=TerminalState.SUCCESS, verified_success=False),
        TerminalCandidate(state=TerminalState.SECURITY_STOP),
    )

    assert choose_terminal(candidates).state is TerminalState.SECURITY_STOP


@pytest.mark.parametrize(
    "failure_state",
    [
        TerminalState.BUDGET_EXHAUSTED,
        TerminalState.SANDBOX_FAILURE,
        TerminalState.NO_PROGRESS,
    ],
)
def test_ordinary_failure_overrides_an_unverified_success_claim(
    failure_state: TerminalState,
) -> None:
    candidates = (
        TerminalCandidate(state=TerminalState.SUCCESS, verified_success=False),
        TerminalCandidate(state=failure_state),
    )

    assert choose_terminal(candidates).state is failure_state


def test_success_is_not_selected_when_a_failure_candidate_exists() -> None:
    candidates = (
        TerminalCandidate(state=TerminalState.SUCCESS, verified_success=True),
        TerminalCandidate(state=TerminalState.BUDGET_EXHAUSTED),
    )

    assert choose_terminal(candidates).state is TerminalState.BUDGET_EXHAUSTED


def test_same_ordinary_failure_uses_stable_tie_break_for_all_permutations() -> None:
    candidates = (
        TerminalCandidate(state=TerminalState.BUDGET_EXHAUSTED, reason="z-tool"),
        TerminalCandidate(state=TerminalState.BUDGET_EXHAUSTED, reason="a-wall"),
    )

    for ordering in permutations(candidates):
        assert choose_terminal(ordering).reason == "a-wall"


def test_distinct_ordinary_failures_follow_total_priority_for_all_combinations() -> None:
    ordered_states = (
        TerminalState.HUMAN_ABORTED,
        TerminalState.CONFIGURATION_ERROR,
        TerminalState.MODEL_UNAVAILABLE,
        TerminalState.SANDBOX_FAILURE,
        TerminalState.BUDGET_EXHAUSTED,
        TerminalState.NO_PROGRESS,
    )

    for size in range(2, len(ordered_states) + 1):
        for state_combination in combinations(ordered_states, size):
            expected = state_combination[0]
            candidates = tuple(
                TerminalCandidate(state=state, reason=state.value)
                for state in state_combination
            )
            for ordering in permutations(candidates):
                assert choose_terminal(ordering).state is expected


def test_verified_success_is_accepted_when_it_is_the_only_candidate() -> None:
    candidate = TerminalCandidate(
        state=TerminalState.SUCCESS,
        reason="all_required_checks_passed",
        verified_success=True,
    )

    assert choose_terminal((candidate,)) == candidate


def test_multiple_success_candidates_choose_verified_independent_of_order() -> None:
    unverified = TerminalCandidate(
        state=TerminalState.SUCCESS,
        reason="unverified",
        verified_success=False,
    )
    verified_z = TerminalCandidate(
        state=TerminalState.SUCCESS,
        reason="z-verified",
        verified_success=True,
    )
    verified_a = TerminalCandidate(
        state=TerminalState.SUCCESS,
        reason="a-verified",
        verified_success=True,
    )

    assert choose_terminal((unverified, verified_z, verified_a)) == verified_a
    assert choose_terminal((verified_a, verified_z, unverified)) == verified_a


def test_multiple_unverified_success_candidates_are_rejected() -> None:
    candidates = (
        TerminalCandidate(state=TerminalState.SUCCESS, reason="b"),
        TerminalCandidate(state=TerminalState.SUCCESS, reason="a"),
    )

    with pytest.raises(HarnessValidationError, match="verified"):
        choose_terminal(candidates)


def test_no_terminal_candidate_is_rejected() -> None:
    with pytest.raises(HarnessValidationError, match="at least one"):
        choose_terminal(())
