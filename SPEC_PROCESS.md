# Governed Coding Agent Harness — 规格形成过程记录

版本：初稿  
日期：2026-08-14  
证据范围：本项目真实 brainstorming 对话、冻结的 `SPEC.md` 与当前 `PLAN.md`

## 0. 文档用途与证据约束

本文记录设计如何从初始设想演化为冻结规格，重点保留问题、选择、质疑、取舍与修改原因。它不是产品说明书，也不替代 `SPEC.md` 或 `PLAN.md`。

记录规则：

- 只使用当前对话中实际发生的内容，以及 `SPEC.md`、`PLAN.md` 中可核对的结果。
- 对尚未执行的实验、尚未获得的外部答复和当前上下文中不存在的证据标记 `TODO`。
- 不把 AI 提出的方案写成已经实施的事实。
- “已冻结”指当前 `SPEC.md` 标记的设计状态，不代表代码已经实现或验收通过。

## 1. 初始项目设想

用户最初提出要实现一个 Coding Agent Harness：Agent 能读取和编辑项目文件、运行 shell 和 pytest，并根据测试反馈自动修复代码。与一般“让模型写代码”的项目不同，用户从第一条需求起就明确希望把 Governance / HITL 作为主要贡献。

最初可确认的目标包括：

- Agent 具有项目文件与测试工具能力。
- Harness 能依据测试反馈驱动多轮修复。
- 危险动作需要在执行前治理，而不是执行后告警。
- 必要时暂停并等待人工审批。
- 用沙箱和边界限制 Agent 的行动空间。

当时尚未明确的主要问题包括：

- 项目是研究原型、实用开发工具，还是课程/作品集成果。
- 人工审批是逐操作、阶段式还是风险分级。
- 交互界面采用 WebUI、CLI/TUI 还是 API。
- 模型接入、离线测试和真实模型之间如何解耦。
- 沙箱是否依赖 Docker，以及 Harness 与执行环境如何分界。
- 风险策略、审计、记忆、凭据、反馈和成功判定分别由谁负责。
- “测试通过”是否等于成功，以及预算耗尽、安全异常同时发生时如何裁决。

仓库探索显示，当时项目基本为空：Git 历史只有 `.gitignore` 的初始化提交，`README.md` 与 `AGENT_LOG.md` 尚无已提交实现内容。因此最终架构不是从既有代码重构而来，而是在需求澄清后从零收敛。

## 2. Brainstorming 关键节点

以下按实际对话推进顺序整理。为避免把建议误写为决定，分别记录 AI 提问/方案、用户选择和形成的结果。

### 2.1 项目定位

- AI 给出的选项：研究型原型、可实际使用的开发工具、课程/作品集项目。
- 用户选择：课程/作品集项目（C）。
- 结果：优先控制规模、保证架构和演示完整，并突出 Governance/HITL，而不是追求生产级多用户或广泛语言支持。

### 2.2 人工审批模式

- AI 给出的选项：风险分级审批、关键阶段审批、逐操作审批。
- 用户选择：风险分级审批。
- 用户进一步明确：危险动作执行前拦截，必要时暂停等待人工审批，并用沙箱/边界限制行动空间。
- 结果：形成执行前 Guardrail/Policy/HITL 与执行时 Sandbox 的纵深防御方向。

### 2.3 交互界面

- AI 给出的选项：Web 控制台、CLI/TUI、REST API + 简单 CLI。
- 用户选择：CLI/TUI。
- 后续出现的歧义：用户描述 Docker 时写过“Harness 与 HITL/WebUI 运行在宿主机”。
- AI 追问：这是改为 WebUI、CLI/TUI 为主，还是双界面。
- 用户再次选择：CLI/TUI 为主，不做 WebUI。
- 结果：当前冻结规格只有 CLI/TUI。课程 rubric 是否强制 WebUI 尚未得到教师确认，见第 6 节 TODO。

### 2.4 LLM 边界

