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
