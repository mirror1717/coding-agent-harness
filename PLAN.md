# Governed Coding Agent Harness 实施计划

> **执行要求：** 每个 Task 由一个 subagent 在一次会话内按 TDD 完成，并经过独立审查。实施时使用 `superpowers:subagent-driven-development`（推荐）或 `superpowers:executing-plans`。不得跳过失败测试、验证或依赖门禁。

## 1. 目标与全局约束

目标：实现冻结版 [SPEC.md](./SPEC.md) 中的 Python Coding Agent Harness。`AgentRuntime` 是唯一流程协调者；所有可执行 Action 都走 ToolRegistry → Guardrail → PolicyEngine → 必要 ApprovalBroker → ToolDispatcher → DockerSandbox；Governance/HITL、沙箱、确定性反馈和 tamper-evident 审计是主要贡献。

技术栈：Python 3.12+、Pydantic v2、Typer、Textual、PyYAML safe loader、OpenAI Python SDK adapter、keyring、SQLite、Docker CLI structured argv、pytest、GitHub Actions、PyPI/pipx、OCI image。

全局约束：

- 首版仅支持 Python + pytest、单机单用户单任务、CLI/TUI；不增加 WebUI 或多语言层。
- ShellAction 只接受结构化 `argv`、workspace-relative `cwd`、受限 `env/stdin/timeout`，使用 `shell=False` 等价语义。
- MODEL、HUMAN_EDIT、VERIFICATION Action 不存在治理或 Docker 旁路。
- hard safety boundary 不能被 YAML 或人工批准覆盖。
- AuditLog、RunArtifactStore、MemoryStore 分别表示事实事件、原始证据和可变推导知识，不得混用。
- API key 只存系统 Keyring，不进入 workspace、Docker、Audit、Artifact、Memory 或模型上下文。
- `SECURITY_STOP` 的终止优先级最高。
- required AcceptanceCheck 全部通过且存在 finish 请求，才能 `SUCCESS`。
- 核心测试必须可通过 deterministic MockLLM 离线运行。
- 不修改无关用户改动；尤其保留现有 `.gitignore`、`README.md`、`AGENT_LOG.md` 内容。

## 2. 目标文件结构

```text
pyproject.toml
src/coding_agent_harness/
  __init__.py
  domain.py
  errors.py
  canonical.py
  artifacts.py
  audit.py
  tools.py
  guardrail.py
  policy.py
  approvals.py
  sandbox.py
  feedback.py
  memory.py
  budgets.py
  termination.py
  credentials.py
  llm.py
  verification.py
  runtime.py
  cli.py
  tui.py
config/default-policy.yaml
config/default-verification.yaml
sandbox/Dockerfile
sandbox/image-policy.json
demo/fixtures/{governance_hitl,feedback_verification,audit_tamper}/
tests/{unit,integration,e2e}/
.github/workflows/ci.yml
docs/acceptance-traceability.md
```

## 3. 依赖 DAG 与 Worktree 并行策略

```text
T01
├─ T02 ─┬─ T04 ───────────────────────────────┐
│       └─ T08                                │
├─ T03 ─── T04 ─ T11                         │
├─ T05 ─┬─ T06 ─ T09 ─ T10 ─ T11             │
│       ├─ T07 ─ T08                          │
│       └─ T16                                │
├─ T12                                         ├─ T17 ─ T18 ─┬─ T19 ─ T20
├─ T13                                         │             │
├─ T14 ─ T15                                   │             ├─ T21
└──────────────────────────────────────────────┘             ├─ T22
                                                             └─ T23
T20 + T21 + T22 + T23 ─ T24 ─ T25
```

推荐并行 wave：

| Wave | 可并行 Task | 合并门禁 |
|---|---|---|
| W1 | T01 | 包可导入，领域模型测试通过 |
| W2 | T02、T03、T05、T12、T13、T14 | 各 worktree 只改各自列出的文件；全部基于 T01 |
| W3 | T04、T06、T07、T15、T16 | 分别先合并其依赖；T06/T07 共享 T05 但文件不重叠 |
| W4 | T08、T09 | T08 依赖 T02/T07；T09 依赖 T05/T06 |
| W5 | T10 | 真实 Docker 隔离门禁 |
| W6 | T11 | Artifact + Sandbox 结果契约门禁 |
| W7 | T17 | 汇总 T01–T16，建立唯一主循环 |
| W8 | T18 | 完成失败、预算与安全终止路径 |
| W9 | T19、T21、T22、T23 | T19 实现 CLI；三个 demo 使用独立 fixture/test 文件，可与之并行，但最终在 T24 前基于合并后的 CLI 复验 |
| W10 | T20 | 依赖 T19，单独实现 Textual TUI，避免与 CLI 文件冲突 |
| W11 | T24 | 汇总所有产品与 demo 任务 |
| W12 | T25 | 最终 AC-1–AC-18 追踪与发行验证 |

Worktree 规则：并行 subagent 不得修改未列在自己 Task 中的文件；发现接口不够时先报告，由主分支在依赖 Task 中统一修订。`pyproject.toml` 由 T01 首建、T24 最终调整；`runtime.py` 仅由 T17/T18 串行修改；`cli.py` 仅由 T19 修改；`tui.py` 仅由 T20 修改。

## 4. 共享接口契约

