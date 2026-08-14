# REFLECTION


## 1. SPEC、PLAN 与 Cold-start Validation 对开发方式的改变

这次项目要求在正式实现之前先完成 brainstorming、SPEC 和 PLAN，并且在 SPEC 冻结之后才开始开发。一开始我其实觉得这个过程有些重，因为很多设计在没有开始编码之前很难判断是否真的需要如此详细。但是随着模块逐渐增多，我发现前期花在 SPEC 和 PLAN 上的时间实际上减少了后面的返工。

例如，我在设计阶段提前确定了 AgentRuntime 是唯一的核心协调器，Guardrail 与 Policy 分别负责不同层次的约束，Human Edit 之后不能直接绕过治理流程执行，而是需要生成新的 Action 并重新经过完整检查；同时 Audit Log 和 Memory Store 也被明确区分，一个保存不可篡改的事实记录，一个保存可以持续更新的经验。这些设计如果等代码写到一半以后再决定，很可能会导致大量模块重新修改。

Cold-start Validation 也让我印象比较深。自己写 SPEC 时，因为脑中已经有完整的设计背景，所以很多表述即使不够明确，我自己仍然能够理解。但是让一个全新的 Agent 只阅读 SPEC 和 PLAN，再尝试完成任务时，一些隐含假设就会暴露出来。

## 2. 多 Agent 开发提高了效率，也放大了接口一致性问题

本项目开发过程中，我使用了多个 Coding Agent 来完成不同阶段的任务，并且将不同模块拆分到独立的分支和 worktree 中。这样的开发方式明显提高了实现速度，因为不同 Agent 可以围绕相对明确的模块独立工作，例如 Audit、Policy、Approval、Sandbox、Memory 等模块可以分别推进。

但与此同时，我也发现多 Agent 开发最大的风险之一是接口漂移。每个 Agent 往往能够很好地完成自己当前负责的任务，并让对应测试通过，但它不一定拥有其他 Agent 所有实现细节的上下文。因此，两个模块分别看可能都没有问题，真正集成时却可能对同一个接口存在不同理解。

最终质量检查阶段就出现了一个很典型的例子。虽然完整 pytest 已经能够通过，但 mypy 仍然发现 `ApprovalRequest`、`ApprovalOutcome` 和 Runtime 之间存在静态类型契约不一致的问题，例如 Runtime 使用了 `decision`、`fingerprint`、`replacement_proposal` 等字段，但是类型系统没有正确识别这些属性。另外 CLI 中也存在对 Audit 接口旧版本的调用方式。

这些问题最终没有改变运行时行为，但它让我认识到，“测试通过”并不意味着系统的模块契约一定完整一致。以后如果继续使用多 Agent 并行开发，我会更强调稳定的公共接口，并且在每一个模块 PR 阶段同时执行 pytest、mypy 和 lint，而不是直到项目最后才统一进行静态类型检查。

## 3. TDD 让我重新理解了测试的作用

以前我写项目时，更多把测试理解为“功能实现完成之后检查代码有没有写错”。这次项目要求按照 RED-GREEN-REFACTOR 的方式进行开发，让我逐渐体会到测试也可以反过来定义系统应该遵守的行为。

这一点在安全相关模块中尤其明显。例如 Secret 不能进入 Docker 环境、日志、Audit、Artifact 和 Memory；Human Edit 之后必须重新经过治理链；高风险动作不能因为一次审批而无限复用；Audit 被人为修改之后必须能够发现完整性破坏。这些行为很多时候不是通过手动运行一次程序就可以可靠验证的。

因此，对于这些安全边界，先通过测试明确“什么行为一定不能发生”，再去实现对应逻辑，比实现完成后再检查更可靠。TDD 在这里不仅是保证代码正确的方法，也实际上成为了安全设计的一部分。


## 4. 最有价值的两个失败案例

项目最后验收阶段出现的两个问题，对我的帮助反而比很多顺利完成的功能更大。

第一个问题是 Python 环境污染。最终测试时突然出现了大量 `ModuleNotFoundError` 和类型不存在的问题，一开始看起来像是代码在合并以后被破坏了。但继续排查后发现，真正的原因是 Conda base 环境之前曾经以 editable mode 安装过用于 Cold-start Validation 的另一个 worktree。因此虽然当前终端目录位于正式项目中，Python 实际 import 的源码却来自 `coding-agent-harness-coldstart`。

这让我意识到，开发环境本身也是工程可复现性的一部分。一个项目即使代码完全正确，如果 Python 解释器、虚拟环境或者 editable install 状态不明确，依然可能得到完全不同的结果。以后我会更严格地坚持一个 worktree 对应一个独立 `.venv`，并优先通过 `python -m pytest`、锁定依赖和明确的环境路径运行工具，而不是默认相信当前 shell 的 PATH。

