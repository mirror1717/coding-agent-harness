### Agent handoff

- 原主开发 Agent：OpenAI Codex
- 新主开发 Agent：Claude Code
- 原因：原 Agent 使用额度达到限制
- Handoff context：
  - SPEC.md
  - PLAN.md
  - AGENT_LOG.md
  - SPEC_PROCESS.md
  - Git history / worktree / diff / test results
- 约束：
  新 Agent 不重新设计产品，从现有冻结规格与实现状态继续。
- 经验：
  SPEC/PLAN/Git/Agent Log 使开发状态能够跨 Agent 恢复，
  减少对单一会话隐性上下文的依赖。

---

### T01-T05, T13 (Codex → Claude Code handoff)

- Agent: Codex (original), Claude Code (handoff)
- Tasks: T01 domain, T02 canonical, T03 artifacts, T05 tools, T13 budgets/termination
- Status: Completed, merged to feat/harness-implementation
- Commits: 7ab82ce, 5958116, 3a97010, feba572, bc953fe
- Tests: 362 passed

### T04 Audit (fixed by GLM-5.2)

- Agent: GLM-5.2 (opencode)
- Task: T04 AuditLog hash chain
- RED: 3 failures (error message string mismatch: "audit anchor path is not a regular file" vs expected "audit anchor is not a regular file")
- GREEN: Fixed label format, 35 passed
- Commit: 0cc718d
- Lesson: Shared format strings with different labels need consistent message construction

### T06 Guardrail (committed by GLM-5.2)

- Agent: GLM-5.2 (opencode)
- Task: T06 hard boundary guardrail
- Status: Was GREEN but staged, committed and merged
- Tests: 74 passed
- Commit: 2e6f8b5

### T12 Memory (committed by GLM-5.2)

- Agent: GLM-5.2 (opencode)
- Task: T12 deterministic memory store
- Status: Was GREEN but staged, committed and merged
- Tests: 27 passed
- Commit: 2358b1a

### T14 Credentials (committed by GLM-5.2)

- Agent: GLM-5.2 (opencode)
- Task: T14 credential store with keyring
- Status: Was GREEN but staged, committed and merged
- Tests: 19 passed
- Commit: 2f13204

### T07 PolicyEngine (subagent)

- Agent: subagent (general)
- Task: T07 declarative YAML policy engine
- RED: 37 tests failed (ModuleNotFoundError)
- GREEN: 37 passed, 554 full suite
- Commit: 6a2e1a6

### T15 LLM/MockLLM (subagent)

- Agent: subagent (general)
- Task: T15 LLM protocol with deterministic mock
- RED: 22 tests failed (ModuleNotFoundError)
- GREEN: 22 passed, 539 full suite
- Commit: 07d1e7d

### T16 VerificationProfile (subagent)

- Agent: subagent (general)
- Task: T16 verification profile with system actions
- RED: 23 tests failed (ModuleNotFoundError)
- GREEN: 23 passed, 540 full suite
- Commit: 876587d

### T08 ApprovalBroker (GLM-5.2)

- Agent: GLM-5.2 (opencode, direct implementation after subagent returned empty)
- Task: T08 approval broker with fingerprint-bound authorization
- RED: 14 tests written
- GREEN: 14 passed, 613 full suite
- Commit: b1bef51
- Lesson: Subagents may return empty results; have fallback to direct implementation

### T09 Docker command builder (subagent)

- Agent: subagent (general)
- Task: T09 Docker command building and result classification
- RED: 42 tests failed (ModuleNotFoundError)
- GREEN: 42 passed, 641 full suite
- Commit: 95185d7

### T10 DockerSandbox + ToolDispatcher (GLM-5.2)

- Agent: GLM-5.2 (opencode, direct implementation after subagent returned empty)
- Task: T10 Docker sandbox execution and tool dispatcher
- RED: Integration tests failed (permission denied in Docker)
- GREEN: Fixed permissions, 52 tests passed (10 integration + 42 unit)
- Commit: 87cb552
- Lesson: Docker containers running as non-root need world-readable workspace files

### T11 FeedbackEngine (GLM-5.2)

- Agent: GLM-5.2 (opencode, direct implementation after subagent returned empty)
- Task: T11 feedback engine with redaction and truncation
- RED: 14 tests written
- GREEN: 14 passed, 669 full suite
- Commit: f908437

### T17 AgentRuntime (GLM-5.2)

- Agent: GLM-5.2 (opencode, direct implementation)
- Task: T17+T18 agent runtime with governance chain and verification
- RED: 5 tests written
- GREEN: 5 passed, 674 full suite
- Commit: 67b7c14
- Lesson: Missing break statement after success candidate caused infinite loop; always verify control flow

### T19 CLI (GLM-5.2)

- Agent: GLM-5.2 (opencode, direct implementation)
- Task: T19 Typer CLI with doctor, credentials, audit, demo commands
- RED: 11 tests written
- GREEN: 11 passed
- Commit: 9e0e288

### T20 TUI (GLM-5.2)

- Agent: GLM-5.2 (opencode, minimal implementation)
- Task: T20 Textual TUI (minimal)
- Commit: 6ab462e

### T21-T23 Demos (GLM-5.2)

- Agent: GLM-5.2 (opencode, fixture + test creation)
- Task: T21 DEMO-1, T22 DEMO-2, T23 DEMO-3
- Commit: 6ab462e

### T24 CI (GLM-5.2)

- Agent: GLM-5.2 (opencode)
- Task: T24 CI pipeline with unit-test job
- Commit: 6ab462e

### T25 Docs (GLM-5.2)

- Agent: GLM-5.2 (opencode)
- Task: T25 README with run, security, demo instructions
- Commit: 6ab462e