```python
class LLM(Protocol):
    async def decide(self, context: AgentContext) -> ModelDecision: ...

class ApprovalPort(Protocol):
    async def decide(self, request: ApprovalRequest) -> ApprovalOutcome: ...

class Sandbox(Protocol):
    async def execute(self, action: NormalizedAction) -> RawExecutionResult: ...
    async def cancel(self, execution_id: str) -> None: ...

class Clock(Protocol):
    def monotonic(self) -> float: ...
    def now_utc(self) -> datetime: ...

class IdGenerator(Protocol):
    def new(self, namespace: str) -> str: ...
```

公开 Pydantic 事实模型使用 `ConfigDict(extra="forbid", frozen=True)`。若 Task 发现必须调整公开字段，必须同时更新直接依赖测试，并由主 agent 判断是否需要重排 DAG。

---

## T01：包骨架与核心领域模型

**状态：** ✅ 完成 — implementation commit `7ab82ce82b1467bfa37b1191cb6b6d4febe12a69`

**目标：** 建立可安装的 src-layout 包、稳定错误码和后续任务共享的不可变领域类型。

**依赖与并行：** 无依赖；必须首先完成，不与其他任务并行。

**涉及文件：**

- 新建 `pyproject.toml`
- 新建 `src/coding_agent_harness/__init__.py`
- 新建 `src/coding_agent_harness/domain.py`
- 新建 `src/coding_agent_harness/errors.py`
- 新建 `tests/unit/test_domain.py`

**预期实现要点：**

- 定义 `ActionSource`、`ActionProposal`、`NormalizedAction`、`RuntimeState`、`TerminalState`、`ModelDecision`、`RawExecutionResult`、`StructuredFeedback`、`TaskRequest`、`RunResult`。
- 定义 `HarnessError(code, message)` 及 validation/configuration/security/evidence 子类。
- `pyproject.toml` 固定 Python `>=3.12`，声明 runtime/dev 依赖范围、pytest asyncio mode、ruff/mypy 和 `harness` entry point。
- Runtime dependencies 必须使用：`pydantic>=2.13,<3`、`typer>=0.27.1,<0.28`、`textual>=8.2,<9`、`PyYAML>=6.0.3,<7`、`openai>=2.54,<3`、`keyring>=25.7,<26`。
- Development/test dependencies 必须使用：`pytest>=9.1,<10`、`pytest-asyncio>=1.4,<2`、`ruff>=0.16.3,<0.17`、`mypy>=2.3,<3`。
- 所有依赖必须有上界；Typer 与 Ruff 使用窄 minor-version 上界。OpenAI SDK 首版锁定 2.x API 契约。若 resolver 报告冲突，停止并保留 resolver 输出，不得擅自放宽上界。
- 生成并提交选定解析器的锁文件；CI 后续从锁文件安装。

**验证步骤：**

1. 先写失败测试 `test_models_forbid_extra_fields`、`test_fact_models_are_frozen`、`test_terminal_state_contains_security_stop`，运行 `pytest tests/unit/test_domain.py -q`，预期因包/类型不存在而 FAIL。
2. 实现最小领域模型后重跑同一命令，预期 PASS。
3. 运行 `python -c "import coding_agent_harness"`，预期退出码 0。
4. 运行 `ruff check src/coding_agent_harness/domain.py tests/unit/test_domain.py`，预期 PASS。

## T02：Canonical JSON 与版本化审批指纹

**状态：** ✅ 完成 — implementation commit `9833c8d701724760447f9ea37f8870970f179aac`（integration commit `5958116`）

**目标：** 实现稳定 canonical serialization 和不会因字符串边界混淆而误授权的 fingerprint。

**依赖与并行：** 依赖 T01；可与 T03、T05、T12、T13、T14 并行。

**涉及文件：**

- 新建 `src/coding_agent_harness/canonical.py`
- 新建 `tests/unit/test_canonical.py`

**预期实现要点：**

- `canonical_json_bytes(value)` 使用 UTF-8、排序 key、紧凑分隔符、禁止 NaN/Infinity。
- `approval_fingerprint(action, version=1)` 对 `version/action_type/normalized_args/workspace_id` canonical JSON 对象计算 SHA-256。
- version 不支持时 fail closed；不使用简单字符串拼接。

**验证步骤：**

1. 写失败测试 `test_fingerprint_preserves_field_boundaries`：`["ab","c"]` 与 `["a","bc"]` 的指纹不同；写 `test_key_order_is_canonical` 和 `test_nan_is_rejected`。
2. 运行 `pytest tests/unit/test_canonical.py -q`，预期因模块不存在 FAIL。
3. 实现后重跑，预期 PASS；再运行测试两次确认相同输入得到相同 digest。

## T03：RunArtifactStore

**目标：** 为 stdout、stderr、pytest report 和 diff 提供私有、原子、run-local 的证据存储。

**依赖与并行：** 依赖 T01；可与 T02、T05、T12、T13、T14 并行。

**涉及文件：**

- 新建 `src/coding_agent_harness/artifacts.py`
- 新建 `tests/unit/test_artifacts.py`

**预期实现要点：**

- 实现 `ArtifactRef` 和 descriptor-backed `RunArtifactStore.put/read/verify`；不公开 `path_for()`，业务组件不得绕过 Store 用普通 `Path` 进行证据 I/O。
- 若提供诊断位置，只能返回明确标记 diagnostic-only 的字符串；T04/T11 等消费者只持有 ArtifactRef，并通过 Store 读取/验证。
- run 目录权限 `0700`、artifact `0600`；同目录 temp + fsync + `os.replace` 原子写入。
- Harness 生成 storage key，拒绝 caller path；限制单 artifact、单 run 总大小/数量。
- digest mismatch、missing、越界引用使用稳定状态/错误码。

