"""Integration tests for the tool dispatcher with real Docker."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from coding_agent_harness.domain import ActionSource, NormalizedAction
from coding_agent_harness.sandbox import create_default_tools
from coding_agent_harness.tools import ToolDispatcher

docker_available = shutil.which("docker") is not None
pytestmark = pytest.mark.skipif(not docker_available, reason="Docker not available")


class TestToolDispatcherIntegration:
    @pytest.mark.asyncio
    async def test_shell_tool_executes(self, tmp_path: Path) -> None:
        tmp_path.chmod(0o755)
        tools = create_default_tools(tmp_path, "test-ws")
        dispatcher = ToolDispatcher(tools)
        action = NormalizedAction(
            action_id="act-1",
            source=ActionSource.MODEL,
            type="shell",
            raw_args={"argv": ["echo", "test"], "cwd": "."},
            normalized_args={"argv": ["echo", "test"], "cwd": "."},
            workspace_id="test-ws",
            round=1,
        )
        result = await dispatcher.dispatch(action)
        assert result.exit_code == 0

    @pytest.mark.asyncio
    async def test_pytest_tool_executes(self, tmp_path: Path) -> None:
        tmp_path.chmod(0o755)
        (tmp_path / "test_app.py").write_text("def test_ok(): assert True\n")
        (tmp_path / "test_app.py").chmod(0o644)
        tools = create_default_tools(tmp_path, "test-ws")
        dispatcher = ToolDispatcher(tools)
        action = NormalizedAction(
            action_id="act-1",
            source=ActionSource.VERIFICATION,
            type="pytest",
            raw_args={"argv": ["pytest", "-q"], "cwd": "."},
            normalized_args={"argv": ["pytest", "-q"], "cwd": "."},
            workspace_id="test-ws",
            round=1,
        )
        result = await dispatcher.dispatch(action)
        assert result.exit_code == 0
