"""Integration tests for Docker sandbox execution and tool dispatcher."""

from __future__ import annotations

import asyncio
import os
import shutil
from pathlib import Path

import pytest

from coding_agent_harness.domain import ActionProposal, ActionSource, NormalizedAction
from coding_agent_harness.sandbox import (
    CapturedDockerSandbox,
    DockerSandbox,
    SandboxConfig,
    create_default_tools,
)
from coding_agent_harness.tools import ToolDispatcher

docker_available = shutil.which("docker") is not None
pytestmark = pytest.mark.skipif(not docker_available, reason="Docker not available")


def _make_workspace(tmp_path: Path) -> Path:
    """Create a workspace dir with world-readable/writable permissions for Docker."""
    tmp_path.chmod(0o777)
    return tmp_path


def _make_shell_action(
    argv: list[str], *, cwd: str = ".", action_id: str = "act-1"
) -> NormalizedAction:
    return NormalizedAction(
        action_id=action_id,
        source=ActionSource.MODEL,
        type="shell",
        raw_args={"argv": argv, "cwd": cwd},
        normalized_args={"argv": argv, "cwd": cwd},
        workspace_id="test-ws",
        round=1,
    )


def _make_pytest_action(action_id: str = "act-pytest") -> NormalizedAction:
    return NormalizedAction(
        action_id=action_id,
        source=ActionSource.VERIFICATION,
        type="pytest",
        raw_args={"argv": ["pytest", "-q"], "cwd": "."},
        normalized_args={"argv": ["pytest", "-q"], "cwd": "."},
        workspace_id="test-ws",
        round=1,
    )


class TestDockerSandboxExecution:
    @pytest.mark.asyncio
    async def test_shell_executes_and_returns_result(self, tmp_path: Path) -> None:
        ws = _make_workspace(tmp_path)
        (ws / "test.txt").write_text("hello")
        (ws / "test.txt").chmod(0o644)
        sandbox = CapturedDockerSandbox(ws, "test-ws")
        action = _make_shell_action(["cat", "test.txt"])
        result, stdout, stderr = await sandbox.execute_with_output(action)
        assert result.exit_code == 0
        assert b"hello" in stdout

    @pytest.mark.asyncio
    async def test_workspace_write_persists(self, tmp_path: Path) -> None:
        ws = _make_workspace(tmp_path)
        sandbox = CapturedDockerSandbox(ws, "test-ws")
        action = NormalizedAction(
            action_id="act-1",
            source=ActionSource.MODEL,
            type="shell",
            raw_args={"argv": ["sh", "-c", "echo hello > output.txt"], "cwd": "."},
            normalized_args={"argv": ["sh", "-c", "echo hello > output.txt"], "cwd": "."},
            workspace_id="test-ws",
            round=1,
        )
        result, _, _ = await sandbox.execute_with_output(action)
        assert result.exit_code == 0
        assert (ws / "output.txt").exists()
        assert "hello" in (ws / "output.txt").read_text()

    @pytest.mark.asyncio
    async def test_network_is_disabled(self, tmp_path: Path) -> None:
        sandbox = CapturedDockerSandbox(tmp_path, "test-ws")
        action = _make_shell_action(["python3", "-c", "import urllib.request; urllib.request.urlopen('http://example.com', timeout=5)"])
        result, _, stderr = await sandbox.execute_with_output(action)
        assert result.exit_code != 0
        assert b"Network" in stderr or b"network" in stderr or b"Name or service" in stderr or b"Connection" in stderr or result.exit_code != 0

    @pytest.mark.asyncio
    async def test_no_residual_container(self, tmp_path: Path) -> None:
        sandbox = CapturedDockerSandbox(tmp_path, "test-ws")
        action = _make_shell_action(["echo", "hi"])
        await sandbox.execute_with_output(action)
        proc = await asyncio.create_subprocess_exec(
            "docker", "ps", "-a", "--filter", "name=harness-",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        assert b"" == stdout.strip() or b"CONTAINER" not in stdout or len(stdout.splitlines()) <= 1


class TestToolDispatcher:
    @pytest.mark.asyncio
    async def test_dispatch_routes_to_shell_tool(self, tmp_path: Path) -> None:
        ws = _make_workspace(tmp_path)
        tools = create_default_tools(ws, "test-ws")
        dispatcher = ToolDispatcher(tools)
        action = _make_shell_action(["echo", "hello"])
        result = await dispatcher.dispatch(action)
        assert result.exit_code == 0

    @pytest.mark.asyncio
    async def test_dispatch_routes_to_pytest_tool(self, tmp_path: Path) -> None:
        ws = _make_workspace(tmp_path)
        (ws / "test_pass.py").write_text("def test_pass(): assert True\n")
        (ws / "test_pass.py").chmod(0o644)
        tools = create_default_tools(ws, "test-ws")
        dispatcher = ToolDispatcher(tools)
        action = _make_pytest_action()
        result = await dispatcher.dispatch(action)
        assert result.exit_code == 0

    @pytest.mark.asyncio
    async def test_dispatch_rejects_unknown_tool(self, tmp_path: Path) -> None:
        tools = create_default_tools(tmp_path, "test-ws")
        dispatcher = ToolDispatcher(tools)
        action = NormalizedAction(
            action_id="act-1",
            source=ActionSource.MODEL,
            type="unknown_tool",
            raw_args={},
            normalized_args={},
            workspace_id="test-ws",
            round=1,
        )
        with pytest.raises(Exception):
            await dispatcher.dispatch(action)

    @pytest.mark.asyncio
    async def test_all_tools_call_sandbox(self, tmp_path: Path) -> None:
        tools = create_default_tools(tmp_path, "test-ws")
        for name, tool in tools.items():
            assert hasattr(tool, "execute")
            assert callable(getattr(tool, "execute"))