**验证步骤：**

1. 写失败测试 `test_put_is_private_and_atomic`、`test_digest_mismatch_detected`、`test_missing_artifact_reported`、`test_storage_key_cannot_escape_run`、`test_store_exposes_no_public_path_capability`。
2. 运行 `pytest tests/unit/test_artifacts.py -q`，预期 FAIL。
3. 实现后重跑，预期 PASS；用 `pytest ... --basetemp` 确认测试不写 workspace 外固定路径。

## T04：AuditLog 哈希链与确定性摘要

**目标：** 保存 append-only 事实事件，并检测修改、删除、插入、重排及 artifact 证据缺失。

**依赖与并行：** 依赖 T02、T03；W3 内可与 T06、T07、T15、T16 并行。

**涉及文件：**

- 新建 `src/coding_agent_harness/audit.py`
- 新建 `tests/unit/test_audit.py`

**预期实现要点：**

- `event_hash = SHA-256(prev_hash + canonical_json(event_without_event_hash))`，固定 genesis、严格递增 seq。
- 每次 append flush + fsync 后才返回；事件内联 ArtifactRef/digest，不内联 bytes。
- `verify()` 分离 `chain_valid` 与 `evidence_complete`。
- `build_run_summary()` 只消费已验证事件，不读取 Memory 补事实。

**验证步骤：**

1. 写失败测试 `test_valid_chain_builds_summary`，以及 mutation/delete/insert/reorder 四个篡改测试。
2. 写失败测试 `test_missing_artifact_keeps_chain_valid_but_evidence_incomplete`。
3. 运行 `pytest tests/unit/test_audit.py -q`，预期 FAIL；实现后预期全部 PASS。
4. 运行 `pytest tests/unit/test_artifacts.py tests/unit/test_audit.py -q`，确认联合契约 PASS。

## T05：ToolRegistry 与结构化 Action

**目标：** 定义首版工具 schema，并把不可信 ActionProposal 确定性规范化为 NormalizedAction。

**依赖与并行：** 依赖 T01；可与 T02、T03、T12、T13、T14 并行。

**涉及文件：**

- 新建 `src/coding_agent_harness/tools.py`
- 新建 `tests/unit/test_tools.py`

**预期实现要点：**

- 注册 `list_files/read_file/write_file/shell/pytest`，未知工具和额外字段拒绝。
- ShellArgs 只含非空 `argv`、workspace-relative `cwd`、allowlisted `env`、有上限的 `stdin/timeout_seconds`。
- T05 只校验 env key allowlist 与 stdin/timeout 大小，不判断载荷是否包含宿主 secret；secret containment 是 T06 不可覆盖的 hard boundary。
- 拒绝 `command` string、`bash -c`、`sh -c`、PowerShell string 等解释器求值模式。
- canonical path/cwd 必须在 workspace 内，并阻止符号链接逃逸。
- 定义 Tool Protocol 与 ToolDispatcher，但本任务不实现 Docker。

**验证步骤：**

1. 写失败参数化测试，覆盖合法 argv、空 argv、shell string、解释器 `-c`、`../`、绝对路径、symlink escape、非法 env、超大 stdin。
2. 运行 `pytest tests/unit/test_tools.py -q`，预期 FAIL。
3. 实现后重跑，预期 PASS；运行 `mypy src/coding_agent_harness/tools.py` 验证 Protocol。

## T06：不可覆盖的 Guardrail

**目标：** 编码 workspace、挂载、网络、特权和资源 hard boundary，并区分正常拒绝与边界完整性故障。

**依赖与并行：** 依赖 T05；可与 T04、T07、T15、T16 并行。

**涉及文件：**

- 新建 `src/coding_agent_harness/guardrail.py`
- 新建 `sandbox/image-policy.json`
- 新建 `tests/unit/test_guardrail.py`

**预期实现要点：**

- `Guardrail.check(NormalizedAction)` 是纯函数式检查，返回 PASS 或稳定 DENY reason。
- hard maxima 由 package-owned 配置加载，Policy/HITL 无覆盖入口。
- 注入 `SecretDetector` Protocol（`contains_secret(value: str | bytes) -> bool`），对所有可能进入 Docker 的 env value、stdin 等载荷做 exact-secret containment 检查；命中返回稳定 `GUARDRAIL_DENY` reason，YAML/HITL 不可覆盖。
- T06 不导入 T14。无真实凭据时注入 `EmptySecretDetector`；系统存在应保护凭据但无法建立 detector 时 fail closed。首版不得自行增加 token regex。
- `assert_enforcement(sandbox_metadata)` 发现实际网络/挂载/特权/资源约束失效时抛 `BoundaryIntegrityError`。

**验证步骤：**

1. 写失败表驱动测试，覆盖 path escape、Docker socket、network、privileged、额外挂载、超限资源、shell interpreter eval，以及 FakeSecretDetector 命中的 env/stdin。
2. 写 `test_empty_secret_detector_allows_nonempty_payload` 与 `test_missing_required_detector_fails_closed`。
3. 写 `test_policy_like_override_is_not_accepted` 与 `test_observed_boundary_failure_is_security_error`。
4. 运行 `pytest tests/unit/test_guardrail.py -q`，预期 FAIL；实现后预期 PASS。

