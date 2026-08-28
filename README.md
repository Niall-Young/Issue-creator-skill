# GitHub Issue Workflow

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

创建 Agent 可直接接手的 GitHub Issue，自动发现新任务，并在干净 Agent 上下文中完成受控修复。

Create agent-ready GitHub Issues, detect new assignments automatically, and run controlled repairs in clean agent contexts.

[中文](#中文) | [English](#english)

---

<a id="中文"></a>
## 中文

### 项目简介

这是一个面向 Agent 开发流程的技能包，包含四个权限独立、可组合的 Skill：`github-issue-handoff` 把仓库上下文和用户意图交接成可执行 Issue；`github-issue-repair` 在隔离 worktree 中修复既有 Issue；`github-issue-autopilot` 自动发现符合本地策略的 open Issue，并在 Orca 中启动可见的 Agent 任务；`github-issue-workflow-update` 从最新稳定 Release 更新整套本地安装。

从旧版本升级时，请删除运行时中的 `github-issue-creator/` 安装副本，再安装 `github-issue-handoff/`，并把显式调用改为 `$github-issue-handoff`，避免新旧技能同时被发现。

### 核心能力

- `github-issue-handoff`：校验仓库、检索重复项、套用 Feature / Bug / Refactor / Research 模板，通过可执行性门禁后创建并回读中文 Issue。
- `github-issue-repair`：把 Issue 归一化为工作包，识别依赖、重复、同根因与冲突关系；仓库 URL 默认只做只读分诊。
- `github-issue-autopilot`：一句话为当前仓库安装本地循环，领取由当前用户创建且带 `agent-ready` 的全部 open Issue，包括启用前已创建的积压任务，并调用 `$github-issue-repair`。
- `github-issue-workflow-update`：用户说“检查 issue-workflow 更新”时只读比较版本，说“更新 issue-workflow”时覆盖当前 Agent 运行时中的四个官方 Skill，并同步已安装的 Autopilot runtime。
- 修复 worker 使用 Orca 管理的项目子 worktree，不修改用户正在使用的工作树；一次记录全部合格 Issue，最多并发 3 个任务。
- 独立 reviewer 核对验收标准、验证证据、范围漂移和测试弱化。
- 双层账本以 Issue node ID 防止重复派单，并为每次 attempt 记录所选 Agent、Orca Task/Dispatch/worktree、分支、base/head SHA 和结果。
- 交互模式仍由用户批准本地修改；Autopilot 可把符合可信本地策略的 Issue 视为一次低/中风险本地修复授权。安装器只负责缺失的仓库触发标签；修复执行不 push、不建 PR，也不写 Issue、发布或部署。

### 快速开始

#### 环境要求

- 正在运行的 Orca，以及至少一个可由 Orca 启动的 Agent（如 Codex、Claude 或 OMP）
- 已认证的 GitHub CLI（`gh`）

#### 安装

推荐将下面的提示词直接复制到你的 Agent 会话中，让 Agent 根据其运行环境完成安装：

```text
帮我安装这个 skill 仓库中的四个技能：https://github.com/Niall-Young/github-issue-workflow
```

Agent 可能会根据运行环境请求必要的授权，或说明无法自动安装时的限制。

如需手动安装，可克隆仓库，再将四个技能目录复制到 Agent 运行时的 skill 目录。以下命令以 Claude Code 的 `~/.claude/skills/` 为例；安装后请重新启动会话：

```sh
git clone https://github.com/Niall-Young/github-issue-workflow.git
cp -R github-issue-workflow/github-issue-handoff ~/.claude/skills/github-issue-handoff
cp -R github-issue-workflow/github-issue-repair ~/.claude/skills/github-issue-repair
cp -R github-issue-workflow/github-issue-autopilot ~/.claude/skills/github-issue-autopilot
cp -R github-issue-workflow/github-issue-workflow-update ~/.claude/skills/github-issue-workflow-update
```

#### 运行

单次创建和修复可在 Agent 会话中显式调用；自动调度器是本地常驻/定时命令：

```sh
$github-issue-handoff <GitHub URL>
$github-issue-repair <GitHub Issue URL>
$github-issue-repair <GitHub Repository URL>
python3 github-issue-autopilot/scripts/autopilot_admin.py install --repo-path /absolute/repository/root --agent codex
$github-issue-workflow-update 更新 issue-workflow
```

### 使用方法

给出最小示例。显式调用技能即授权创建一条 Issue：

```sh
$github-issue-handoff https://github.com/owner/repo 帮我排查登录接口偶发超时的问题
```

预期结果：技能按 Bug 模板生成一份中文 Issue，通过创建前门禁与去重检查后提交，并返回标题与可点击的 Issue URL。若仓库存在安全风险，则按 Security Policy 走私有上报渠道，不创建公开 Issue。

修复一条既有 Issue：

```sh
$github-issue-repair https://github.com/owner/repo/issues/123
```

预期结果：技能先只读分析并展示工作包、base SHA、风险、预算和验证方案。用户批准后才允许本地修改；用户检查 diff 与证据并授权发布后，才允许 push 和创建 draft PR。仅提供仓库 URL 时，技能只输出有限候选批次，不会自动修复所有 Issue。

在任意 GitHub 项目中说“构建 Issue 循环检查机制”，Autopilot 会运行确定性安装器，创建缺失的 `agent-ready` 标签、仓库独立配置、Orca 原生 Automation 与 Orca 协调器。Automation 可在 GUI 中暂停、恢复和查看历史；它每三分钟预检一次，只有协调器缺失时才调用 Agent 恢复，不会每轮消耗模型。全部合格 Issue 都会入账，最多三个在 Orca 左侧作为项目子 worktree 同时运行。未完成的 `failed` attempt 若对应 Issue 仍 open 且符合策略，会在下一轮自动重试，默认总计最多两次；待验收、明确需人工或策略阻塞的结果不会自动重做。每个 Issue 的每次 attempt 必须使用从未被历史 attempt 或其他 Issue 使用过的 Orca Task、Dispatch、worktree ID 和绝对路径；身份复用或成功证据不完整会停止等待人工处理，不会退回手工或共享 worktree。安装时用 `--agent` 选择默认 Agent；单个 Issue 可用唯一 `agent:ID` 标签覆盖，冲突、禁用或不可用的 Agent 会停在可见的人工处理状态。满意时明确 `accept` 到当前干净的目标分支；它会在合并成功后关闭并回读对应 Issue，关闭失败则保留 worktree 供安全重试。若 worktree 已被手工清理，只有记录的修复 head 已在目标分支历史中时才能补关。不满意时通过明确的 `retry` 保留旧 attempt 历史并创建全新 Orca 子 worktree；人工重试可超过自动次数上限。旧 worktree 仍存在且决定舍弃时使用 `retry --discard-worktree` 精确清理。standalone repair、手工 `git worktree`、共享 worktree、手工合并或补写账本都不能替代该流程。详见 [`configuration.md`](github-issue-autopilot/references/configuration.md)。

### 更新机制

更新不会在后台自动运行。首次安装 `github-issue-workflow-update` 后，只有用户明确要求检查或更新时，Agent 才会调用更新器：

```sh
$github-issue-workflow-update 检查 issue-workflow 更新
$github-issue-workflow-update 更新 issue-workflow
```

- **检查**是只读操作，只返回当前安装版本、最新稳定版本和是否有更新，不修改 Skill 或 Autopilot。
- **更新**从 `Niall-Young/github-issue-workflow` 获取最新已发布的稳定 Release。即使版本号相同，也会重新安装官方文件以清除本地漂移；四个受管 Skill 中的手工修改会被覆盖。成功后建议新开会话，让 Agent 重新加载 Skill 指令。
- 更新器先校验 Release 来源、版本、归档 SHA-256、清单、文件白名单和逐文件哈希，再对所有已安装的 Autopilot runtime 做只读预检。预检通过后，它事务式替换四个 Skill，同步 runtime，并回读验证 Orca Automation 和 runtime 文件。
- Autopilot 的配置、SQLite 账本、日志、attempt 历史和 worktree 会保留。Skill、runtime 或 Automation 更新失败时会自动恢复旧状态；只有回滚本身也失败的罕见 `partial-update` 才需要人工恢复，Agent 必须明确报告受影响组件。
- 更新范围仅限当前 Agent 运行时中的四个官方 Skill，以及由它们管理的已安装 Autopilot runtime。它不会执行 `git pull`、修改源码 checkout、更新其他 Skill，也不会扫描或改动其他 Agent 运行时。

发布端以 `vMAJOR.MINOR.PATCH` tag 触发 GitHub Actions：先运行测试，再构建带版本清单、逐文件哈希和归档校验和的可复现包，最后发布稳定 Release。更新器只消费已发布的稳定 Release，拒绝 draft、prerelease、降级、校验失败和异常归档；仅推送 tag、但尚未成功生成稳定 Release 时，不会成为可安装更新。`v0.1.0` 是首个声明的发行版本。

### 配置

- 四个 Skill 的 `agents/openai.yaml` 提供 OpenAI 兼容 Agent 的界面配置并允许自动发现；技能发现本身不构成修改代码或远程写入授权。
- `github-issue-repair/scripts/run_state.py` 使用 Python 标准库，在仓库 Git 公共目录中维护运行账本，无需额外依赖。
- `github-issue-autopilot/scripts/autopilot_admin.py` 幂等安装、列举、检查、停用、验收、放弃、重做、刷新 runtime 或卸载仓库循环；`issue_watcher.py` 负责全部合格 open Issue 的扫描、最多三任务领取、Orca 调度、attempt 账本和 Git 证据回读，远程发布策略固定为 `never`。
- `github-issue-workflow-update/scripts/update_workflow.py` 校验正式 Release 并事务式替换当前运行时；`scripts/build_release.py` 生成包含版本清单、逐文件哈希和归档校验和的可复现发布包。
- 管理员级 `doctor` 同时检查 watcher、Orca Automation 的启用状态与安全字段，以及是否残留重复的旧 LaunchAgent，全程只读并返回可诊断字段。每轮扫描包含启用前已创建但当前仍符合作者与标签策略的 open Issue；不可变 Issue node ID 防止活动中、待验收或需人工的任务重复派单，同时允许失败任务在 `max_attempts` 上限内于下一轮续做。

移除 Orca 项目或删除本地仓库前，先处理待验收和运行中的任务，再卸载自动化：

```sh
python3 github-issue-autopilot/scripts/autopilot_admin.py list
python3 github-issue-autopilot/scripts/autopilot_admin.py stop --repository owner/repo
python3 github-issue-autopilot/scripts/autopilot_admin.py discard --repo-path /absolute/repository/root --issue-url URL
python3 github-issue-autopilot/scripts/autopilot_admin.py uninstall --repository-id REPOSITORY_ID
```

仓库路径丢失、不再是原 Git 根目录或被移出 Orca 时，预检和协调器会自动暂停 Automation，不会删除账本或启动恢复 Agent。`list`、`stop`、`uninstall` 可在仓库已删除后按仓库名或不可变 ID 定位安装。`uninstall` 会拒绝未决任务，导出 Automation 历史，并把配置、SQLite、日志与 runtime 副本移入 `~/.local/state/github-issue-autopilot/archives/`；它不会删除本地仓库。

### 项目结构

```
.
├── LICENSE                        # MIT 许可证
├── README.md                      # 项目说明与安装文档
├── ROADMAP.md                     # Issue 创建到修复的分阶段计划
├── github-issue-handoff/          # Issue 创建与交接 Skill
├── github-issue-autopilot/        # 自动发现、领取与 Orca 可见任务调度
│   ├── SKILL.md
│   ├── references/configuration.md
│   ├── references/orca-automation.md
│   └── scripts/
│       ├── autopilot_admin.py     # 一句话安装与生命周期命令
│       └── issue_watcher.py       # 轮询、attempt 与防重复调度
├── github-issue-repair/           # Issue 分诊与修复 Skill
│   ├── SKILL.md
│   ├── agents/openai.yaml
│   ├── references/repair-contract.md
│   └── scripts/run_state.py       # 状态、审批与回执账本
├── github-issue-workflow-update/  # 最新稳定 Release 检查与事务式更新
│   ├── SKILL.md
│   ├── VERSION
│   └── scripts/update_workflow.py
├── scripts/build_release.py       # 构建带清单与校验和的正式发布包
├── .github/workflows/release.yml  # 通过版本 tag 测试并发布稳定包
└── tests/                         # 修复账本与自动调度单元测试
```

### 开发与验证

运行自动化测试和 Skill 校验：

```sh
python3 -m unittest discover -s tests -v
python3 /path/to/skill-creator/scripts/quick_validate.py github-issue-handoff
python3 /path/to/skill-creator/scripts/quick_validate.py github-issue-repair
python3 /path/to/skill-creator/scripts/quick_validate.py github-issue-autopilot
python3 /path/to/skill-creator/scripts/quick_validate.py github-issue-workflow-update
python3 scripts/build_release.py --tag v0.1.0 --commit "$(git rev-parse HEAD)" --output /tmp/issue-workflow-dist
```

真实 GitHub 操作还需通过 CLI 端到端回读：

- `gh repo view` 校验仓库信息与 Issues 可用性
- `gh issue create` 提交 Issue（正文经 stdin 或临时文件传递）
- `gh issue view` 回读并核对提交结果

### 成熟度

当前已提供单机 Orca 原生 Automation 一键安装、SQLite 幂等领取、Orca 可见子 worktree、可配置 Agent、最多三任务并发，以及基于稳定 Release 的整套本地更新与 runtime 回滚代码；更新入口要求 GitHub 上存在已发布的稳定 Release。成功后保留本地分支等待人工接受；失败任务保留可见现场，并在仍 open 且符合策略时进行有上限的下一轮续做。自动 draft PR 与多机调度仍未启用。普通用户进程属于“降低风险的隔离”，不是保护全部本地秘密的强安全边界；处理不可信仓库时应使用低权限账户或更强沙箱。完整门禁和扩展指标见 [ROADMAP.md](ROADMAP.md#中文)。

### 许可证

本项目使用 [MIT License](LICENSE)。

[English](#english) · [返回顶部](#github-issue-workflow)

---

<a id="english"></a>
## English

### Overview

This package contains four composable Skills with separate permission surfaces. `github-issue-handoff` turns repository context and user intent into an executable Issue. `github-issue-repair` repairs existing Issues in isolated worktrees. `github-issue-autopilot` discovers eligible open Issues and starts visible Agent tasks in Orca. `github-issue-workflow-update` updates the complete local installation from the latest stable Release.

When upgrading, remove the installed `github-issue-creator/` copy before installing `github-issue-handoff/`, and update explicit invocations to `$github-issue-handoff` so runtimes do not discover both skills.

### Features

- `github-issue-handoff` validates repositories, detects duplicates, applies Feature / Bug / Refactor / Research templates, and creates a Chinese Issue only after its executability gate passes.
- `github-issue-repair` normalizes Issues into work packages and models dependencies, duplicates, shared root causes, and conflicts; repository URLs default to read-only triage.
- `github-issue-autopilot` installs a local loop with one request, claims every open Issue created by the current user carrying `agent-ready`, including eligible backlog created before activation, and invokes `$github-issue-repair`.
- `github-issue-workflow-update` checks versions read-only when asked to check and replaces all four official Skills plus installed Autopilot runtimes when asked to update issue-workflow.
- Repair workers use Orca-managed child worktrees and never edit the user's active tree. Every eligible Issue is recorded, with at most three tasks running concurrently.
- An independent reviewer checks acceptance criteria, verification evidence, scope drift, and weakened tests.
- A two-level ledger deduplicates immutable Issue node IDs and records every attempt's selected Agent, Orca Task/Dispatch/worktree, branch, base/head SHAs, and outcome.
- Interactive repairs still require user approval. Autopilot may treat an Issue matching trusted local policy as approval for one low/medium-risk local repair. The installer only provisions a missing repository trigger label; repair runs never push, create PRs, write Issues, release, or deploy.

### Quick Start

#### Prerequisites

- A running Orca instance and at least one Agent Orca can launch, such as Codex, Claude, or OMP
- An authenticated GitHub CLI (`gh`)

#### Install

For the easiest setup, copy the following prompt directly into your agent session and let the agent install the skill for its runtime:

```text
Please install all four skills from this repository: https://github.com/Niall-Young/github-issue-workflow
```

Depending on the runtime and its permissions, the agent may request authorization or explain why it cannot install the skill automatically.

For manual installation, clone the repository and copy all four skill directories into the skill directory used by your runtime. The commands below use Claude Code's `~/.claude/skills/` as an example; restart the session after installation:

```sh
git clone https://github.com/Niall-Young/github-issue-workflow.git
cp -R github-issue-workflow/github-issue-handoff ~/.claude/skills/github-issue-handoff
cp -R github-issue-workflow/github-issue-repair ~/.claude/skills/github-issue-repair
cp -R github-issue-workflow/github-issue-autopilot ~/.claude/skills/github-issue-autopilot
cp -R github-issue-workflow/github-issue-workflow-update ~/.claude/skills/github-issue-workflow-update
```

#### Run

Create and repair one-off tasks in an Agent session; run the dispatcher locally for automatic execution:

```sh
$github-issue-handoff <GitHub URL>
$github-issue-repair <GitHub Issue URL>
$github-issue-repair <GitHub Repository URL>
python3 github-issue-autopilot/scripts/autopilot_admin.py install --repo-path /absolute/repository/root --agent codex
$github-issue-workflow-update update issue-workflow
```

### Usage

Minimal example. An explicit skill invocation authorizes creating one Issue:

```sh
$github-issue-handoff https://github.com/owner/repo Investigate occasional login API timeouts
```

Expected result: the skill produces a Chinese Issue from the Bug template, passes the pre-creation gate and deduplication check, submits it, and returns the title with a clickable Issue URL. If the report is security-sensitive, it follows the Security Policy's private channel instead of creating a public Issue.

Repair an existing Issue:

```sh
$github-issue-repair https://github.com/owner/repo/issues/123
```

Expected result: the skill first performs read-only analysis and presents the package, base SHA, risk, budget, and verification plan. It may edit locally only after scope approval, and may push and create a draft PR only after the user reviews the diff and evidence and authorizes publication. With only a repository URL, it proposes a capped candidate batch instead of repairing every Issue.

In any GitHub checkout, ask the Agent to “build an Issue loop.” Autopilot creates the missing `agent-ready` label, repository-isolated configuration, a native Orca Automation, and an Orca coordinator. The Automation can be paused, resumed, and inspected in the GUI. It prechecks every three minutes and invokes an Agent only when the coordinator needs recovery, so healthy checks consume no model turn. Every eligible Issue is recorded, and up to three run concurrently as visible child worktrees in the Orca sidebar. An unfinished `failed` attempt whose Issue remains open and eligible is retried on the next cycle, with two total automatic attempts by default; review-ready, explicit human-action, and policy-blocked results do not relaunch automatically. Each Issue attempt must use Orca Task, Dispatch, worktree ID, and absolute-path identities that no historical attempt or other Issue has used; reused identity or incomplete success evidence stops for human action without a manual or shared-worktree fallback. `--agent` selects the repository default; one `agent:ID` Issue label may override it, while conflicting, disallowed, or unavailable choices stop visibly for human action. Explicitly `accept` a satisfactory branch into a named clean target branch; after a successful merge, the command closes and reads back the matching Issue, preserving the worktree for a safe retry if closure fails. If the worktree was manually removed, closure proceeds only when the recorded repair head is already in the target branch history. An unsatisfactory attempt continues through explicit `retry`, which preserves the old attempt as history, may exceed the automatic limit, and creates a fresh Orca child worktree. When an old worktree still exists and is being discarded, `retry --discard-worktree` removes that exact recorded worktree. Standalone repair, manual `git worktree`, shared worktrees, manual merges, and ledger backfills are not substitutes. See [`configuration.md`](github-issue-autopilot/references/configuration.md).

### Update Mechanism

Updates never run automatically in the background. After `github-issue-workflow-update` has been installed once, the Agent invokes it only when the user explicitly asks to check or apply an update:

```sh
$github-issue-workflow-update check for issue-workflow updates
$github-issue-workflow-update update issue-workflow
```

- **Check** is read-only. It reports the installed version, latest stable version, and update availability without modifying a Skill or Autopilot installation.
- **Update** fetches the latest published stable Release from `Niall-Young/github-issue-workflow`. It reinstalls official files even when the version is unchanged, removing local drift and overwriting manual edits in the four managed Skills. Start a new conversation afterward so the Agent reloads the Skill instructions.
- The updater verifies the Release source, version, archive SHA-256, manifest, file allowlist, and every file hash, then performs a read-only preflight of all installed Autopilot runtimes. After preflight it transactionally swaps the four Skills, refreshes the runtimes, and reads back the Orca Automations and runtime files.
- Autopilot configuration, SQLite ledgers, logs, attempt history, and worktrees are preserved. A Skill, runtime, or Automation failure restores the previous state automatically. Only the rare `partial-update` result, where rollback itself also fails, requires manual recovery; the Agent must identify the affected component.
- The scope is limited to the four official Skills in the current Agent runtime and the installed Autopilot runtimes they manage. It never runs `git pull`, modifies a source checkout, updates an unrelated Skill, or scans or changes another Agent runtime.

On the publishing side, a `vMAJOR.MINOR.PATCH` tag triggers GitHub Actions to run the tests, build a reproducible bundle with a version manifest, per-file hashes, and an archive checksum, and publish a stable Release. The updater consumes only published stable Releases and rejects drafts, prereleases, downgrades, checksum failures, and malformed archives. A pushed tag that has not produced a stable Release is not installable. `v0.1.0` is the first declared release version.

### Configuration

- Each of the four Skills has `agents/openai.yaml` metadata and allows automatic discovery. Skill discovery is not authorization to edit code or write remotely.
- `github-issue-repair/scripts/run_state.py` uses only the Python standard library and stores its ledger under the repository's common Git directory.
- `github-issue-autopilot/scripts/autopilot_admin.py` idempotently installs, lists, checks, stops, accepts, discards, retries, refreshes runtimes, or uninstalls a repository loop. `issue_watcher.py` owns full eligible-open-Issue scans, three-slot claims, Orca dispatch, attempt history, and Git evidence readback; remote publication remains fixed to `never`.
- `github-issue-workflow-update/scripts/update_workflow.py` verifies and transactionally installs stable Releases. `scripts/build_release.py` creates reproducible assets containing a release manifest, per-file hashes, and an archive checksum.
- Administrator-level `doctor` checks the watcher, the Orca Automation's enabled state and safety fields, and any duplicate legacy LaunchAgent without mutating them. Every poll includes older open Issues that still match the author and label policy; immutable Issue node IDs prevent duplicate dispatch for active, review-ready, and human-stopped work while allowing failed work to continue on the next cycle within `max_attempts`.

Before removing an Orca project or deleting its checkout, settle review-ready and running work, then uninstall the Automation:

```sh
python3 github-issue-autopilot/scripts/autopilot_admin.py list
python3 github-issue-autopilot/scripts/autopilot_admin.py stop --repository owner/repo
python3 github-issue-autopilot/scripts/autopilot_admin.py discard --repo-path /absolute/repository/root --issue-url URL
python3 github-issue-autopilot/scripts/autopilot_admin.py uninstall --repository-id REPOSITORY_ID
```

If the path disappears, is no longer the original Git root, or is removed from Orca, the precheck and coordinator pause the Automation without deleting the ledger or launching a recovery Agent. `list`, `stop`, and `uninstall` can locate an installation by repository name or immutable ID after the checkout is gone. `uninstall` refuses unresolved work, exports Automation history, and moves configuration, SQLite, logs, and runtime copies into `~/.local/state/github-issue-autopilot/archives/`; it never deletes the local repository.

### Project Structure

```
.
├── LICENSE                        # MIT license
├── README.md                      # Project documentation and installation guide
├── ROADMAP.md                     # Phased plan from Issue creation to repair
├── github-issue-handoff/          # Issue creation and handoff Skill
├── github-issue-autopilot/        # Automatic detection, claims, and visible Orca dispatch
│   ├── SKILL.md
│   ├── references/configuration.md
│   ├── references/orca-automation.md
│   └── scripts/
│       ├── autopilot_admin.py     # One-request installation and lifecycle commands
│       └── issue_watcher.py       # Polling, attempts, and duplicate prevention
├── github-issue-repair/           # Issue triage and repair Skill
│   ├── SKILL.md
│   ├── agents/openai.yaml
│   ├── references/repair-contract.md
│   └── scripts/run_state.py       # State, approval, and receipt ledger
├── github-issue-workflow-update/  # Stable Release check and transactional update
│   ├── SKILL.md
│   ├── VERSION
│   └── scripts/update_workflow.py
├── scripts/build_release.py       # Build the manifest and checksummed release bundle
├── .github/workflows/release.yml  # Test and publish stable version tags
└── tests/                         # Repair-ledger and automatic-dispatch unit tests
```

### Development and Verification

Run the automated tests and Skill validators:

```sh
python3 -m unittest discover -s tests -v
python3 /path/to/skill-creator/scripts/quick_validate.py github-issue-handoff
python3 /path/to/skill-creator/scripts/quick_validate.py github-issue-repair
python3 /path/to/skill-creator/scripts/quick_validate.py github-issue-autopilot
python3 /path/to/skill-creator/scripts/quick_validate.py github-issue-workflow-update
python3 scripts/build_release.py --tag v0.1.0 --commit "$(git rev-parse HEAD)" --output /tmp/issue-workflow-dist
```

Real GitHub mutations still require CLI readback:

- `gh repo view` validates repository info and Issues availability
- `gh issue create` submits the Issue (body passed via stdin or a temporary file)
- `gh issue view` reads back and verifies the result

### Maturity

Single-machine installation through native Orca Automation, SQLite idempotent claims, visible Orca child worktrees, configurable Agents, up to three concurrent tasks, and the stable-Release updater with runtime rollback are implemented; the update entry point requires a published stable GitHub Release. Successful branches wait for human acceptance. Failed attempts preserve visible evidence and receive a bounded next-cycle retry while their Issue remains open and eligible. Automatic draft PRs and multi-runner scheduling remain disabled. A normal user process provides reduced isolation, not a strong boundary protecting all local secrets; use a low-privilege account or stronger sandbox for hostile repositories. See [ROADMAP.md](ROADMAP.md#english) for gates and expansion metrics.

### License

This project is licensed under the [MIT License](LICENSE).

[中文](#中文) · [Back to top](#github-issue-workflow)
