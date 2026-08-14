# 受治编码代理框架

一个 Python 编码代理框架，由运行时而非模型控制执行。LLM 只能提出结构化动作；框架强制执行治理、人工审批、Docker 沙箱、反馈驱动修复和防篡改审计。

## 快速开始

```bash
# 安装
pip install -e ".[dev]"

# 构建 Docker 沙箱镜像
docker build -t coding-agent-harness-sandbox:latest sandbox/

# 1. 预检（Docker、策略、密钥环）
harness doctor

# 2. 运行离线演示（需要 Docker，无需 API Key）
harness demo demo/fixtures/governance_hitl
# 输出示例：
#   Demo: governance-hitl
#   Terminal state: TerminalState.SUCCESS
#   Reason: all required checks passed
#   Audit log: <path>/workspace/audit.jsonl

# 3. 验证审计链 + 生成摘要（使用 demo 输出的 Audit log 路径）
harness audit verify <path>/workspace/audit.jsonl
harness summary <path>/workspace/audit.jsonl

# 4. 运行真实 LLM 任务（需要凭据）
harness credentials status openai       # 检查凭据状态
harness credentials set openai          # 交互式输入 API Key（隐藏输入）
harness run --workspace ./myproject --task "修复测试" --no-mockllm

# 5. 离线 MockLLM 模式（默认，需要脚本，建议用 demo）
harness run --workspace ./myproject --task "修复测试" --mockllm
```

> **注意**：`harness run` 默认使用 MockLLM。要使用真实 LLM，请加 `--no-mockllm`。
> MockLLM 模式需要脚本驱动，建议使用 `harness demo` 运行确定性演示。

## 架构

```
LLM → ToolRegistry → Guardrail → PolicyEngine → ApprovalBroker → ToolDispatcher → DockerSandbox → FeedbackEngine → AgentRuntime（下一轮）
```

**AgentRuntime** 是唯一的流程协调者。所有组件均为注入式，组件之间不直接互调。

### 核心组件

| 组件 | 职责 |
|---|---|
| **ToolRegistry** | 模式校验、路径规范化、argv 约束 |
| **Guardrail** | 硬安全边界（路径逃逸、网络、特权、资源限制） |
| **PolicyEngine** | 声明式 YAML 风险规则（ALLOW / REQUIRE_APPROVAL / DENY） |
| **ApprovalBroker** | 基于指纹绑定的一次性审批、拒绝、编辑后执行 |
| **DockerSandbox** | network=none、非 root、只读根文件系统、CPU/内存/PID 限制 |
| **FeedbackEngine** | 解析 pytest/shell/lint 输出、脱敏、截断至 64 KiB |
| **VerificationProfile** | 系统生成的 VERIFICATION 动作，无旁路 |
| **AuditLog** | 仅追加的 JSONL，带 SHA-256 哈希链 |
| **RunArtifactStore** | 私有、原子化的证据存储，带摘要校验 |
| **MemoryStore** | 确定性、基于规则的检索（无 LLM/embedding） |
| **BudgetController** | 独立的墙钟、沙箱、HITL、轮次、LLM/工具预算 |
| **CredentialStore** | 系统密钥环，无明文回退，不暴露给 Docker |

## 安全边界

- **硬边界**：Guardrail 检查不可被 YAML 策略或人工审批覆盖
- **Docker 隔离**：所有有副作用的工具在 Docker 中执行；无宿主执行捷径
- **结构化 Shell**：仅 `argv`，无 `shell=True`，无 `bash -c`
- **密钥保护**：API Key 绝不进入 Docker、工作区、审计或模型上下文
- **防篡改**：审计哈希链可检测修改、删除、插入、重排

## 确定性演示

三个使用 MockLLM 的离线演示（需要 Docker，无需 API Key）：

1. **演示-1：治理 + HITL** — 风险动作 → 编辑 → 重新治理 → approve_once
2. **演示-2：反馈修复 + 验收** — pytest 失败 → 修复 → finish → VERIFICATION → PASS → SUCCESS
3. **演示-3：审计篡改证据** — 合法运行 → 篡改 → 验证失败 → SECURITY_STOP

## CLI 命令

| 命令 | 说明 |
|---|---|
| `harness doctor` | 预检 Docker、策略文件、验证配置、密钥环 |
| `harness run` | 运行编码任务（默认 MockLLM，`--no-mockllm` 使用真实 LLM） |
| `harness demo <fixture>` | 从 fixture 目录运行确定性演示 |
| `harness credentials set/update/clear/status` | 管理凭据（系统密钥环） |
| `harness audit verify <file>` | 验证审计哈希链完整性 |
| `harness summary <file>` | 生成运行摘要 |

## 审计 / 产物 / 记忆 分离

| 存储 | 语义 | 可变性 |
|---|---|---|
| **AuditLog** | 事实（发生了什么） | 仅追加，哈希链 |
| **RunArtifactStore** | 证据（原始输出） | 不可变，摘要校验 |
| **MemoryStore** | 知识（习得模式） | 可变，规则门控 |

记忆从不作为历史事实的来源。

## CI 流水线

`.github/workflows/ci.yml` 包含：
- `unit-test`：Ruff + mypy + pytest 单元测试
- `mockllm-offline-e2e`：离线 MockLLM 测试
- `package-build`：Wheel/sdist 构建 + 冒烟测试
- `sandbox-image-build`：Docker 镜像构建 + 验证
- `pipeline-pass`：最终门禁（以上全部必须通过）

## 依赖

- Python >=3.12
- Pydantic >=2.13,<3 | Typer >=0.27.1,<0.28 | Textual >=8.2,<9
- PyYAML >=6.0.3,<7 | openai >=2.54,<3 | keyring >=25.7,<26
- 开发：pytest >=9.1,<10 | pytest-asyncio >=1.4,<2 | ruff >=0.16.3,<0.17 | mypy >=2.3,<3

## 目标平台

- **一级**：Linux x86_64，Docker Engine，Secret Service
- **二级**：macOS 13+（Docker Desktop，Keychain），Windows 11 WSL2

## 残余风险

- 同一宿主账户可访问已解锁的密钥环
- API 调用期间密钥存在于进程内存中
- 宿主机/内核/Docker 守护进程被攻破不在范围内
- 哈希链提供防篡改证据，而非宿主机完全被攻破下的防篡改保护