- AI 给出的选项：OpenAI-compatible API + Mock、直接调用 Agent CLI、暂不接真实模型。
- 用户选择并细化：统一 LLM 抽象接口，提供 deterministic `MockLLM`；Harness 自己实现主循环、工具调用、反馈、治理和停机，真实模型只是可替换决策组件。
- 结果：LLM 只能提出结构化决策，不能直接执行文件、shell 或 Docker 操作；核心机制验收不依赖真实模型。

### 2.5 执行隔离

- AI 给出的选项：Docker 默认沙箱、本地受限执行、两种都支持。
- 用户选择：Docker 默认沙箱。
- 用户补充：workspace 是唯一指定挂载，默认禁网，并限制 CPU、内存、进程和超时；Guardrail/HITL 负责执行前治理，Docker 负责规则漏判后的纵深防御。
- 结果：首版没有宿主执行或无 Docker 降级路径。

### 2.6 首版领域范围

- AI 给出的选项：仅 Python + pytest、Python 为主并预留适配器、首版多语言。
- 用户选择：仅 Python + pytest。
- 结果：避免把贡献稀释到多语言工程，反馈与验收围绕 Python/pytest 做深。

### 2.7 策略、审批与预算

- 风险策略：用户选择声明式 YAML，并明确 `PolicyLoader`、Schema Validator、`PolicyEngine`、`ALLOW / REQUIRE_APPROVAL / DENY`，以及不可被用户策略覆盖的 hard safety boundary。
- 审批决定：用户选择 `approve_once / reject / edit_and_execute`。人工编辑必须生成新 Action 并重新治理。
- 停机：用户选择多重预算，而不是只限制轮次或每轮都人工决定。
- 结果：Policy 决策可在 MockLLM 下确定性单测；授权采取最小范围语义；LLM 无权延长预算。

### 2.8 审计

- AI 给出的选项：JSONL + hash chain、普通 JSONL、SQLite 事件库。
- 用户选择：append-only JSONL + hash chain。
- 用户明确边界：它提供 tamper-evidence，不是在宿主完全失陷时的绝对不可篡改保证。
- 结果：事实事件、哈希链校验和确定性 RunSummary 成为核心验收内容。

### 2.9 总体架构方案

- AI 提出三种方案：模块化单体 + 显式状态机、事件溯源架构、宿主 Orchestrator + 独立 Worker 服务。
- AI 推荐：模块化单体 + 显式状态机。
- 用户选择：该方案。
- 结果：`AgentRuntime` 成为唯一流程协调者；事件日志保留审计用途，但不承担完整运行时状态数据库职责。

### 2.10 规格与计划成形

- AI 最初按 Superpowers 默认路径写设计文档，并提出较早版本的实施计划。
- 用户要求最终设计文件命名为根目录 `SPEC.md`，并明确必须覆盖问题陈述、用户故事、模块规约、NFR、架构、数据模型、凭据与分发、技术选型、验收、风险、领域机制等内容。
- 用户随后要求计划命名为根目录 `PLAN.md`，每个 Task 可由单个 subagent 一次完成，并标记依赖和 worktree 并行性。
- 结果：当前 `SPEC.md` 冻结为 18 条验收标准；`PLAN.md` 保留 T01–T25。

## 3. 关键设计迭代

### 3.1 迭代一：从“工具调用”到“唯一治理链”

**原始设想**

早期目标只是让 Agent 读取/编辑文件、运行 shell/pytest，并根据反馈修复；治理强调危险动作前拦截，但工具定义、分发与执行职责尚未完全分离。

**AI 提出的质疑或替代方案**

AI 在首版总体架构中提出 `ToolRegistry`、`Guardrail`、`PolicyEngine`、`ApprovalBroker` 和 `DockerSandbox`，并让 `AgentRuntime` 作为协调者。该版本尚未单列 ToolDispatcher，也没有把 Feedback、Memory 和 Credential 独立成完整组件。

**用户判断**

用户认为总体方向可以保留，但要求：

- `ToolRegistry` 只负责 Action 定义和参数规范化。
- 新增 `ToolDispatcher` 负责路由到具体 Tool，再交给 DockerSandbox。
- `AgentRuntime` 保持唯一协调者，其他组件不直接相互控制。
- 人工修改 Action 后必须重新从规范化开始走全链。