第二个问题发生在 GitHub Actions。项目在本地已经能够通过 717 个测试，但第一次放到 GitHub CI 后却出现了 711 passed、6 failed。失败的全部是 Docker Sandbox 相关的 integration test，并且统一返回 exit code 125。最后发现问题并不在 Sandbox 的业务逻辑，而是 GitHub Runner 中并不存在本地已经构建好的 `coding-agent-harness-sandbox:latest` 镜像。

这件事让我重新理解了 CI。CI 并不是简单地“在另一台机器上执行 pytest”，而是在一个接近全新的环境中重新验证项目是否真正可复现。因此 CI 必须明确描述项目成立所依赖的全部条件，包括 Python 版本、依赖安装方式以及 Docker 镜像的构建过程。修复 workflow，使 CI 在运行 integration test 前主动构建 Sandbox 镜像后，远程测试才真正全部通过。

## 5.Superpowers 方法论反思

对我帮助最大的 Superpowers 技能是 `brainstorming`、`writing-plans`、`subagent-driven-development`、`TDD` 和 `verification-before-completion`。其中前两者最重要，因为它们先明确了 Runtime、Guardrail、Policy、HITL、Sandbox、Audit、Credential 等模块的职责边界，避免后续 Agent 各自理解一套。相对而言，`using-git-worktrees` 和一些小任务上的独立 code review 有时会显得形式大于实质，尤其当任务只有少量机械修改时，流程成本可能高于实际收益。

我认为 TDD 在 AI 协作下总体是放大器，但前提是需求已经足够明确。对于“Secret 不能进入 Docker”“Audit 被篡改必须失败”“Verification 不通过不能 SUCCESS”这类确定行为，测试可以很好地约束 Agent；但如果架构本身还没想清楚，过早写测试反而会把错误设计固化下来。

`subagent-driven-development` 最适合“一个明确能力 + 一个主要模块 + 少量稳定依赖 + 一组独立测试”的任务。任务过大，例如同时涉及 Runtime、Approval、Policy、Audit 和 Verification，Agent 很容易偏离；但拆得太碎又会增加协调成本。

SPEC / PLAN 的质量直接决定 subagent 的实现质量。一个典型案例是 Secret Detection。最初如果只写“禁止 Secret 泄漏”，Agent 可能分别在 ToolRegistry、Guardrail 和 CredentialStore 中各实现一套检测逻辑。后来明确规定 ToolRegistry 只做参数规范化，Guardrail 负责 hard secret boundary，CredentialStore 只提供 host secret source，职责才真正稳定下来。这让我认识到，SPEC 不仅要写“做什么”，还要写清楚“谁负责”和“谁不负责”。

我最有效的 Prompt 策略是：提供固定系统约束、当前任务相关的最小上下文、明确禁止项，以及可执行的验收条件，例如 pytest、ruff、mypy 和 `git diff --check`。这种方式既能减少 Agent 猜测，也能避免上下文过多导致发散。

凭据和分发要求也迫使我考虑了原本容易忽略的问题。Credential 不只是“放进 `.env`”这么简单，还要考虑它是否进入 Model Context、Docker、Audit、Artifact、Memory 和日志；而分发要求则让我认识到“本地能跑”不等于“项目可复现”。例如本地 717 个测试全部通过，但 GitHub Actions 最初因为没有构建 Sandbox Docker 镜像而有 6 个集成测试失败。

如果重做一次，我会更早建立 CI，把 pytest、ruff 和 mypy 作为每个 PR 的常规门禁，并且严格执行一个 worktree 对应一个独立 `.venv`。

我对 Superpowers 的总体评价是：它非常适合高可靠、边界明确的工程任务，但它隐含假设需求可以较早冻结、任务可以清晰拆分，而且更多流程通常能提高可靠性。这些假设在本项目中大部分成立，但在跨模块 integration 和小规模修改中并不完全成立。因此我认为它更适合作为一套风险控制框架，而不是必须机械执行的固定流程。

## 6. 如果重新实现一次，我会做出的改进

如果重新开始这个项目，我认为至少有三个地方可以做得更好。

第一，我会更早建立 CI。实际上 GitHub Actions 是在项目开发接近结束时才补充的，因此 Docker 镜像缺失等环境问题直到最后才暴露。如果从项目初期就建立一个最小 CI，很多可复现性问题都可以在模块数量还比较少时被发现。

第二，我会把静态类型检查更早放入每一个任务的完成标准。本次最终 mypy 暴露出的 Approval 类型契约问题本身并不严重，但说明在多 Agent 开发中，仅依靠运行时测试无法完全保证公共接口一致。如果每个 PR 都同时要求 pytest、ruff 和 mypy 全部通过，接口漂移会更早被发现。

第三，我会更加严格地管理 worktree 和 Python 虚拟环境。Cold-start worktree 污染正式环境的问题虽然最终比较容易解决，但它说明开发过程中仍然存在一些隐式状态。以后创建 worktree 时，我会同步建立独立 `.venv`，避免不同任务之间共享 editable installation。
