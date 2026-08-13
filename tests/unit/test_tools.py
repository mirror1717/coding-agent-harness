"""Tests for ToolRegistry and structured Action validation - T05"""
import pytest


def test_valid_argv_accepted():
    """Valid structured argv must be accepted"""
    from coding_agent_harness.domain import ActionProposal, ActionSource
    from coding_agent_harness.tools import ToolRegistry

    registry = ToolRegistry()
    proposal = ActionProposal(
        action_id="act-1",
        source=ActionSource.MODEL,
        type="shell",
        raw_args={"argv": ["pytest", "-q"], "cwd": "src"},
        workspace_id="ws-1",
        round=1,
    )

    normalized = registry.normalize(proposal, workspace_path="/workspace")

    assert normalized.type == "shell"
    assert normalized.normalized_args["argv"] == ["pytest", "-q"]


def test_empty_argv_rejected():
    """Empty argv must be rejected"""
    from coding_agent_harness.domain import ActionProposal, ActionSource
    from coding_agent_harness.errors import ValidationError
    from coding_agent_harness.tools import ToolRegistry

    registry = ToolRegistry()
    proposal = ActionProposal(
        action_id="act-1",
        source=ActionSource.MODEL,
        type="shell",
        raw_args={"argv": [], "cwd": "."},
        workspace_id="ws-1",
        round=1,
    )

    with pytest.raises(ValidationError):
        registry.normalize(proposal, workspace_path="/workspace")


def test_shell_string_rejected():
    """Shell string (not argv array) must be rejected"""
    from coding_agent_harness.domain import ActionProposal, ActionSource
    from coding_agent_harness.errors import ValidationError
    from coding_agent_harness.tools import ToolRegistry

    registry = ToolRegistry()
    proposal = ActionProposal(
        action_id="act-1",
        source=ActionSource.MODEL,
        type="shell",
        raw_args={"command": "pytest -q", "cwd": "."},
        workspace_id="ws-1",
        round=1,
    )

    with pytest.raises(ValidationError):
        registry.normalize(proposal, workspace_path="/workspace")


def test_bash_c_rejected():
    """bash -c and sh -c patterns must be rejected"""
    from coding_agent_harness.domain import ActionProposal, ActionSource
    from coding_agent_harness.errors import ValidationError
    from coding_agent_harness.tools import ToolRegistry

    registry = ToolRegistry()

    # bash -c
    proposal1 = ActionProposal(
        action_id="act-1",
        source=ActionSource.MODEL,
        type="shell",
        raw_args={"argv": ["bash", "-c", "echo hi"], "cwd": "."},
        workspace_id="ws-1",
        round=1,
    )

    with pytest.raises(ValidationError):
        registry.normalize(proposal1, workspace_path="/workspace")

    # sh -c
    proposal2 = ActionProposal(
        action_id="act-2",
        source=ActionSource.MODEL,
        type="shell",
        raw_args={"argv": ["sh", "-c", "echo hi"], "cwd": "."},
        workspace_id="ws-1",
        round=1,
    )

    with pytest.raises(ValidationError):
        registry.normalize(proposal2, workspace_path="/workspace")


def test_path_escape_rejected():
    """Paths escaping workspace must be rejected"""
    from coding_agent_harness.domain import ActionProposal, ActionSource
    from coding_agent_harness.errors import ValidationError
    from coding_agent_harness.tools import ToolRegistry

    registry = ToolRegistry()
    proposal = ActionProposal(
        action_id="act-1",
        source=ActionSource.MODEL,
        type="read_file",
        raw_args={"path": "../outside.txt"},
        workspace_id="ws-1",
        round=1,
    )

    with pytest.raises(ValidationError):
        registry.normalize(proposal, workspace_path="/workspace")