## T07：声明式 YAML PolicyEngine

**目标：** 实现严格 schema、安全默认值、可解释风险和确定性规则优先级。

**依赖与并行：** 依赖 T05；可与 T04、T06、T15、T16 并行。

**涉及文件：**

- 新建 `src/coding_agent_harness/policy.py`
- 新建 `config/default-policy.yaml`
- 新建 `tests/unit/test_policy.py`

**预期实现要点：**

- `yaml.safe_load` + strict Pydantic schema，拒绝未知字段/重复 rule ID。
- 匹配只看 NormalizedAction；结果含 decision/risk/rule_id/reason/version。
- 先 specificity，再以 `DENY > REQUIRE_APPROVAL > ALLOW` 解决同分冲突。
- 默认：安全读取 ALLOW；ordinary write/shell REQUIRE_APPROVAL；secret/destructive DENY；required verification pytest ALLOW。

**验证步骤：**

1. 写失败测试覆盖 invalid YAML/schema、no match、同分冲突、source-aware verification、write/shell/secret path。
2. 运行 `pytest tests/unit/test_policy.py -q`，预期 FAIL。
3. 实现后重跑，预期 PASS；将规则顺序打乱后再测，语义结果应保持确定性。

## T08：ApprovalBroker 与最小授权

**目标：** 实现 approve_once、reject、edit 的类型化结果，Broker 自身不具备执行权。

**依赖与并行：** 依赖 T02、T07；与 T09 可并行。

**涉及文件：**

- 新建 `src/coding_agent_harness/approvals.py`
- 新建 `tests/unit/test_approvals.py`

**预期实现要点：**

- ApprovalRequest 展示 fingerprint version、规范化参数、workspace、风险、规则与超时。
- approve_once 绑定 action ID + versioned canonical fingerprint。
- EDIT 产生新 ID、`source=HUMAN_EDIT`、parent_action_id，只返回 ActionProposal；不调用 Registry/Policy/Dispatcher/Sandbox。
- 单次 timeout 返回稳定拒绝原因并报告 wait duration。

**验证步骤：**

1. 写失败测试 `test_approve_once_binds_exact_fingerprint`、`test_edit_creates_new_proposal`、`test_timeout_rejects_without_execution`、`test_broker_has_no_dispatch_dependency`。
2. 运行 `pytest tests/unit/test_approvals.py -q`，预期 FAIL；实现后预期 PASS。
3. 联合运行 `pytest tests/unit/test_canonical.py tests/unit/test_policy.py tests/unit/test_approvals.py -q`。

## T09：Docker 命令构建与结果分类

**目标：** 在不启动真实容器的单元层构建受控 Docker argv，并分类 timeout/OOM/exit。

**依赖与并行：** 依赖 T05、T06；可与 T08 并行；T10 的前置。

**涉及文件：**

- 新建 `src/coding_agent_harness/sandbox.py`
- 新建 `sandbox/Dockerfile`
- 新建 `tests/unit/test_sandbox_command.py`

**预期实现要点：**

- 使用 `asyncio.create_subprocess_exec(*argv)`；代码中禁止 `create_subprocess_shell`。
- argv 固定包含 `--rm --network=none --read-only --cap-drop=ALL --security-opt=no-new-privileges`、非 root、tmpfs、CPU/memory/PID/timeout 和唯一 workspace rw mount。
- 不接受 caller-supplied mounts/network/privileged；不挂载 Docker socket或凭据。
- 生成稳定 `RawExecutionResult` outcome。

**验证步骤：**

1. 写失败测试 `test_builder_enforces_all_flags`、`test_only_workspace_mount_exists`、`test_shell_api_never_used`、`test_timeout_and_oom_classification`。
2. 运行 `pytest tests/unit/test_sandbox_command.py -q`，预期 FAIL。
3. 实现后重跑，预期 PASS；运行 `rg -n 'create_subprocess_shell|shell=True' src/coding_agent_harness/sandbox.py`，预期无匹配。

## T10：DockerSandbox 真实隔离与 ToolDispatcher

**目标：** 用真实 Docker 证明隔离边界，并完成 Tool 到 Sandbox 的唯一执行分发。

**依赖与并行：** 依赖 T09；单独执行，避免共享 Docker 资源与镜像 tag 冲突。

**涉及文件：**

- 修改 `src/coding_agent_harness/sandbox.py`
- 修改 `src/coding_agent_harness/tools.py`
- 新建 `tests/integration/test_docker_sandbox.py`
- 新建 `tests/integration/test_tool_dispatcher.py`

**预期实现要点：**

- 每个 Action 使用唯一命名临时容器；完成/超时/中断后清理。
- 实现文件、shell、pytest Tool，全部只调用 Sandbox Protocol。
- Dispatcher 只接收已规范化 Action，不实现治理判断。
- 返回 stdout/stderr bytes 与观察到的 sandbox metadata，供 Artifact/Guardrail 后验验证。

**验证步骤：**

1. 先写失败集成测试：workspace 写入可持久化、host path/socket 不可访问、网络失败、PID/timeout 生效、无残留容器、所有 Tool 调用 Sandbox spy。
2. 运行 `docker build -t coding-agent-harness-sandbox:test sandbox`，预期镜像构建成功。
3. 运行 `pytest tests/integration/test_docker_sandbox.py tests/integration/test_tool_dispatcher.py -q`，初次预期 FAIL；实现后预期 PASS。

