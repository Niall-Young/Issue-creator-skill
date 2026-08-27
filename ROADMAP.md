# GitHub Issue Workflow Roadmap

[中文](#中文) | [English](#english)

---

<a id="中文"></a>
## 中文

### 产品边界

项目保留三个权限独立、可组合的工作流：

- `github-issue-handoff`：把仓库上下文与用户意图交接成 Agent 可执行的 Issue。创建 Issue 不会自动触发代码修改。
- `github-issue-repair`：读取既有 Issue，提出工作包与验证方案，在获得授权后修改代码，并可在再次授权后发布 draft PR。
- `github-issue-autopilot`：按可信本地策略自动发现 owner 创建的新 Issue，并在全新 Agent 进程中调用 repair 工作流。

三者可以共享 URL 解析、GitHub 读取、仓库上下文采集与结构化 Issue 数据，但必须拥有不同的触发条件、授权边界、状态与成功标准。

当前仓库已包含三个 Skill、工作包契约、审批状态机、修复账本和 SQLite 自动调度账本。单机轮询、单 Issue 串行自动领取已实现；仓库级小批次、多机协调、自动发布与有限并发仍需真实验收数据解锁。

### 核心原则

- 调度单位是“工作包”，不是机械地“一条 Issue 一个 subagent”。一条 Issue 可以拆成多个工作包；多个 Issue 也可能因为相同根因合并。
- 仓库 URL 首先代表只读分诊，不代表“修复全部打开的 Issue”。用户必须确认执行批次。
- 协调器负责确定性的状态、依赖、预算、审批和恢复；Agent 只承担有边界的规划、实施与审查角色。
- worker 使用隔离 worktree，不接触远程写凭据，也不修改用户正在使用的工作树。
- 自动化可以准备证据和 draft PR，但 MVP 不自动合并、关闭 Issue、评论、打标签、发布或部署。
- 新部署必须设置 `activate_after`，避免首次运行扫入历史 backlog；自动任务以 GitHub Issue node ID 幂等识别，Issue 编辑不会自动重跑。

### 分阶段计划

#### Phase 0：强化交接契约

目标：让 `github-issue-handoff` 产出的 Issue 更适合人和 Agent 接手。

- 为 Issue 明确可观察的验收标准、最强可用验证方式、依赖/阻塞关系和风险提示。
- 将“可能涉及的路径”作为调查线索，而不是允许修改范围。
- 为后续 repair 工作流定义机器可读取的结构化输出，不改变当前创建授权语义。

验收：新 Agent 只读 Issue 和仓库即可判断任务是否可执行、如何验证，以及缺少什么证据。

#### Phase 1：只读 Repair Planner

输入一条 Issue URL；仓库 URL 模式只做候选 Issue 排序与分诊。

- 固定 Issue 快照和仓库 base SHA。
- 输出工作包、依赖/重复/冲突关系、风险等级、预计影响范围、验证契约与预算。
- 把结果分为 `ready`、`needs-human`、`unsafe`，不创建分支、不改代码、不写 GitHub。

验收：同一快照可重复得到可审计的计划；不完整或高风险任务会明确停止。

#### Phase 2：单工作包本地闭环

- 一次只接受一条明确的 Issue URL 和一个经用户批准的工作包。
- 从固定 base SHA 创建隔离 worktree 与 `repair/<package-id>-<slug>` 分支。
- 记录修改前基线；实施后运行针对性测试、相关完整检查与范围审查。
- Bug 在适用时提供“base 上失败、分支上通过”的回归证据；其他任务使用最强可用的客观验证。
- 独立 reviewer 用新上下文核对 diff、验收标准、范围漂移和测试弱化。
- 只产出本地提交与证据包，不远程推送。

验收：中断后可恢复；不会污染用户工作树；失败分支和证据会保留而非被假装成成功。

#### Phase 3：受控发布 draft PR

- 用户核对 diff 与验证证据后，一次明确授权 push 和 draft PR。
- PR 正文包含关联 Issue、base/head SHA、验收标准映射、验证命令与结果、剩余风险和 Agent 来源。
- 不自动使用关闭关键字，不自动评论、打标签或合并。
- 持久 ledger 记录审批、分支、提交和远程回执，恢复时先对账再写入。

验收：远程动作可追踪、可幂等恢复，不会因超时或未知回执重复创建 PR。

#### Phase 4：自动领取、小批次与有限并发

已完成的单机基础：

- `github-issue-autopilot` 用 `gh` 只读轮询允许仓库，按作者、`activate_after` 与可选标签筛选。
- SQLite WAL 账本用 node ID 去重、事务领取与租约恢复；每次执行启动 fresh Agent 进程，并在执行前复核 Issue 仍然符合策略。
- 符合策略的 Issue 只预授权一个低/中风险本地工作包；远程发布固定为 `never`，merge 始终由人完成。
- `launchd` 可定时运行 `once`；无有效 `AUTOPILOT_RESULT` 回执不得判定成功。

只有上述串行流程在真实仓库中稳定后才继续开放：

- 从仓库 URL 选择有上限的批次，用户确认后执行。
- 根据 `blocked-by`、`duplicate-of`、`same-root-cause`、`conflicts-with` 与共享契约建立依赖图。
- 只对低交互风险工作包进行有限并发；每个 worktree 独立，集成后重新验证。
- 工作包实际触及范围与预测不符时，暂停相关任务并重新规划。

验收：并发不会提高回归逸出、冲突返工或人工审查时间；否则退回串行。

### 安全与恢复门禁

1. 范围门禁：用户确认工作包、顺序、预算与验证方案。
2. 扩围门禁：依赖升级、迁移、安全/认证/支付、公共 API、破坏性命令或异常大 diff 必须停止并重新授权。
3. 发布门禁：push、draft PR 及任何 Issue 写操作均需明确授权。
4. 合并门禁：始终由人完成，不进入 MVP 自动化。

每次 run 持久记录 Issue 快照、计划哈希、base/head SHA、工作包状态、审批、命令与退出码、验证证据和远程回执。失败分为暂时性故障、需要重规划、需要人工判断、不安全、验证失败与集成冲突；只有暂时性故障可在预算内自动重试。

### 扩展指标

是否进入下一阶段，不看 subagent 数量，而看：

- draft PR 被接受或合并的比例
- 人工审查与返工时间
- 回归逸出和错误成功率
- 需要人工介入的频率
- 中断恢复成功率
- 每个被接受 PR 的成本

阈值应按仓库类型和任务组合用试点数据确定，不预设统一数字。

[English](#english) · [返回顶部](#github-issue-workflow-roadmap)

---

<a id="english"></a>
## English

### Product Boundary

Keep three composable workflows with separate permission surfaces:

- `github-issue-handoff`: turns repository context and user intent into an agent-ready Issue. Creating an Issue never triggers code changes.
- `github-issue-repair`: reads an existing Issue, proposes work packages and verification, modifies code after approval, and may publish a draft PR after a separate publication authorization.
- `github-issue-autopilot`: discovers new owner-authored Issues under trusted local policy and invokes the repair workflow in a fresh Agent process.

They may share URL parsing, GitHub reads, repository context collection, and structured Issue data, but they must retain distinct triggers, authorization boundaries, state, and success criteria.

The repository now includes all three Skills, the work-package contract, approval state machine, repair ledger, and SQLite dispatch ledger. Single-machine polling and sequential automatic claims are implemented; repository batches, multi-runner coordination, automatic publication, and bounded concurrency still require real acceptance evidence.

### Core Principles

- Schedule work packages, not mechanically one subagent per Issue. One Issue may split into several packages, while several Issues may share one root cause and become one package.
- A repository URL initially requests read-only triage, not “fix every open Issue.” The user confirms the execution batch.
- A deterministic coordinator owns state, dependencies, budgets, approvals, and recovery. Agents perform bounded planning, implementation, and review roles.
- Workers use isolated worktrees, receive no remote-write credentials, and never modify the user's active working tree.
- Automation may prepare evidence and draft PRs, but the MVP never merges, closes Issues, comments, labels, releases, or deploys automatically.
- New deployments require `activate_after` so the first run cannot sweep an old backlog. Automatic jobs are idempotent by immutable GitHub Issue node ID; Issue edits do not retrigger work.

### Phased Plan

#### Phase 0: Strengthen the handoff contract

- Make observable acceptance criteria, the strongest applicable verification, dependencies, blockers, and risks explicit.
- Treat likely paths as investigation hints, not write permissions.
- Define machine-readable output for the later repair workflow without changing current creation authorization.

Exit criterion: a new agent can decide whether the task is actionable, how to verify it, and which evidence is missing using only the Issue and repository.

#### Phase 1: Read-only Repair Planner

Accept one Issue URL; repository URL mode only ranks and triages candidates.

- Pin the Issue snapshot and repository base SHA.
- Output packages, dependency/duplicate/conflict relations, risk, expected impact, verification contracts, and budgets.
- Classify results as `ready`, `needs-human`, or `unsafe`; create no branches and perform no repository or GitHub writes.

Exit criterion: the same snapshot yields an auditable plan, while incomplete or risky work stops explicitly.

#### Phase 2: One-package local loop

- Accept one explicit Issue URL and one user-approved work package.
- Create an isolated worktree and `repair/<package-id>-<slug>` branch from the pinned SHA.
- Capture the baseline, implement, run targeted and relevant broader checks, and review scope.
- For bugs, show a regression failing on base and passing on the branch when applicable; use the strongest objective oracle for other tasks.
- An independent reviewer with fresh context checks the diff, acceptance criteria, scope drift, and weakened tests.
- Produce only a local commit and evidence package; do not push.

Exit criterion: runs resume safely, never contaminate the active worktree, and preserve failed branches and evidence instead of reporting false success.

#### Phase 3: Controlled draft-PR publication

- After the user inspects the diff and evidence, one explicit authorization covers push and draft-PR creation.
- The PR records linked Issues, base/head SHAs, acceptance-criteria mapping, commands and results, residual risk, and agent provenance.
- Do not use closing keywords automatically; do not comment, label, or merge.
- A durable ledger records approvals, branches, commits, and remote receipts; recovery reconciles state before writing.

Exit criterion: remote actions are traceable and resumable without duplicate PRs after timeouts or unknown receipts.

#### Phase 4: Automatic claims, small batches, and bounded concurrency

Implemented single-machine foundation:

- `github-issue-autopilot` performs read-only `gh` polling on allowlisted repositories and filters by author, `activate_after`, and optional labels.
- A SQLite WAL ledger deduplicates by node ID, claims transactionally, and recovers stale leases. Every run starts a fresh Agent process and revalidates eligibility before execution.
- An eligible Issue pre-authorizes only one low/medium-risk local work package. Remote publication is fixed to `never`, and merge remains human-only.
- `launchd` can schedule `once`; a run without a valid `AUTOPILOT_RESULT` receipt is never reported as success.

Enable the following only after the sequential flow is reliable in real repositories:

- Select a capped repository-level batch, then require user confirmation.
- Build a graph using `blocked-by`, `duplicate-of`, `same-root-cause`, `conflicts-with`, and shared-contract relationships.
- Run only low-interaction-risk packages concurrently; isolate every worktree and reverify the integrated result.
- Pause and replan when actual touched scope differs from predictions.

Exit criterion: concurrency does not increase regression escapes, conflict rework, or human review time; otherwise return to sequential execution.

### Safety and Recovery Gates

1. Scope gate: the user approves packages, order, budget, and verification.
2. Expansion gate: dependency upgrades, migrations, security/auth/payment work, public APIs, destructive commands, or unusually broad diffs stop for renewed authorization.
3. Publication gate: push, draft PR, and every Issue write require explicit authorization.
4. Merge gate: always human-only and outside the MVP.

Every run persists the Issue snapshot, plan hash, base/head SHAs, package states, approvals, commands and exit codes, verification evidence, and remote receipts. Failures are classified as transient, replan-required, human-required, unsafe, verification failure, or integration conflict. Only transient failures are retried automatically within a budget.

### Expansion Metrics

Advance based on outcomes rather than subagent count:

- Accepted or merged draft-PR rate
- Human review and rework time
- Regression escapes and false-success rate
- Human intervention frequency
- Recovery success after interruption
- Cost per accepted PR

Targets should be calibrated from pilot data by repository class and task mix rather than imposed as one universal threshold.

[中文](#中文) · [Back to top](#github-issue-workflow-roadmap)