def test_absolute_path_rejected():
    """Absolute paths must be rejected"""
    from coding_agent_harness.domain import ActionProposal, ActionSource
    from coding_agent_harness.errors import ValidationError
    from coding_agent_harness.tools import ToolRegistry

    registry = ToolRegistry()
    proposal = ActionProposal(
        action_id="act-1",
        source=ActionSource.MODEL,
        type="read_file",
        raw_args={"path": "/etc/passwd"},
        workspace_id="ws-1",
        round=1,
    )

    with pytest.raises(ValidationError):
        registry.normalize(proposal, workspace_path="/workspace")


def test_symlink_escape_rejected(tmp_path):
    """Symlink escaping workspace must be rejected"""
    from coding_agent_harness.domain import ActionProposal, ActionSource
    from coding_agent_harness.errors import ValidationError
    from coding_agent_harness.tools import ToolRegistry

    # Create workspace and symlink to outside
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("secret")
    (workspace / "link.txt").symlink_to(outside)

    registry = ToolRegistry()
    proposal = ActionProposal(
        action_id="act-1",
        source=ActionSource.MODEL,
        type="read_file",
        raw_args={"path": "link.txt"},
        workspace_id="ws-1",
        round=1,
    )

    with pytest.raises(ValidationError):
        registry.normalize(proposal, workspace_path=str(workspace))


def test_illegal_env_rejected():
    """Environment variables not in allowlist must be rejected"""
    from coding_agent_harness.domain import ActionProposal, ActionSource
    from coding_agent_harness.errors import ValidationError
    from coding_agent_harness.tools import ToolRegistry

    registry = ToolRegistry()
    proposal = ActionProposal(
        action_id="act-1",
        source=ActionSource.MODEL,
        type="shell",
        raw_args={
            "argv": ["pytest"],
            "cwd": ".",
            "env": {"OPENAI_API_KEY": "sk-secret"},
        },
        workspace_id="ws-1",
        round=1,
    )

    with pytest.raises(ValidationError):
        registry.normalize(proposal, workspace_path="/workspace")


def test_oversized_stdin_rejected():
    """Oversized stdin must be rejected"""
    from coding_agent_harness.domain import ActionProposal, ActionSource
    from coding_agent_harness.errors import ValidationError
    from coding_agent_harness.tools import ToolRegistry

    registry = ToolRegistry()
    proposal = ActionProposal(
        action_id="act-1",
        source=ActionSource.MODEL,
        type="shell",
        raw_args={
            "argv": ["cat"],
            "cwd": ".",
            "stdin": "x" * 100000,  # Too large
        },
        workspace_id="ws-1",
        round=1,
    )

    with pytest.raises(ValidationError):
        registry.normalize(proposal, workspace_path="/workspace")


def test_unknown_tool_rejected():
    """Unknown tool types must be rejected"""
    from coding_agent_harness.domain import ActionProposal, ActionSource
    from coding_agent_harness.errors import ValidationError
    from coding_agent_harness.tools import ToolRegistry

    registry = ToolRegistry()
    proposal = ActionProposal(
        action_id="act-1",
        source=ActionSource.MODEL,
        type="delete_database",
        raw_args={},
        workspace_id="ws-1",
        round=1,
    )

    with pytest.raises(ValidationError):
        registry.normalize(proposal, workspace_path="/workspace")


def test_extra_fields_rejected():
    """Extra fields in args must be rejected"""
    from coding_agent_harness.domain import ActionProposal, ActionSource
    from coding_agent_harness.errors import ValidationError
    from coding_agent_harness.tools import ToolRegistry

    registry = ToolRegistry()
    proposal = ActionProposal(
        action_id="act-1",
        source=ActionSource.MODEL,
        type="shell",
        raw_args={
            "argv": ["pytest"],
            "cwd": ".",
            "extra_field": "not_allowed",
        },
        workspace_id="ws-1",
        round=1,
    )

    with pytest.raises(ValidationError):
        registry.normalize(proposal, workspace_path="/workspace")