**最终修改**

冻结数据流明确为：LLM 提议 → ToolRegistry 规范化 → Guardrail → PolicyEngine → 必要 ApprovalBroker → ToolDispatcher → DockerSandbox → FeedbackEngine。所有 `ALLOW`、人工批准和 `source=VERIFICATION` Action 都不能绕过 Dispatcher/Sandbox。

**修改原因**

把“定义/规范化”“治理判断”“分发”“执行”拆开后，每个单元可确定性测试，也更容易证明没有宿主快捷路径。人工编辑重新治理避免旧授权被扩大到新参数。

### 3.2 迭代二：从原始测试输出到结构化 Feedback 与有限 Memory

**原始设想**

最初只有“根据 pytest 反馈自动修复”的功能描述，尚未定义原始输出如何解析、截断、回灌，以及历史经验是否长期保存。

**AI 提出的质疑或替代方案**

AI 初版组件列表没有独立 FeedbackEngine/MemoryStore；用户指出 pytest、shell、lint 原始结果不能直接无限注入模型上下文，需要结构化反馈，并需要一个轻量记忆层。

**用户判断**

用户要求：

- 新增 `FeedbackEngine`，把原始结果解析为退出码、失败测试、错误位置、摘要等结构化反馈。
- 新增 `MemoryStore` 保存项目约定、历史决策、已知失败和已验证经验，但按需检索而非全量加载。
- Memory 不能把所有执行结果无差别写入；只有明确提炼规则命中时更新。
- Memory 与 Audit 语义必须分离：前者可整理，后者保存事实。

**最终修改**

SPEC 将 Feedback、Memory、Audit 分为独立组件。Feedback 有长度上限、解析失败标记和原始证据引用；Memory 只接受已确认约定、重复失败、验证成功经验或人工明确保存，并采用确定性有限检索规则，不依赖 LLM/embedding。

后续又增加 `RunArtifactStore`，用于保存 stdout/stderr、pytest report 和 diff 等 run-local 证据，Audit 只保存 ArtifactRef 与 digest。

**修改原因**

结构化反馈控制上下文和噪声；有限 Memory 避免把瞬时错误固化为长期事实；Artifact/Audit/Memory 三分法使“大体积证据”“不可改写事实”“可变推导知识”各自有明确边界。

**尚未完成的过程修订**

最近一次用户要求将统一 secret redaction 前移到 stdout/stderr 持久化之前，使 RunArtifactStore 保存 redacted raw evidence。核对当前文件后确认该修订尚未一致落地：`SPEC.md` 的非功能需求写有“日志、异常和反馈在落盘及显示前”脱敏，但主数据流仍是先持久化 Artifact、再由 FeedbackEngine 脱敏；`PLAN.md` 也仍让 FeedbackEngine 从 RunArtifactStore 读取原始 bytes 后脱敏。`TODO`：统一修改 SPEC/PLAN，使持久化前 redaction 成为明确步骤，并保留修订 diff 证据。

### 3.3 迭代三：从“pytest 通过即成功”到系统强制验收

**原始设想**

AI 在早期停机设计中把 `SUCCESS` 描述为最终 pytest 在沙箱中通过。

**AI 提出的质疑或替代方案**

AI 原方案用 pytest 作为固定成功条件，优点是简单，但把领域默认工具与成功语义硬编码在一起。

**用户判断**

用户明确反对硬编码：应增加可配置 `VerificationProfile / AcceptanceChecks`。只有 finish 条件满足且所有 required checks 通过才能成功；默认 required check 可以是 pytest。

用户随后补充：由 VerificationProfile 触发的检查必须生成 `source=VERIFICATION` 系统 Action，并完整经过 ToolRegistry、Guardrail、PolicyEngine、必要审批、ToolDispatcher 和 DockerSandbox，不设置安全旁路。

**最终修改**

冻结规格把模型的 `finish` 视为“请求验收”，而不是成功声明。Harness 创建系统 Action，执行 required/optional checks；required 失败回到修复循环，全部 required 通过后才产生 success candidate。