## T11：FeedbackEngine 与统一脱敏

**目标：** 从 Artifact 解析 pytest/shell/lint 反馈，输出有界、脱敏、确定性的 StructuredFeedback。

**依赖与并行：** 依赖 T03、T10；单独执行。

**涉及文件：**

- 新建 `src/coding_agent_harness/feedback.py`
- 新建 `tests/unit/test_feedback.py`

**预期实现要点：**

- 只通过 RunArtifactStore 读取原始 bytes；解析 exit code、失败测试、位置、错误签名。
- exact secret + 保守 token pattern 双重 redaction。
- serialized model-visible feedback 不超过 65,536 bytes，截断显式标记。
- 未知/二进制/解析失败使用 `parse_error=True` 降级，不丢 ArtifactRef。

**验证步骤：**

1. 写失败 fixture 测试：pytest failure、SyntaxError、lint、未知 bytes、>64 KiB、fake token。
2. 运行 `pytest tests/unit/test_feedback.py -q`，预期 FAIL；实现后预期 PASS。
3. 联合运行 `pytest tests/unit/test_artifacts.py tests/unit/test_feedback.py -q`，确认 artifact digest 引用保留。

## T12：确定性 MemoryStore

**目标：** 用 SQLite 保存受提炼规则约束的长期知识，并实现不依赖 LLM/embedding 的有限检索。

**依赖与并行：** 依赖 T01；W2 可与 T02、T03、T05、T13、T14 并行。

**涉及文件：**

- 新建 `src/coding_agent_harness/memory.py`
- 新建 `tests/unit/test_memory.py`

**预期实现要点：**

- `consider()` 仅接受 confirmed convention、repeated failure、verified fix、explicit human save。
- 普通 stdout、单次失败、模型猜测、secret 候选返回 NO_UPDATE。
- search：workspace/kind 过滤；token/tag、evidence、updated_at 整数计分；`score DESC, updated_at DESC, memory_id ASC`；top_k + char_limit。
- 只用 `sqlite3` 参数化 SQL，不 import LLM/vector/HTTP。

**验证步骤：**

1. 写失败测试覆盖四种允许提炼、四种拒绝、稳定 tie-break、top_k/char_limit、workspace 隔离。
2. 运行 `pytest tests/unit/test_memory.py -q`，预期 FAIL；实现后预期 PASS。
3. 固定 clock/ID 连续运行同一检索三次，预期 ordered memory IDs 完全一致。

## T13：BudgetController、NoProgress 与终止优先级

**状态：** ✅ 完成 — implementation commit `f0cb4fe`（integration commit `bc953fe`）

**目标：** 独立核算预算，并集中裁决唯一终止状态。

**依赖与并行：** 依赖 T01；W2 可与 T02、T03、T05、T12、T14 并行。

**涉及文件：**

- 新建 `src/coding_agent_harness/budgets.py`
- 新建 `src/coding_agent_harness/termination.py`
- 新建 `tests/unit/test_budgets.py`
- 新建 `tests/unit/test_termination.py`

**预期实现要点：**

- 分离 wall-clock、sandbox execution、per-approval、cumulative HITL wait、round、LLM/tool calls。
- 等待审批消耗 wall/HITL，不消耗 sandbox execution。
- NoProgress 识别重复 fingerprint 与等价 error signature。
- `choose_terminal()` 明确 `SECURITY_STOP > all`；SUCCESS 只能由 verified success candidate 提供。

**验证步骤：**

1. 写失败 fake-clock 测试覆盖每类预算的独立变化与运行中不可上调。
2. 写失败测试同时输入 SUCCESS/BUDGET/SANDBOX/SECURITY 候选，预期 SECURITY_STOP。
3. 运行 `pytest tests/unit/test_budgets.py tests/unit/test_termination.py -q`，初次 FAIL；实现后 PASS。

## T14：CredentialStore

**目标：** 通过系统 Keyring 安全完成 key 的录入、状态、更新、读取供客户端使用和清除。

**依赖与并行：** 依赖 T01；W2 可与 T02、T03、T05、T12、T13 并行。

**涉及文件：**

- 新建 `src/coding_agent_harness/credentials.py`
- 新建 `tests/unit/test_credentials.py`

**预期实现要点：**

- Keyring backend 作为注入接口；service=`coding-agent-harness/<provider>`。
- status 仅返回 configured/endpoint/model/updated metadata，不返回 secret。
- backend 不可用时 fail closed；不创建 `.env`、JSON 或其他明文 fallback。
- 提供统一 exact-secret redaction 输入给 LLM/Feedback/UI 层。
- 提供基于宿主已知 exact secret literals 的 `SecretDetector` adapter，供 T06 通过 Protocol 注入；不让 Guardrail 直接依赖 CredentialStore，也不引入 token regex。

**验证步骤：**

1. 写失败 fake-keyring 测试覆盖 set/status/update/get_for_client/clear/backend locked。
2. 写 `test_no_plaintext_fallback_created`、`test_status_and_exception_do_not_contain_key` 和 exact-literal detector adapter 测试。
3. 运行 `pytest tests/unit/test_credentials.py -q`，初次 FAIL；实现后 PASS。

## T15：MockLLM 与 OpenAI-compatible Adapter

