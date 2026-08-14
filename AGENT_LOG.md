# Agent Implementation Log

## T01 — 包骨架与核心领域模型

- 状态：完成
- Implementation commit：`7ab82ce82b1467bfa37b1191cb6b6d4febe12a69`
- Implementer：fresh subagent `/root/t01_implementer`
- TDD：初始 RED 为 `ModuleNotFoundError: coding_agent_harness`；初始 GREEN 为 15 tests passed。代码质量修复轮新增 10 个预期 RED，最终 33 tests passed。
- Verification：package import、Ruff、mypy、`git diff --cached --check` 全部通过。
- Spec compliance review：通过，无 Critical/Important/Minor。
- Code quality review：初审发现 4 个 Important（嵌套可变映射、无效 ModelDecision 形态、错误码缺测、终止集合保护不足）与 1 个 Minor；fix round 1 全部修复，复审无新 Critical/Important。
- 依赖解析：用户补充精确有界依赖后，`uv lock` 成功解析 53 packages；未放宽任何上界。
- 范围：仅 T01 声明文件及用户授权的 PLAN 依赖范围同步；未扩展产品功能。

## T02 — Canonical JSON 与版本化审批指纹

- 状态：完成
- Implementation commit：`9833c8d701724760447f9ea37f8870970f179aac`（集成为 `5958116`）
- Implementer：fresh subagent `/root/w2_t02`
- TDD：初始 RED 为缺少 `coding_agent_harness.canonical`；质量审查修复轮先复现 `version=1.0` 未被拒绝的 RED，最终 canonical 16 tests、与 domain 联合 49 tests passed。
- Verification：Ruff、mypy、`git diff --check` 全部通过。
- Spec compliance review：初审通过；质量修复后由 fresh reviewer 复审为 SPEC COMPLIANT。
- Code quality review：初审发现 1 个 Important（Python 相等语义使 `1.0` 被当作版本 1 接受）；TDD 修复后 fresh reviewer 批准，无 Critical/Important/Minor。
- 范围：仅 `canonical.py` 与 `test_canonical.py`；未扩展产品功能。

## 实现阶段规格裁决 — T05/T06/T14 secret containment

- 发现：T05 规格写明 env/stdin 受限，但未定义 exact-secret 来源或检测接口；T05 与 T14 同属 W2，不能让 ToolRegistry 隐式依赖 CredentialStore。T05 spec review 因此提出了无法按冻结文本确定实现的凭据检测项。
- 人工裁决：secret 泄漏属于 hard safety boundary，而非 Action schema/canonicalization 错误。T05 不增加 secret-content 检测；该 review 项判定为范围外，不阻塞 T05。
- 职责：T06 Guardrail 注入 `SecretDetector` Protocol，对进入 Docker 的 env value/stdin 做宿主已知 exact literal containment；命中为不可由 Policy/HITL 覆盖的稳定拒绝。T14 提供 detector adapter，T06 不直接导入 T14。
- 边界：不发明 token regex；MockLLM/无凭据使用 EmptySecretDetector。存在应保护凭据但无法建立 detector 时 fail closed。
- 过程影响：PLAN 的 T05/T06/T14 验收点已按本次人工裁决澄清，未新增产品能力。

## 实现阶段规格裁决 — T03 公开 Path 安全矛盾

- 发现：T03 原 PLAN/brief 要求公开 `RunArtifactStore.path_for()`；即使 Store 内部使用可信 `dir_fd`，普通 `Path` 返回后，调用者的后续 open/read 仍可能在目录替换后逃逸，形成证据安全边界旁路。
- 人工裁决：删除公开 `path_for()` 契约。业务组件只能持有 `ArtifactRef`，所有 evidence I/O 必须通过 descriptor-backed `put/read/verify`。
- 诊断边界：如需展示位置，只允许 diagnostic-only 字符串，不得用于 read/verify；内部私有 helper 也不得替代 anchored I/O。
- 过程影响：SPEC §4.10/AC-18 与 PLAN T03 已同步收紧；T04/T11 消费者继续仅使用 ArtifactRef + Store，没有扩张 T03 其他范围。

## T13 — BudgetController、NoProgress 与终止优先级

- 状态：完成
- Implementation commit：`f0cb4fe`（集成为 `bc953fe`）
- Implementer：fresh subagent `/root/w2_t13`
- TDD：多轮 RED→GREEN 覆盖独立预算、非有限/超大数与时钟回拨、累计溢出不污染状态、严格输入类型、NoProgress 独立信号、run-scoped snapshot、verified SUCCESS 与完整确定性终态优先表。
- Verification：最终 focused 127 passed、全部 unit 160 passed；Ruff、mypy、cached diff check 通过。
- Spec compliance review：最终 SPEC COMPLIANT；确认 `SECURITY_STOP` 最高且全部终态排列裁决稳定。
- Code quality review：初审发现负快照、security 覆盖假阳性及候选事实矛盾；TDD 修复后 CODE QUALITY APPROVED。
- 范围：仅 T13 声明的四个实现/测试文件；未加入 Runtime 协调逻辑。

## T05 — ToolRegistry 与结构化 Action

- 状态：完成
- Implementation commit：`d7a3b58`（集成为 `feba572`）
- Implementer：fresh subagent `/root/w2_t05`，后续安全修复由 fresh subagent/reviewer 在同一隔离 worktree 完成。
- TDD：从缺失模块 RED 开始，多轮负向回归覆盖 strict argv、路径/symlink、pytest masquerade、有限 wrapper grammar、解释器启动选项与 module/script/stdin 边界；最终 tools+domain 169 passed。
- Verification：Ruff、mypy、cached diff check 通过。
- Spec compliance review：最终 SPEC COMPLIANT；exact-secret content 检测依人工裁决留给 T06 Guardrail。
- Code quality review：修复 Perl `-E` 漏拒、Python attached option 误报与 grammar 规则漂移后 CODE QUALITY APPROVED。
- 范围：仅 `tools.py` 与 `test_tools.py`；未实现 Docker、Policy、Guardrail 或 secret detector。

## T03 — RunArtifactStore

- 状态：完成；implementation `e7f64d4`，integration `3a97010`。
- TDD/验证：50 artifact tests、83 unit tests；Ruff、mypy、cached diff check 通过。
- Review：SPEC COMPLIANT；CODE QUALITY APPROVED（quota mode，无 Critical/Spec/Security 阻塞）。
- 安全结果：descriptor-backed I/O、原子 no-replace、配额锁、digest/size、私有权限、symlink/目录替换与稳定 evidence error。
- 非阻塞 TODO：pytest restrictive-umask fixture 留下临时目录 cleanup warning，后续测试卫生任务处理。