**修改原因**

这样能阻止模型虚假完成，也能在不改变 Runtime 成功语义的情况下更换验收命令。系统验收仍受相同治理，避免“内部动作比模型动作更可信”的错误假设。

### 3.4 迭代四：从单一预算到独立时间语义与终止优先级

**原始设想**

早期停机标准是测试通过或多重预算耗尽，但不同等待时间与执行时间尚未严格区分，终止条件同时出现时也没有明确优先级。

**AI 提出的质疑或替代方案**

AI 提出了最大轮次、工具调用、总时长、连续失败等多重预算，并把审批等待从执行时长中排除。

**用户判断**

用户要求进一步区分：

- wall-clock 总任务预算；
- Sandbox execution budget；
- 单次 approval timeout；
- 累计 HITL wait budget。

同时要求 hard boundary 或 AuditLog 完整性异常必须以 `SECURITY_STOP` 为最终状态，不能被普通预算或失败状态覆盖。

**最终修改**

SPEC 和 PLAN 将预算独立核算，并设置集中终止裁决器。`SECURITY_STOP` 显式高于其他所有状态；审批等待计入 wall/HITL，但不计入 Sandbox execution。

**修改原因**

独立预算让演示和测试能准确解释资源耗在哪里；安全异常最高优先级保证最终摘要不会把隔离失效误报成普通超时、预算耗尽甚至成功。

### 3.5 迭代五：从普通 JSONL 到 hash chain 与 Artifact 证据

**原始设想**

早期只有“记录工具调用和测试反馈”的一般日志需求。

**AI 提出的质疑或替代方案**

AI 给出普通 JSONL、带 hash chain 的 JSONL、SQLite 事件库三种选择，并推荐 hash chain 方案。

**用户判断**

用户选择 append-only JSONL + hash chain，并明确要通过修改/删除日志验证链失败。同时拒绝把它描述为宿主完全失陷下的绝对不可篡改。

后续用户指出 ExecutionResult/StructuredFeedback 只有 stdout/stderr/raw 引用，却没有原始产物存储定义，因此要求增加 RunArtifactStore 或等价 run-local storage。

**最终修改**

AuditEvent 维护 `prev_hash/event_hash`，RunSummary 从合法事件流确定性生成；RunArtifactStore 保存大体积运行证据，Audit 记录引用与 digest；缺失 artifact 与链篡改分别报告。

**修改原因**

hash chain 使基本篡改可检测，ArtifactStore 补上引用的实际落点；两者分开避免把大输出内联到 JSONL，同时保留证据完整性检查。

## 4. AI 建议的采纳与拒绝

### 4.1 有代表性的采纳

#### 模块化单体 + 显式状态机

AI 比较了模块化单体、事件溯源和独立 Worker 服务。用户采纳模块化单体，因为它在作品集范围内能保持边界清晰、测试确定、部署简单；事件溯源和 RPC Worker 会把主要工作转移到基础设施，而不是 Governance/HITL。

#### CLI/TUI 而非 WebUI

AI 推荐 CLI/TUI 作为课程/作品集的可控范围方案，用户两次确认。该选择减少前后端和远程审批复杂度，使治理事件、风险和审批仍能在终端完整展示。

注意：这不是对课程最终 rubric 的确认。是否允许豁免 WebUI 仍是 TODO。

#### Docker 默认沙箱

AI 推荐 Docker 作为默认执行边界，用户采纳并强化资源、网络与挂载限制。选择理由是前置规则不可能覆盖全部命令语义，需要执行层纵深防御；同时不实现本地无 Docker 降级，避免形成旁路。

#### YAML 策略与 deterministic MockLLM

AI 推荐声明式策略和可替换 Mock；用户采纳并把它们提升为核心可验收机制。工程收益是策略决策、Agent 主循环和治理状态可以离线复现，不受模型费用、网络和随机性影响。

### 4.2 被拒绝或被显著修改的建议

#### 拒绝事件溯源与独立 Worker 作为首版架构