**目标：** 提供统一 LLM Protocol、deterministic MockLLM 和真实 provider adapter，模型不拥有工具能力。

**依赖与并行：** 依赖 T01、T14；W3 可与 T04、T06、T07、T16 并行。

**涉及文件：**

- 新建 `src/coding_agent_harness/llm.py`
- 新建 `tests/unit/test_llm.py`

**预期实现要点：**

- `LLM.decide(AgentContext) -> ModelDecision`；Context 只含有界 task/memory/feedback/schema/budget summary。
- MockLLM 按脚本顺序返回，耗尽时稳定失败；不读取网络或 Keyring。
- OpenAI adapter 从 CredentialStore 取 key，映射 structured output；只分类错误，不自行 sleep/retry。
- 对 provider exception 做 exact-secret redaction。

**验证步骤：**

1. 写失败测试 `test_mockllm_replays_exact_sequence`、`test_mockllm_exhaustion_is_typed`、`test_provider_maps_structured_decision`、`test_provider_error_is_redacted`。
2. 运行 `pytest tests/unit/test_llm.py -q`，预期 FAIL；实现后 PASS。
3. 在移除常见 API key 环境变量后重跑 MockLLM 测试，预期仍 PASS 且无网络调用。

## T16：VerificationProfile 与系统 Action

**目标：** 把 finish 转成 `source=VERIFICATION` 的 required/optional AcceptanceCheck Action。

**依赖与并行：** 依赖 T01、T05；W3 可与 T04、T06、T07、T15 并行。

**涉及文件：**

- 新建 `src/coding_agent_harness/verification.py`
- 新建 `config/default-verification.yaml`
- 新建 `tests/unit/test_verification.py`

**预期实现要点：**

- profile strict schema，至少一个 required check；默认 required pytest 使用 structured argv。
- `create_verification_actions()` 只创建 ActionProposal，不执行、不治理。
- missing/failed required check 不能产生 success candidate；optional failure 只产生 warning。

**验证步骤：**

1. 写失败测试：无 required check 拒绝、默认 pytest required、generated source=VERIFICATION、required missing/fail 拒绝成功。
2. 运行 `pytest tests/unit/test_verification.py -q`，初次 FAIL；实现后 PASS。
3. 将生成 Action 送入 T05 Registry 单测 fixture，确认按普通 Action 正常规范化。

## T17：AgentRuntime 主路径与调用顺序

**目标：** 实现唯一状态协调者的 happy path、拒绝路径和人工编辑重入，不在本任务加入复杂恢复逻辑。

**依赖与并行：** 依赖 T04、T08、T11、T12、T13、T15、T16 及其传递依赖；不得并行修改 `runtime.py`。

**涉及文件：**

- 新建 `src/coding_agent_harness/runtime.py`
- 新建 `tests/unit/test_runtime_order.py`

**预期实现要点：**

- constructor 注入所有 ports；组件间不互调。
- 可执行 Action 顺序固定：Registry → Guardrail → Policy → Approval if needed → audit pre-exec → Dispatcher → Artifact persist → audit result → Feedback → conditional Memory。
- Guardrail deny 在 Policy 前停止；Policy deny 在 Approval/Dispatcher 前停止。
- EDIT 创建新 proposal 并回到 Registry；ALLOW 仍进 Dispatcher/Sandbox。
- 状态变化前先 append 对应事件。

**验证步骤：**

1. 写失败 spy 测试 `test_allow_action_uses_exact_order`、`test_guardrail_deny_stops_before_policy`、`test_policy_deny_stops_before_approval`、`test_edit_restarts_at_registry`、`test_audit_append_precedes_state_change`。
2. 运行 `pytest tests/unit/test_runtime_order.py -q`，预期 FAIL。
3. 实现最小状态循环后重跑，预期 PASS；检查所有 spy 中只有 Runtime 发起跨组件调用。

## T18：AgentRuntime 反馈循环、验收、恢复与停机

**目标：** 在 T17 主循环上完成 feedback-driven repair、系统验收、预算、重试、取消和终止优先级。

**依赖与并行：** 依赖 T17；必须串行修改 `runtime.py`。

**涉及文件：**

- 修改 `src/coding_agent_harness/runtime.py`
- 新建 `tests/integration/test_runtime_scenarios.py`

**预期实现要点：**

- validation/deny/execution/required-check failure 形成下一轮 context；Memory 仅在 consider 规则命中时更新。
- finish 生成 Verification Action 并完整重走治理链。
- transient model error 使用注入 sleeper 有限重试并计预算。
- Audit/artifact/boundary integrity fault 立即禁止后续 Action，并以 SECURITY_STOP 覆盖同时发生的预算/普通失败。
- Ctrl-C/abort 调用 Sandbox.cancel，若无更高安全故障则 HUMAN_ABORTED。

**验证步骤：**

1. 写失败场景测试：required check fail→继续修复、all required pass→SUCCESS、approval timeout、model retry exhausted、no progress、wall/sandbox/HITL budget、abort。
2. 写 `test_integrity_fault_plus_budget_exhaustion_is_security_stop` 和 `test_verification_has_no_bypass`。
3. 运行 `pytest tests/integration/test_runtime_scenarios.py -q`，初次 FAIL；实现后 PASS。
4. 联合运行 `pytest tests/unit/test_runtime_order.py tests/integration/test_runtime_scenarios.py -q`，预期 PASS。

