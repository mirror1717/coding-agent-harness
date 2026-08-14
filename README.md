# Governed Coding Agent Harness

A Python coding agent harness where the runtime — not the model — controls execution. The LLM can only propose structured actions; the harness enforces governance, human approval, Docker sandboxing, feedback-driven repair, and tamper-evident auditing.

## Quick Start

```bash
# Install
pip install -e ".[dev]"

# Pre-check Docker, policy, and keyring
harness doctor

# Run a demo (offline, no API key needed)
harness demo demo/fixtures/governance_hitl

# Verify an audit log
harness audit verify path/to/audit.jsonl
```

## Architecture

```
LLM → ToolRegistry → Guardrail → PolicyEngine → ApprovalBroker → ToolDispatcher → DockerSandbox → FeedbackEngine → AgentRuntime (next round)
```

**AgentRuntime** is the sole flow coordinator. All components are injected; none call each other directly.

### Key Components

| Component | Role |
|---|---|
| **ToolRegistry** | Schema validation, path normalization, argv constraints |
| **Guardrail** | Hard safety boundary (path escape, network, privilege, resource limits) |
| **PolicyEngine** | Declarative YAML risk rules (ALLOW / REQUIRE_APPROVAL / DENY) |
| **ApprovalBroker** | Fingerprint-bound one-time approval, reject, edit-and-execute |
| **DockerSandbox** | network=none, non-root, read-only rootfs, CPU/memory/PID limits |
| **FeedbackEngine** | Parse pytest/shell/lint output, redact secrets, truncate to 64 KiB |
| **VerificationProfile** | System-generated VERIFICATION actions, no bypass |
| **AuditLog** | Append-only JSONL with SHA-256 hash chain |
| **RunArtifactStore** | Private, atomic evidence storage with digest verification |
| **MemoryStore** | Deterministic, rule-based retrieval (no LLM/embedding) |
| **BudgetController** | Independent wall-clock, sandbox, HITL, rounds, LLM/tool budgets |
| **CredentialStore** | System Keyring, no plaintext fallback, no Docker exposure |

## Security Boundaries

- **Hard boundary**: Guardrail checks cannot be overridden by YAML policy or human approval
- **Docker isolation**: All side-effecting tools execute in Docker; no host execution shortcut
- **Structured shell**: `argv` only, no `shell=True`, no `bash -c`
- **Secret protection**: API keys never enter Docker, workspace, audit, or model context
- **Tamper evidence**: Audit hash chain detects modification, deletion, insertion, reordering

## Deterministic Demos

Three offline demos using MockLLM (no network, no API key):

1. **DEMO-1: Governance + HITL** — Risky action → edit → re-govern → approve_once
2. **DEMO-2: Feedback Repair + Verification** — pytest fail → fix → finish → VERIFICATION → PASS → SUCCESS
3. **DEMO-3: Audit Tamper Evidence** — Legal run → tamper → verify failure → SECURITY_STOP

## Audit / Artifact / Memory Separation

| Store | Semantics | Mutability |
|---|---|---|
| **AuditLog** | Facts (what happened) | Append-only, hash-chained |
| **RunArtifactStore** | Evidence (raw output) | Immutable, digest-verified |
| **MemoryStore** | Knowledge (learned patterns) | Mutable, rule-gated |

Memory is never a source of historical facts.

## CI Pipeline

`.gitlab-ci.yml` includes:
- `unit-test`: Ruff + pytest unit tests
- `mockllm-offline-e2e`: Offline MockLLM tests
- `package-build`: Wheel/sdist build + smoke test
- `sandbox-image-build`: Docker image build + verification
- `pipeline-pass`: Final gate (all above must pass)

## Dependencies

- Python >=3.12
- Pydantic >=2.13,<3 | Typer >=0.27.1,<0.28 | Textual >=8.2,<9
- PyYAML >=6.0.3,<7 | openai >=2.54,<3 | keyring >=25.7,<26
- Dev: pytest >=9.1,<10 | pytest-asyncio >=1.4,<2 | ruff >=0.16.3,<0.17 | mypy >=2.3,<3

## Target Platforms

- **Tier 1**: Linux x86_64, Docker Engine, Secret Service
- **Tier 2**: macOS 13+ (Docker Desktop, Keychain), Windows 11 WSL2

## Remaining Risks

- Same host account can access unlocked Keyring
- Key exists in process memory during API calls
- Host/kernel/Docker daemon compromise is out of scope
- Hash chain provides tamper-evidence, not tamper-proofing under full host compromise