它们不是技术上无效，而是与课程/作品集的范围不匹配。事件重放、RPC、Worker 生命周期和一致性会稀释治理贡献，因此保留普通运行时状态 + append-only 审计，而不把 Audit 当数据库。

#### 修改“pytest 通过即 SUCCESS”

AI 的早期定义过于具体。用户改为 VerificationProfile + required AcceptanceChecks，并要求系统 Action 也完整治理。这一修改牺牲少量实现简单性，换来成功语义可配置、不可由模型自报和无验收旁路。

#### 修改初版组件边界

AI 首版总体架构没有独立列出 FeedbackEngine、MemoryStore、ToolDispatcher、CredentialStore。用户没有接受“由已有组件顺带承担”的隐式方案，而是要求拆分职责，以获得更清晰的确定性测试和安全边界。

#### 修改初版审批指纹表达

早期表达使用 `SHA-256(action_type + canonical_normalized_args + workspace_id)`。用户要求改成对带 `version/action_type/normalized_args/workspace_id` 的 canonical JSON 对象计算 SHA-256，避免简单拼接的字段边界歧义，并支持未来 canonicalization 升级。

#### 拒绝扩大首版语言与界面范围

用户选择仅 Python + pytest，且明确不做 WebUI。多语言、双 UI、分布式执行虽然“更完整”，但会扩大接口、测试矩阵和部署成本，不利于突出 Governance/HITL。

#### 修改默认文档组织与计划颗粒度

Superpowers 默认把规格和计划写入 `docs/superpowers/...`，AI 最初也沿用了该方式。用户要求根目录 `SPEC.md` 与 `PLAN.md`，并多次要求增加具体章节、依赖图和更细任务。这反映出课程交付需要直接可见、可审阅的主文档，而不是遵循工具默认目录。

## 5. Cold-start Validation
### CS-01：依赖版本范围未明确

Cold-start Agent 在执行 T01 时指出，PLAN 要求 pyproject.toml
使用 bounded dependency ranges，但 SPEC/PLAN 没有提供具体版本边界，
因此无法在“不自行猜测”的约束下确定依赖声明。

判断：PLAN 缺陷，非 Agent 阅读错误。

处理：
补充首版 runtime/dev dependency ranges，并规定：
兼容范围写入 pyproject.toml，同时提交 lock file；
发生 resolver conflict 时必须报告，不能由 subagent 擅自放宽版本边界。

影响：
消除了不同 subagent 或不同机器选择不同依赖版本造成的实现与测试漂移。
### 验证设置

- 主开发 Agent：Codex
- Cold-start Agent：Claude Code
- Session：全新 session，无历史对话和项目 memory
- 提供材料：仅 `SPEC.md` 与 `PLAN.md`
- 验证任务：T01、T02、T05
- 要求：遇到任何歧义或缺失信息必须暂停询问，不允许自行猜测
- worktree：../coding-agent-harness-coldstart

### 验证结果

Cold-start Agent 能够仅依据 `SPEC.md` 与 `PLAN.md` 理解任务目标、接口边界、
TDD 顺序和验收要求，并完成所选任务的实现尝试。

过程中未出现：
- 阻塞性规格歧义；
- SPEC 与 PLAN 冲突；
- 需要额外口头背景才能理解的隐含假设；
- 与原设计意图明显不一致的实现解释。

因此本轮 cold-start 未触发 SPEC/PLAN 修订。

### 结论

本轮结果表明，当前 SPEC 与 PLAN 对所抽查任务已经具备较好的独立可执行性。
由于 cold-start 只抽查了部分任务，这不能证明后续所有任务均不存在规格缺陷；
正式实现过程中若 subagent 发现新的歧义，仍应停止猜测并反馈主 Agent。

## 6. 对 Superpowers Brainstorming 的评价

### 6.1 有效之处

#### 强制先澄清定位和边界

流程没有直接从空仓库开始编码，而是先确认课程/作品集定位、Python/pytest 范围、CLI/TUI、Docker 和 LLM 接口。这些选择显著减少了后续“既要多语言又要多界面”的漂移。

#### 一次一个关键问题