## T19：Typer CLI 与通用 Demo Runner

**目标：** 提供任务、doctor、credentials、audit、summary 和 demo 命令的薄适配器。

**依赖与并行：** 依赖 T18、T14、T04；可与 T21–T23 并行，因为 demo task 不修改 `cli.py`。

**涉及文件：**

- 新建 `src/coding_agent_harness/cli.py`
- 新建 `tests/unit/test_cli.py`

**预期实现要点：**

- 命令：`run`、`doctor`、`credentials set/status/update/clear`、`audit verify`、`run summary`、`demo <fixture-dir>`。
- doctor 在 LLM 前预检 workspace/Docker/policy/profile/keyring。
- credential 输入使用 hidden prompt，不接受 key 参数。
- demo runner 读取 fixture manifest/MockLLM script/approval script，不硬编码三个 demo 的业务流程。
- CLI 只装配依赖并调用 Runtime，不做治理判断。

**验证步骤：**

1. 写失败 CliRunner 测试：doctor fail-before-model、secret hidden/status redacted、audit chain/evidence 分开显示、demo manifest 调用 Runtime fake。
2. 运行 `pytest tests/unit/test_cli.py -q`，初次 FAIL；实现后 PASS。
3. 运行 `python -m coding_agent_harness.cli --help`，预期列出所有命令。

## T20：Textual HITL TUI

**目标：** 实现只负责展示事件与收集审批的 Textual UI，不承载治理或执行规则。

**依赖与并行：** 依赖 T19、T08；单独修改 `tui.py`，可在 T21–T23 完成后并行复验，但不要改 `cli.py`。

**涉及文件：**

- 新建 `src/coding_agent_harness/tui.py`
- 新建 `tests/unit/test_tui.py`

**预期实现要点：**

- 展示 Action source/type/normalized args/workspace/risk/rule/reason/fingerprint version/timeout。
- 仅返回 approve_once、reject、edit、abort 类型化输入。
- 显示状态/预算/反馈摘要；默认不显示 sensitive raw artifact。
- Ctrl-C 发送 abort 请求，不直接 kill 宿主进程或执行容器命令。

**验证步骤：**

1. 写失败 Textual `run_test()`：审批字段完整、快捷键映射正确、edited args 返回数据、secret/raw artifact 不显示。
2. 运行 `pytest tests/unit/test_tui.py -q`，初次 FAIL；实现后 PASS。
3. 向教师确认课程是否强制 WebUI；若是，停止并发起独立 presentation-adapter 变更，不扩张本冻结计划。

## T21：DEMO-1 Governance + HITL

**目标：** 离线展示低风险自动执行、危险动作编辑、新 ID 重审和 approve_once 最小授权。

**依赖与并行：** 依赖 T18；可与 T19、T22、T23 并行；只改本 demo 目录和测试。

**涉及文件：**

- 新建 `demo/fixtures/governance_hitl/manifest.yaml`
- 新建 `demo/fixtures/governance_hitl/mockllm.json`
- 新建 `demo/fixtures/governance_hitl/approvals.json`
- 新建 `demo/fixtures/governance_hitl/workspace/`
- 新建 `tests/e2e/test_demo_governance_hitl.py`

**预期实现要点：**

- 脚本顺序：read ALLOW → risky structured Action → edit_and_execute → new Action re-govern → approve_once → execute。
- 固定 clock/ID；断言 old/new canonical fingerprint 不同，原授权不可复用。
- semantic event fixture 不比较非确定性 Docker ID/真实耗时。

**验证步骤：**

1. 先写失败 E2E `test_governance_hitl_demo_is_deterministic`，预期 fixture/runner 缺失 FAIL。
2. 创建 fixture 后运行 `pytest tests/e2e/test_demo_governance_hitl.py -q` 两次，预期均 PASS 且 normalized event sequence 相同。
3. T19 合并后运行 `harness demo demo/fixtures/governance_hitl`，预期退出码 0。

## T22：DEMO-2 Feedback Repair + Verification

**目标：** 离线展示 pytest 失败、artifact 解析、代码修复、finish 和无旁路系统验收。

**依赖与并行：** 依赖 T18；可与 T19、T21、T23 并行；只改本 demo 目录和测试。

**涉及文件：**

- 新建 `demo/fixtures/feedback_verification/manifest.yaml`
- 新建 `demo/fixtures/feedback_verification/mockllm.json`
- 新建 `demo/fixtures/feedback_verification/workspace/app.py`
- 新建 `demo/fixtures/feedback_verification/workspace/test_app.py`
- 新建 `tests/e2e/test_demo_feedback_verification.py`

**预期实现要点：**

- 初始测试确定性失败；MockLLM pytest → consume feedback → write fix → finish。
- required check 生成 source=VERIFICATION Action 并完整经过 Registry/Guardrail/Policy/Dispatcher/Docker。
- 只有该 check PASS 才 SUCCESS。

**验证步骤：**

1. 写失败 E2E `test_feedback_verification_demo_requires_system_check`。
2. 运行 `pytest tests/e2e/test_demo_feedback_verification.py -q`，初次 FAIL；fixture 完成后连续运行两次均 PASS。
3. T19 合并后运行对应 `harness demo`，断言 Audit 中存在 VERIFICATION 全链事件。

## T23：DEMO-3 Audit/Artifact Tamper Evidence

**目标：** 展示链篡改、证据缺失、digest mismatch，以及 SECURITY_STOP 对预算状态的覆盖。

