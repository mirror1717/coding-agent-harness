"""Typer CLI for the governed coding agent harness."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

app = typer.Typer(
    name="harness",
    help="Governed coding agent harness with Docker sandbox and audit chain.",
    no_args_is_help=True,
)


@app.command()
def run(
    workspace: Annotated[Path, typer.Option("--workspace", "-w", help="Workspace path")],
    task: Annotated[str, typer.Option("--task", "-t", help="Task description")],
    policy: Annotated[Path, typer.Option("--policy", help="Policy YAML file")] = Path("config/default-policy.yaml"),
    verification: Annotated[Path, typer.Option("--verification", help="Verification profile YAML")] = Path("config/default-verification.yaml"),
    mockllm: Annotated[bool, typer.Option("--mockllm", help="Use MockLLM")] = True,
) -> None:
    """Run a coding task with governance and sandbox execution."""
    typer.echo(f"Workspace: {workspace}")
    typer.echo(f"Task: {task}")
    typer.echo(f"Policy: {policy}")
    typer.echo(f"Verification: {verification}")
    typer.echo(f"MockLLM: {mockllm}")
    typer.echo("Run not fully implemented in this minimal CLI.")


@app.command()
def doctor(
    workspace: Annotated[Path, typer.Option("--workspace", "-w")] = Path("."),
) -> None:
    """Pre-check Docker, policy, profile, and keyring before running."""
    checks = []
    checks.append(("Docker", _check_docker()))
    checks.append(("Policy file", _check_file(Path("config/default-policy.yaml"))))
    checks.append(("Verification file", _check_file(Path("config/default-verification.yaml"))))
    checks.append(("Workspace", _check_workspace(workspace)))
    all_ok = True
    for name, ok in checks:
        status = "OK" if ok else "FAIL"
        typer.echo(f"  {name}: {status}")
        if not ok:
            all_ok = False
    if not all_ok:
        raise typer.Exit(code=1)


@app.command()
def demo(
    fixture_dir: Annotated[Path, typer.Argument(help="Demo fixture directory")],
) -> None:
    """Run a deterministic demo from a fixture directory."""
    manifest = fixture_dir / "manifest.yaml"
    if not manifest.exists():
        typer.echo(f"Error: manifest not found: {manifest}", err=True)
        raise typer.Exit(code=1)
    typer.echo(f"Running demo: {fixture_dir}")
    typer.echo("Demo runner not fully implemented in this minimal CLI.")


credentials_app = typer.Typer(name="credentials", help="Manage API credentials")
app.add_typer(credentials_app, name="credentials")


@credentials_app.command("status")
def credentials_status(
    provider: Annotated[str, typer.Argument()] = "openai",
) -> None:
    """Show credential configuration status (no secrets)."""
    typer.echo(f"Provider: {provider}")
    typer.echo("Status: not configured (minimal CLI)")


@credentials_app.command("set")
def credentials_set(
    provider: Annotated[str, typer.Argument()] = "openai",
) -> None:
    """Interactively set API key (hidden input)."""
    key = typer.prompt("API Key", hide_input=True)
    if not key:
        typer.echo("Error: empty key", err=True)
        raise typer.Exit(code=1)
    typer.echo(f"Key set for {provider} (minimal CLI - not persisted)")


@credentials_app.command("clear")
def credentials_clear(
    provider: Annotated[str, typer.Argument()] = "openai",
) -> None:
    """Clear stored credentials."""
    typer.echo(f"Cleared {provider} (minimal CLI)")


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
    import json
    run_id = "verify"
    try:
        with open(audit_file) as f:
            first_line = f.readline().strip()
            if first_line:
                run_id = json.loads(first_line).get("run_id", run_id)
    except Exception:
        pass
    log = AuditLog(audit_file, run_id=run_id)
    try:
        verification = log.verify()
        typer.echo(f"Chain valid: {verification.chain_valid}")
        typer.echo(f"Evidence complete: {verification.evidence_complete}")
        if verification.errors:
            typer.echo(f"Errors: {verification.errors}")
    except Exception as e:
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
    from coding_agent_harness.audit import AuditLog
    import json
    run_id = "summary"
    try:
        with open(audit_file) as f:
            first_line = f.readline().strip()
            if first_line:
                run_id = json.loads(first_line).get("run_id", run_id)
    except Exception:
        pass
    log = AuditLog(audit_file, run_id=run_id)
    try:
        summary = log.build_run_summary()
        typer.echo(str(summary))
    except Exception as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(code=1)


def _check_docker() -> bool:
    import shutil
    return shutil.which("docker") is not None


def _check_file(path: Path) -> bool:
    return path.exists()


def _check_workspace(path: Path) -> bool:
    return path.exists() and path.is_dir()


if __name__ == "__main__":
    app()
