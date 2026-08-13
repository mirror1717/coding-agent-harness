"""Tests for core domain models - T01"""
import pytest
from pydantic import ValidationError


def test_models_forbid_extra_fields():
    """Pydantic models must reject extra fields"""
    from coding_agent_harness.domain import ActionProposal

    with pytest.raises(ValidationError) as exc_info:
        ActionProposal(
            action_id="act-1",
            source="MODEL",
            type="shell",
            raw_args={"argv": ["echo", "hi"], "cwd": "."},
            workspace_id="ws-1",
            round=1,
            extra_field="not_allowed"  # This should fail
        )
    assert "extra_forbidden" in str(exc_info.value) or "Extra inputs are not permitted" in str(exc_info.value)


def test_fact_models_are_frozen():
    """Pydantic models must be immutable"""
    from coding_agent_harness.domain import ActionProposal

    action = ActionProposal(
        action_id="act-1",
        source="MODEL",
        type="shell",
        raw_args={"argv": ["echo", "hi"], "cwd": "."},
        workspace_id="ws-1",
        round=1
    )

    with pytest.raises(ValidationError):
        action.action_id = "act-2"  # Should fail - model is frozen


def test_terminal_state_contains_security_stop():
    """TerminalState enum must include SECURITY_STOP"""
    from coding_agent_harness.domain import TerminalState

    assert hasattr(TerminalState, "SECURITY_STOP")
    assert TerminalState.SECURITY_STOP.value == "SECURITY_STOP"


def test_action_source_values():
    """ActionSource must have MODEL, VERIFICATION, HUMAN_EDIT"""
    from coding_agent_harness.domain import ActionSource

    assert ActionSource.MODEL.value == "MODEL"
    assert ActionSource.VERIFICATION.value == "VERIFICATION"
    assert ActionSource.HUMAN_EDIT.value == "HUMAN_EDIT"


def test_harness_error_hierarchy():
    """HarnessError and subclasses must exist with code/message"""
    from coding_agent_harness.errors import (
        ConfigurationError,
        EvidenceError,
        HarnessError,
        SecurityError,
        ValidationError,
    )

    # Base error
    err = HarnessError(code="TEST_ERROR", message="Test message")
    assert err.code == "TEST_ERROR"
    assert err.message == "Test message"

    # Subclasses
    val_err = ValidationError(code="VAL_001", message="Invalid field")
    assert isinstance(val_err, HarnessError)

    cfg_err = ConfigurationError(code="CFG_001", message="Bad config")
    assert isinstance(cfg_err, HarnessError)

    sec_err = SecurityError(code="SEC_001", message="Boundary violation")
    assert isinstance(sec_err, HarnessError)

    ev_err = EvidenceError(code="EV_001", message="Missing artifact")
    assert isinstance(ev_err, HarnessError)