**依赖与并行：** 依赖 T18；可与 T19、T21、T22 并行；只改本 demo 目录和测试。

**涉及文件：**

- 新建 `demo/fixtures/audit_tamper/manifest.yaml`
- 新建 `demo/fixtures/audit_tamper/mockllm.json`
- 新建 `demo/fixtures/audit_tamper/workspace/`
- 新建 `tests/e2e/test_demo_audit_tamper.py`

**预期实现要点：**

- 先产生合法 run，再复制证据并分别修改 JSONL、删除 artifact、修改 artifact bytes。
- 预期分别为 chain invalid、evidence_missing、digest_mismatch。
- active run 同时注入 integrity fault 与 budget exhaustion，最终 SECURITY_STOP。

**验证步骤：**

1. 写失败 E2E 覆盖四种结果和稳定 reason code。
2. 运行 `pytest tests/e2e/test_demo_audit_tamper.py -q`，初次 FAIL；fixture 完成后 PASS。
3. T19 合并后运行对应 `harness demo`，预期无需网络/key，输出四项预期结果。

## T24：CI、包构建与 Sandbox 镜像 Gate

**目标：** 建立 unit、离线 MockLLM、Python package、Sandbox image 和最终 pipeline PASS 五个必需 job。

**依赖与并行：** 依赖 T20–T23；单独整合，避免并行修改 `pyproject.toml` 和 workflow。

**涉及文件：**

- 新建 `.github/workflows/ci.yml`
- 新建 `scripts/check_offline.py`
- 新建 `scripts/verify_image_policy.py`
- 新建 `tests/integration/test_installed_cli.py`
- 修改 `pyproject.toml`

**预期实现要点：**

- `unit-tests`：ruff/mypy + unit/component tests。
- `mockllm-offline-e2e`：移除 provider secrets，禁止公网依赖，运行三个 demo。
- `package-build`：wheel/sdist + clean venv install/help/doctor smoke。
- `sandbox-image-build`：构建 image，运行 non-root/network/resource/pytest smoke。
- `pipeline-pass` 显式 `needs` 前四项，任一失败不得 PASS。
- 上传 wheel/sdist SHA-256；release 记录 OCI repo digest；fork PR 不获取 secret。

**验证步骤：**

1. 先写失败 `test_installed_wheel_exposes_cli`，在未配置完整 package metadata 时运行并确认 FAIL。
2. 实现 package metadata 后运行 `python -m build` 和该测试，预期 PASS。
3. 运行 `env -u OPENAI_API_KEY pytest tests/unit tests/integration tests/e2e -q`，预期 PASS。
4. 运行 `docker build -t coding-agent-harness-sandbox:ci sandbox` 与 `python scripts/verify_image_policy.py`，预期 PASS。
5. 用 workflow linter 或 GitHub Actions 验证五个 job；最终 `pipeline-pass` 必须 PASS。

## T25：文档、AC 追踪与发行候选验证

**目标：** 将 SPEC AC-1–AC-18 映射到客观测试/CI/demo 证据并完成最终离线发行验证。

**依赖与并行：** 依赖 T24；最后执行，不并行。

**涉及文件：**

- 新建 `docs/acceptance-traceability.md`
- 修改 `README.md`（保留已有用户内容）

**预期实现要点：**

- 每条 AC 列出精确 pytest node ID、demo 命令和 CI job；无通过证据不得标完成。
- README 记录 pipx/Docker/Keyring 设置、目标平台、三个 demo、威胁边界、Audit/Artifact/Memory 区别和 tamper-evidence 限制。
- 明确 CLI/TUI 与可能的课程 WebUI rubric 冲突及教师确认结果。
- 记录 wheel/sdist checksum、Sandbox image digest 和 pipeline run ID。

**验证步骤：**

1. 先写一个 traceability completeness 检查（可内联小脚本或 pytest），在矩阵为空时确认 FAIL；补齐 AC-1–AC-18 后 PASS。
2. 运行安全聚焦测试：`pytest tests/unit/test_guardrail.py tests/unit/test_policy.py tests/unit/test_approvals.py tests/unit/test_audit.py tests/unit/test_artifacts.py tests/unit/test_termination.py -q`。
3. 运行全部离线验收：`env -u OPENAI_API_KEY pytest tests/unit tests/integration tests/e2e -q`。
4. 运行 `python -m build`、`sha256sum dist/*`、Sandbox image smoke 和 `git diff --check`；全部退出码必须为 0。
5. 对照 SPEC 检查 AC-1–AC-18 均有证据，三个 demo 均可通过 CLI 离线执行，才可声明发行候选完成。

## 5. 审查与提交规则

每个 Task 的 subagent 必须按以下顺序交付：

1. 展示新增失败测试及 RED 结果。
2. 实现最小范围代码，禁止顺带重构其他模块。
3. 展示该 Task 的 GREEN 命令和结果。
4. 展示 `git diff --check` 与只包含所列文件的 diff。
5. 由独立 reviewer 检查 SPEC 一致性、治理旁路、secret 泄露与测试质量。
6. review 通过后再提交；commit message 使用 `feat/test/ci/docs: <bounded outcome>`。

任一 Task 若需要修改未声明文件、放宽全局约束或改变公开接口，停止该 worktree，先由主 agent 更新依赖 Task/PLAN；不得由 subagent 自行扩张范围。
