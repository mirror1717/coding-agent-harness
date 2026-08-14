"""Typer CLI for the governed coding agent harness."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

import typer
import yaml  # type: ignore[import-untyped]

from coding_agent_harness.approvals import ApprovalBroker
from coding_agent_harness.credentials import CredentialStore
from coding_agent_harness.errors import HarnessError
from coding_agent_harness.llm import LLM
from coding_agent_harness.runtime import AgentRuntime

app = typer.Typer(
    name="harness",
    help="Governed coding agent harness with Docker sandbox and audit chain.",
    no_args_is_help=True,
)


# ---------------------------------------------------------------------------
# run
# ---------------------------------------------------------------------------


@app.command()
def run(
    workspace: Annotated[Path, typer.Option("--workspace", "-w", help="Workspace path")],
    task: Annotated[str, typer.Option("--task", "-t", help="Task description")],
    policy: Annotated[Path, typer.Option("--policy", help="Policy YAML file")] = Path("config/default-policy.yaml"),
    verification: Annotated[Path, typer.Option("--verification", help="Verification profile YAML")] = Path("config/default-verification.yaml"),
    mockllm: Annotated[bool, typer.Option("--mockllm/--no-mockllm", help="Use MockLLM (default) or real LLM")] = True,
) -> None:
    """Run a coding task with governance and sandbox execution."""
    if not workspace.exists():
        typer.echo(f"Error: workspace not found: {workspace}", err=True)
        raise typer.Exit(code=1)
    workspace = workspace.resolve()
    if not _check_docker():
        typer.echo("Error: Docker not available", err=True)
        raise typer.Exit(code=1)

    if mockllm:
        typer.echo("MockLLM mode requires a script. Use 'harness demo <fixture>' for scripted demos.")
        typer.echo("For real LLM, configure credentials with 'harness credentials set openai' then use --no-mockllm.")
        raise typer.Exit(code=0)

    store = _get_credential_store()
    if store is None:
        typer.echo("Real LLM is unavailable: no usable keyring backend.", err=True)
        typer.echo("Configure one with:")
        typer.echo("  harness credentials set openai")
        typer.echo("or use MockLLM:")
        typer.echo("  harness run ... --mockllm")
        raise typer.Exit(code=1)

    try:
        store.status("openai")
    except HarnessError:
        typer.echo("Real LLM is unavailable: no credential configured for 'openai'.", err=True)
        typer.echo("Configure one with:")
        typer.echo("  harness credentials set openai")
        typer.echo("or use MockLLM:")
        typer.echo("  harness run ... --mockllm")
        raise typer.Exit(code=1)

    from coding_agent_harness.domain import TaskRequest

    try:
        runtime = _build_runtime(
            workspace=workspace,
            policy_path=policy,
            verification_path=verification,
            use_mockllm=False,
        )
        task_req = TaskRequest(
            task_id="task-1",
            prompt=task,
            workspace_id="ws-1",
            policy_version="1",
            verification_profile_id="default",
        )
        result = asyncio.run(runtime.run(task_req))
        typer.echo(f"Terminal state: {result.terminal_state}")
        typer.echo(f"Reason: {result.terminal_reason}")
    except HarnessError as e:
        typer.echo(f"Error: {e.code}: {e.message}", err=True)
        raise typer.Exit(code=1)


# ---------------------------------------------------------------------------
# doctor
# ---------------------------------------------------------------------------


@app.command()
def doctor(
    workspace: Annotated[Path, typer.Option("--workspace", "-w")] = Path("."),
) -> None:
    """Pre-check Docker, policy, profile, and keyring before running."""
    required_checks = [
        ("Docker", _check_docker()),
        ("Policy file", _check_file(Path("config/default-policy.yaml"))),
        ("Verification file", _check_file(Path("config/default-verification.yaml"))),
        ("Workspace", _check_workspace(workspace)),
    ]
    keyring_ok = _check_keyring()

    all_required = True
    for name, ok in required_checks:
        status = "OK" if ok else "FAIL"
        typer.echo(f"  {name}: {status}")
        if not ok:
            all_required = False

    if keyring_ok:
        typer.echo("  Keyring: OK")
    else:
        typer.echo("  Keyring: WARN (real LLM unavailable; MockLLM demos remain usable)")

    if not all_required:
        raise typer.Exit(code=1)


# ---------------------------------------------------------------------------
# demo
# ---------------------------------------------------------------------------


@app.command()
def demo(
    fixture_dir: Annotated[Path, typer.Argument(help="Demo fixture directory")],
) -> None:
    """Run a deterministic demo from a fixture directory."""
    manifest = fixture_dir / "manifest.yaml"
    if not manifest.exists():
        typer.echo(f"Error: manifest not found: {manifest}", err=True)
        raise typer.Exit(code=1)

    data = yaml.safe_load(manifest.read_text())
    name = data.get("name", "unknown")
    typer.echo(f"Demo: {name}")

    script_data = data.get("mockllm_script", [])
    if not script_data:
        typer.echo("No mockllm_script in manifest, nothing to run.")
        return

    if not _check_docker():
        typer.echo("Error: Docker not available — demo requires Docker sandbox", err=True)
        raise typer.Exit(code=1)

    from coding_agent_harness.approvals import (
        ApprovalBroker,
        ApprovalDecision,
        ApprovalScript,
        ScriptedApprover,
    )
    from coding_agent_harness.domain import (
        ActionProposal,
        ActionSource,
        ModelDecision,
        TaskRequest,
    )
    from coding_agent_harness.llm import MockLLM

    decisions: list[ModelDecision] = []
    for entry in script_data:
        kind = entry.get("kind", "message")
        if kind == "action":
            action_data = entry["action"]
            decisions.append(ModelDecision(
                kind="action",
                action=ActionProposal(
                    action_id=action_data.get("action_id", ""),
                    source=ActionSource(action_data.get("source", "MODEL")),
                    type=action_data.get("type", ""),
                    raw_args=action_data.get("raw_args", {}),
                    workspace_id=action_data.get("workspace_id", "ws-1"),
                    round=action_data.get("round", 1),
                ),
            ))
        elif kind == "finish":
            decisions.append(ModelDecision(kind="finish"))
        else:
            decisions.append(ModelDecision(kind="message", message=entry.get("message", "")))

    approvals_data = data.get("approvals", [])
    approval_decisions: list[ApprovalDecision] = []
    edited_args: dict[str, object] = {}
    for approval in approvals_data:
        decision = ApprovalDecision(approval["decision"])
        approval_decisions.append(decision)
        if "edited_args" in approval:
            edited_args = approval["edited_args"]

    workspace = (fixture_dir / "workspace").resolve()
    if not workspace.exists():
        workspace.mkdir(parents=True)

    _clean_workspace(workspace)

    llm = MockLLM(decisions)
    if approval_decisions:
        broker = ApprovalBroker(
            approver=ScriptedApprover(ApprovalScript(
                decisions=approval_decisions,
                edited_args=edited_args or None,
            )),
            clock=lambda: datetime.now(tz=UTC),
        )
    else:
        broker = ApprovalBroker(
            approver=ScriptedApprover(
                ApprovalScript(decisions=[ApprovalDecision.APPROVE_ONCE] * 100)
            ),
            clock=lambda: datetime.now(tz=UTC),
        )

    try:
        runtime = _build_runtime(
            workspace=workspace,
            policy_path=Path("config/default-policy.yaml"),
            verification_path=Path("config/default-verification.yaml"),
            use_mockllm=True,
            llm=llm,
            approval_broker=broker,
        )
        task_req = TaskRequest(
            task_id=name,
            prompt=data.get("description", ""),
            workspace_id="ws-1",
            policy_version="1",
            verification_profile_id="default",
        )
        result = asyncio.run(runtime.run(task_req))
        typer.echo(f"Terminal state: {result.terminal_state}")
        typer.echo(f"Reason: {result.terminal_reason}")

        audit_path = workspace / "audit.jsonl"
        if audit_path.exists():
            typer.echo(f"Audit log: {audit_path}")
    except HarnessError as e:
        typer.echo(f"Error: {e.code}: {e.message}", err=True)
        raise typer.Exit(code=1)


# ---------------------------------------------------------------------------
# credentials
# ---------------------------------------------------------------------------


credentials_app = typer.Typer(name="credentials", help="Manage API credentials")
app.add_typer(credentials_app, name="credentials")


def _get_credential_store() -> CredentialStore | None:
    """Return a CredentialStore backed by the system keyring, or None if unavailable."""
    try:
        import keyring

        from coding_agent_harness.credentials import CredentialStore

        backend = keyring.get_keyring()
        if backend.__class__.__module__ == "keyring.backends.fail":
            return None
        return CredentialStore(backend, now_utc=lambda: datetime.now(tz=UTC).isoformat())
    except ImportError:
        return None


@credentials_app.command("status")
def credentials_status(
    provider: Annotated[str, typer.Argument()] = "openai",
) -> None:
    """Show credential configuration status (no secrets)."""
    store = _get_credential_store()
    if store is None:
        typer.echo(f"Provider: {provider}")
        typer.echo("Status: keyring unavailable")
        return
    try:
        status = store.status(provider)
        typer.echo(f"Provider: {status.provider}")
        typer.echo(f"Configured: {status.configured}")
        if status.endpoint:
            typer.echo(f"Endpoint: {status.endpoint}")
        if status.model:
            typer.echo(f"Model: {status.model}")
        if status.updated_at:
            typer.echo(f"Updated: {status.updated_at}")
    except HarnessError as e:
        typer.echo(f"Error: {e.message}", err=True)
        raise typer.Exit(code=1)


@credentials_app.command("set")
def credentials_set(
    provider: Annotated[str, typer.Argument()] = "openai",
) -> None:
    """Interactively set API key (hidden input)."""
    key = typer.prompt("API Key", hide_input=True)
    if not key.strip():
        typer.echo("Error: empty key", err=True)
        raise typer.Exit(code=1)
    store = _get_credential_store()
    if store is None:
        typer.echo("Error: keyring unavailable — cannot persist credential", err=True)
        raise typer.Exit(code=1)
    try:
        store.set(provider, key)
        typer.echo(f"Key set for {provider}")
    except HarnessError as e:
        typer.echo(f"Error: {e.message}", err=True)
        raise typer.Exit(code=1)


@credentials_app.command("update")
def credentials_update(
    provider: Annotated[str, typer.Argument()] = "openai",
) -> None:
    """Interactively update API key (hidden input)."""
    key = typer.prompt("New API Key", hide_input=True)
    if not key.strip():
        typer.echo("Error: empty key", err=True)
        raise typer.Exit(code=1)
    store = _get_credential_store()
    if store is None:
        typer.echo("Error: keyring unavailable — cannot update credential", err=True)
        raise typer.Exit(code=1)
    try:
        store.update(provider, key)
        typer.echo(f"Key updated for {provider}")
    except HarnessError as e:
        typer.echo(f"Error: {e.message}", err=True)
        raise typer.Exit(code=1)


@credentials_app.command("clear")
def credentials_clear(
    provider: Annotated[str, typer.Argument()] = "openai",
) -> None:
    """Clear stored credentials."""
    store = _get_credential_store()
    if store is None:
        typer.echo("Error: keyring unavailable — cannot clear credential", err=True)
        raise typer.Exit(code=1)
    try:
        store.clear(provider)
        typer.echo(f"Cleared {provider}")
    except HarnessError as e:
        typer.echo(f"Error: {e.message}", err=True)
        raise typer.Exit(code=1)


# ---------------------------------------------------------------------------
# audit
# ---------------------------------------------------------------------------


audit_app = typer.Typer(name="audit", help="Audit log operations")
app.add_typer(audit_app, name="audit")


@audit_app.command("verify")
def audit_verify(
    audit_file: Annotated[Path, typer.Argument(help="Audit JSONL file")],
) -> None:
    """Verify audit hash chain integrity."""
    if not audit_file.exists():
        typer.echo(f"Error: {audit_file} not found", err=True)
        raise typer.Exit(code=1)

    from coding_agent_harness.audit import AuditLog

    run_id = "verify"
    try:
        with open(audit_file) as f:
            first_line = f.readline().strip()
            if first_line:
                run_id = json.loads(first_line).get("run_id", run_id)
    except (json.JSONDecodeError, OSError):
        pass
    log = AuditLog(audit_file, run_id=run_id)
    try:
        verification = log.verify()
        typer.echo(f"Chain valid: {verification.chain_valid}")
        typer.echo(f"Evidence complete: {verification.evidence_complete}")
        if verification.errors:
            typer.echo(f"Errors: {verification.errors}")
    except (HarnessError, OSError) as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(code=1)


@app.command("summary")
def run_summary(
    audit_file: Annotated[Path, typer.Argument(help="Audit JSONL file")],
) -> None:
    """Generate a human-readable run summary from audit log."""
    if not audit_file.exists():
        typer.echo(f"Error: {audit_file} not found", err=True)
        raise typer.Exit(code=1)

    from coding_agent_harness.audit import AuditLog, build_run_summary

    run_id = "summary"
    try:
        with open(audit_file) as f:
            first_line = f.readline().strip()
            if first_line:
                run_id = json.loads(first_line).get("run_id", run_id)
    except (json.JSONDecodeError, OSError):
        pass
    log = AuditLog(audit_file, run_id=run_id)
    try:
        verification = log.verify()
        summary = build_run_summary(verification)
        typer.echo(str(summary))
    except (HarnessError, OSError) as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(code=1)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _check_docker() -> bool:
    return shutil.which("docker") is not None


def _check_file(path: Path) -> bool:
    return path.exists()


def _check_workspace(path: Path) -> bool:
    return path.exists() and path.is_dir()


def _check_keyring() -> bool:
    try:
        import keyring

        backend = keyring.get_keyring()
        return backend.__class__.__module__ != "keyring.backends.fail"
    except ImportError:
        return False


def _clean_workspace(workspace: Path) -> None:
    """Remove generated files from a previous demo run and fix permissions."""
    for pattern in ("audit.jsonl", "audit.jsonl.anchor", "memory.db"):
        p = workspace / pattern
        if p.exists():
            p.unlink()
    for child in workspace.iterdir():
        if child.is_dir() and child.name.startswith("run-"):
            shutil.rmtree(child, ignore_errors=True)
        if child.is_dir() and child.name == ".harness":
            shutil.rmtree(child, ignore_errors=True)
    os.chmod(workspace, 0o777)
    for child in workspace.iterdir():
        try:
            if child.is_file():
                os.chmod(child, 0o666)
        except OSError:
            pass


def _build_runtime(
    *,
    workspace: Path,
    policy_path: Path,
    verification_path: Path,
    use_mockllm: bool = True,
    llm: LLM | None = None,
    approval_broker: ApprovalBroker | None = None,
) -> AgentRuntime:
    """Wire up all runtime components and return an AgentRuntime."""
    from coding_agent_harness.approvals import (
        ApprovalBroker,
        ApprovalDecision,
        ApprovalScript,
        ScriptedApprover,
    )
    from coding_agent_harness.artifacts import RunArtifactStore
    from coding_agent_harness.audit import AuditLog
    from coding_agent_harness.budgets import BudgetController, BudgetLimits
    from coding_agent_harness.feedback import FeedbackEngine
    from coding_agent_harness.guardrail import EmptySecretDetector, Guardrail
    from coding_agent_harness.llm import MockLLM
    from coding_agent_harness.memory import MemoryStore
    from coding_agent_harness.policy import PolicyEngine
    from coding_agent_harness.runtime import AgentRuntime, RuntimeConfig
    from coding_agent_harness.sandbox import create_default_tools
    from coding_agent_harness.tools import ToolDispatcher, ToolRegistry
    from coding_agent_harness.verification import load_verification_profile

    run_id = f"run-{datetime.now(tz=UTC).strftime('%Y%m%d%H%M%S')}"
    workspace_id = "ws-1"

    artifact_root = workspace / ".harness" / run_id
    artifact_root.mkdir(parents=True, exist_ok=True)

    if llm is None:
        llm = MockLLM([])

    registry = ToolRegistry()
    guardrail = Guardrail(
        secret_detector=EmptySecretDetector(),
        credentials_present=False,
        workspace_id=workspace_id,
    )
    policy_engine = PolicyEngine.from_file(policy_path)
    tools = create_default_tools(workspace, workspace_id)
    dispatcher = ToolDispatcher(tools)
    artifact_store = RunArtifactStore(artifact_root, run_id=run_id)
    feedback = FeedbackEngine()
    memory = MemoryStore(
        workspace / "memory.db",
        clock=lambda: datetime.now(tz=UTC),
        id_factory=lambda: f"mem-{datetime.now(tz=UTC).timestamp()}",
        secret_detector=EmptySecretDetector(),
    )
    audit = AuditLog(workspace / "audit.jsonl", run_id=run_id)
    budget = BudgetController(
        limits=BudgetLimits(
            wall_clock_seconds=300.0,
            sandbox_execution_seconds=120.0,
            approval_timeout_seconds=30.0,
            hitl_wait_seconds=60.0,
            max_rounds=20,
            max_llm_calls=100,
            max_tool_calls=100,
        ),
        clock=_RealClock(),
        run_id=run_id,
    )
    vp = load_verification_profile(verification_path)
    config = RuntimeConfig(
        workspace_path=workspace,
        workspace_id=workspace_id,
        run_id=run_id,
    )

    if approval_broker is None:
        approval_broker = ApprovalBroker(
            approver=ScriptedApprover(
                ApprovalScript(decisions=[ApprovalDecision.APPROVE_ONCE] * 100)
            ),
            clock=lambda: datetime.now(tz=UTC),
        )

    return AgentRuntime(
        llm=llm,  # type: ignore[arg-type]
        registry=registry,
        guardrail=guardrail,
        policy_engine=policy_engine,
        approval_broker=approval_broker,  # type: ignore[arg-type]
        dispatcher=dispatcher,
        artifact_store=artifact_store,
        feedback_engine=feedback,
        memory_store=memory,
        audit_log=audit,
        budget_controller=budget,
        verification_profile=vp,
        config=config,
    )


class _RealClock:
    """Clock backed by datetime.now for budget accounting."""

    def monotonic(self) -> float:
        return datetime.now(tz=UTC).timestamp()

    def now_utc(self) -> datetime:
        return datetime.now(tz=UTC)


if __name__ == "__main__":
    app()