风险审批、模型接入、沙箱、策略、停机和审计被逐项确认。用户可以在每个节点补充约束，例如 hard boundary 不可覆盖、人工编辑重走全链、Memory 不无差别写入。这比一次性生成完整架构更容易发现隐含假设。

#### 强制比较方案而非直接拍板

模块化单体、事件溯源和独立 Worker 的比较使范围控制理由可见。最终选择不是因为另两种“错误”，而是因为它们会增加事件重放、RPC 和生命周期成本，偏离课程主要贡献。

#### 设计确认后再写规格与计划

分段确认架构、数据流、错误/停机和测试，使冻结 SPEC 能追溯到真实决定。`source=VERIFICATION`、SECURITY_STOP 优先级和 Memory/Audit 分离都来自明确的审阅反馈，而不是事后补写。

### 6.2 不足与 over-engineering 风险

#### 流程开销偏大

Superpowers 要求探索、逐题澄清、方案比较、分段确认、写设计文档、提交、自审、再次用户审阅，再进入 writing-plans。对于已经给出高度具体约束的修订，这套完整循环会产生重复确认和较多元文档工作。

#### 默认文档与提交行为不一定适配当前环境

流程要求把设计写到默认目录并提交。实际过程中，用户后来要求根目录 `SPEC.md`/`PLAN.md`；Git 提交还两次因写入 `.git` 需要授权而被中止。说明流程默认假设与课程仓库布局、受限环境并不完全一致。

#### 容易把计划写得过细或过重

初版 implementation plan 先形成 17 个任务，随后因单个任务仍偏大重构为 T01–T25。最近又提出把 milestone 下拆成 2–5 分钟 micro-steps。细化有助于 subagent 执行，但也可能造成计划维护成本高、步骤与实现现实快速漂移。需要用 cold-start 证据判断哪些细节确实消除歧义，而不是为了形式完整继续扩张。

#### 组件数量增长需要持续警惕

最终组件包含 Registry、Dispatcher、Guardrail、Policy、Approval、Sandbox、Feedback、Memory、Audit、Artifact、Budget、Verification、Credential 等。多数新增来自用户对安全与可测试性的明确要求，而不是 Superpowers 自动生成；但 brainstorming 的“逐项完善”方式仍可能鼓励为每个概念单独建模块。实施中应坚持冻结范围，避免继续添加新核心组件。

#### 强 gate 有时会重复用户已经表达的决定

当用户已经明确说“按这些修订冻结并进入计划”时，brainstorming 的显式批准 gate 仍可能要求再次确认。它能防止误改，但也会降低推进速度。更合适的使用方式是：对架构性变更保留 gate，对纯格式/过程证据修订采用一次范围确认后直接执行。

### 6.3 总体评价

在本项目中，Superpowers brainstorming 最有价值的作用是把 Governance/HITL 从一句目标拆成可验证的执行链、审批语义、停止规则与审计边界，并迫使方案选择说明 trade-off。它的主要成本是流程和文档颗粒度容易继续膨胀。后续应以冻结 SPEC、cold-start 暴露的真实缺陷和 AC-1–AC-18 为变更依据，而不是为了让流程更“完整”继续增加架构范围。

## 7. 当前证据状态

| 项目 | 当前可确认状态 |
|---|---|
| Brainstorming 设计选择 | 已记录于本对话，并反映在 `SPEC.md` |
| SPEC 状态 | `SPEC.md` 标记 Frozen，当前含 AC-1–AC-18 |
| PLAN 状态 | 当前含 T01–T25 与 worktree 并行 wave |
| 代码实现 | 当前上下文没有已完成实现或测试通过证据 |
| Cold-start validation | TODO / 未执行 |
| 教师 WebUI 豁免确认 | TODO / 未取得答复 |
| T00 gate | TODO / 尚未执行 |
| Git 提交证据 | 设计阶段曾两次请求提交授权并被中止；随后 `SPEC.md`、`PLAN.md`、空的 `README.md`/`AGENT_LOG.md` 与 `.gitignore` 变更已由提交 `b8b5592`（`plan编写`）记录。当前 `SPEC_PROCESS.md` 尚未提交 |
