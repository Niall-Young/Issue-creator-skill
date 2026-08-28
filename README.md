# GitHub Issue Workflow

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

创建 Agent 可直接接手的 GitHub Issue，自动发现新任务，并在干净 Agent 上下文中完成受控修复。

Create agent-ready GitHub Issues, detect new assignments automatically, and run controlled repairs in clean agent contexts.

[中文](#中文) | [English](#english)

---

<a id="中文"></a>
## 中文

### 项目简介

这是一个面向 Agent 开发流程的技能包，包含三个权限独立、可组合的 Skill：`github-issue-handoff` 把仓库上下文和用户意图交接成可执行 Issue；`github-issue-repair` 在隔离 worktree 中修复既有 Issue；`github-issue-autopilot` 自动发现符合本地策略的 open Issue，并在 Orca 中启动可见的 Agent 任务。

从旧版本升级时，请删除运行时中的 `github-issue-creator/` 安装副本，再安装 `github-issue-handoff/`，并把显式调用改为 `$github-issue-handoff`，避免新旧技能同时被发现。

### 核心能力

- `github-issue-handoff`：校验仓库、检索重复项、套用 Feature / Bug / Refactor / Research 模板，通过可执行性门禁后创建并回读中文 Issue。
- `github-issue-repair`：把 Issue 归一化为工作包，识别依赖、重复、同根因与冲突关系；仓库 URL 默认只做只读分诊。
- `github-issue-autopilot`：一句话为当前仓库安装本地循环，领取由当前用户创建且带 `agent-ready` 的全部 open Issue，包括启用前已创建的积压任务，并调用 `$github-issue-repair`。
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
帮我安装这个 skill 仓库中的三个技能：https://github.com/Niall-Young/github-issue-workflow
```

Agent 可能会根据运行环境请求必要的授权，或说明无法自动安装时的限制。

如需手动安装，可克隆仓库，再将三个技能目录复制到 Agent 运行时的 skill 目录。以下命令以 Claude Code 的 `~/.claude/skills/` 为例；安装后请重新启动会话：

```sh
git clone https://github.com/Niall-Young/github-issue-workflow.git
cp -R github-issue-workflow/github-issue-handoff ~/.claude/skills/github-issue-handoff
cp -R github-issue-workflow/github-issue-repair ~/.claude/skills/github-issue-repair
cp -R github-issue-workflow/github-issue-autopilot ~/.claude/skills/github-issue-autopilot
```

#### 运行

单次创建和修复可在 Agent 会话中显式调用；自动调度器是本地常驻/定时命令：

```sh
$github-issue-handoff <GitHub URL>
$github-issue-repair <GitHub Issue URL>
$github-issue-repair <GitHub Repository URL>
python3 github-issue-autopilot/scripts/autopilot_admin.py install --repo-path /absolute/repository/root --agent codex
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

在任意 GitHub 项目中说“构建 Issue 循环检查机制”，Autopilot 会运行确定性安装器，创建缺失的 `agent-ready` 标签、仓库独立配置、Orca 原生 Automation 与 Orca 协调器。Automation 可在 GUI 中暂停、恢复和查看历史；它每三分钟预检一次，只有协调器缺失时才调用 Agent 恢复，不会每轮消耗模型。全部合格 Issue 都会入账，最多三个在 Orca 左侧作为项目子 worktree 同时运行。安装时用 `--agent` 选择默认 Agent；单个 Issue 可用唯一 `agent:ID` 标签覆盖，冲突、禁用或不可用的 Agent 会停在可见的人工处理状态。满意时明确 `accept` 到当前干净的目标分支；它会在合并成功后关闭并回读对应 Issue，关闭失败则保留 worktree 供安全重试。若 worktree 已被手工清理，只有记录的修复 head 已在目标分支历史中时才能补关。不满意时明确 `retry --discard-worktree`。详见 [`configuration.md`](github-issue-autopilot/references/configuration.md)。

### 配置

- 三个 Skill 的 `agents/openai.yaml` 提供 OpenAI 兼容 Agent 的界面配置并允许自动发现；技能发现本身不构成修改代码或远程写入授权。
- `github-issue-repair/scripts/run_state.py` 使用 Python 标准库，在仓库 Git 公共目录中维护运行账本，无需额外依赖。
- `github-issue-autopilot/scripts/autopilot_admin.py` 幂等安装、检查、停用、验收或重做仓库循环；`issue_watcher.py` 负责全部合格 open Issue 的扫描、最多三任务领取、Orca 调度、attempt 账本和 Git 证据回读，远程发布策略固定为 `never`。
- 管理员级 `doctor` 同时检查 watcher、Orca Automation 的启用状态与安全字段，以及是否残留重复的旧 LaunchAgent，全程只读并返回可诊断字段。每轮扫描包含启用前已创建但当前仍符合作者与标签策略的 open Issue，再由不可变 Issue node ID 去重，避免漏单和重复派单。

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
└── tests/                         # 修复账本与自动调度单元测试
```

### 开发与验证

运行自动化测试和 Skill 校验：

```sh
python3 -m unittest discover -s tests -v
python3 /path/to/skill-creator/scripts/quick_validate.py github-issue-handoff
python3 /path/to/skill-creator/scripts/quick_validate.py github-issue-repair
python3 /path/to/skill-creator/scripts/quick_validate.py github-issue-autopilot
```

真实 GitHub 操作还需通过 CLI 端到端回读：

- `gh repo view` 校验仓库信息与 Issues 可用性
- `gh issue create` 提交 Issue（正文经 stdin 或临时文件传递）
- `gh issue view` 回读并核对提交结果

### 成熟度

当前已提供单机 Orca 原生 Automation 一键安装、SQLite 幂等领取、Orca 可见子 worktree、可配置 Agent 和最多三任务并发。成功后保留本地分支等待人工接受，失败任务保留可见现场而不盲目重派。自动 draft PR 与多机调度仍未启用。普通用户进程属于“降低风险的隔离”，不是保护全部本地秘密的强安全边界；处理不可信仓库时应使用低权限账户或更强沙箱。完整门禁和扩展指标见 [ROADMAP.md](ROADMAP.md#中文)。

### 许可证

本项目使用 [MIT License](LICENSE)。

[English](#english) · [返回顶部](#github-issue-workflow)

---

<a id="english"></a>
## English

### Overview

This package contains three composable Skills with separate permission surfaces. `github-issue-handoff` turns repository context and user intent into an executable Issue. `github-issue-repair` repairs existing Issues in isolated worktrees. `github-issue-autopilot` discovers eligible open Issues and starts visible Agent tasks in Orca.

When upgrading, remove the installed `github-issue-creator/` copy before installing `github-issue-handoff/`, and update explicit invocations to `$github-issue-handoff` so runtimes do not discover both skills.

### Features

- `github-issue-handoff` validates repositories, detects duplicates, applies Feature / Bug / Refactor / Research templates, and creates a Chinese Issue only after its executability gate passes.
- `github-issue-repair` normalizes Issues into work packages and models dependencies, duplicates, shared root causes, and conflicts; repository URLs default to read-only triage.
- `github-issue-autopilot` installs a local loop with one request, claims every open Issue created by the current user carrying `agent-ready`, including eligible backlog created before activation, and invokes `$github-issue-repair`.
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
Please install all three skills from this repository: https://github.com/Niall-Young/github-issue-workflow
```

Depending on the runtime and its permissions, the agent may request authorization or explain why it cannot install the skill automatically.

For manual installation, clone the repository and copy all three skill directories into the skill directory used by your runtime. The commands below use Claude Code's `~/.claude/skills/` as an example; restart the session after installation:

```sh
git clone https://github.com/Niall-Young/github-issue-workflow.git
cp -R github-issue-workflow/github-issue-handoff ~/.claude/skills/github-issue-handoff
cp -R github-issue-workflow/github-issue-repair ~/.claude/skills/github-issue-repair
cp -R github-issue-workflow/github-issue-autopilot ~/.claude/skills/github-issue-autopilot
```

#### Run

Create and repair one-off tasks in an Agent session; run the dispatcher locally for automatic execution:

```sh
$github-issue-handoff <GitHub URL>
$github-issue-repair <GitHub Issue URL>
$github-issue-repair <GitHub Repository URL>
python3 github-issue-autopilot/scripts/autopilot_admin.py install --repo-path /absolute/repository/root --agent codex
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

In any GitHub checkout, ask the Agent to “build an Issue loop.” Autopilot creates the missing `agent-ready` label, repository-isolated configuration, a native Orca Automation, and an Orca coordinator. The Automation can be paused, resumed, and inspected in the GUI. It prechecks every three minutes and invokes an Agent only when the coordinator needs recovery, so healthy checks consume no model turn. Every eligible Issue is recorded, and up to three run concurrently as visible child worktrees in the Orca sidebar. `--agent` selects the repository default; one `agent:ID` Issue label may override it, while conflicting, disallowed, or unavailable choices stop visibly for human action. Explicitly `accept` a satisfactory branch into a named clean target branch; after a successful merge, the command closes and reads back the matching Issue, preserving the worktree for a safe retry if closure fails. If the worktree was manually removed, closure proceeds only when the recorded repair head is already in the target branch history. Use `retry --discard-worktree` for an unsatisfactory attempt. See [`configuration.md`](github-issue-autopilot/references/configuration.md).

### Configuration

- Each Skill's `agents/openai.yaml` provides OpenAI-compatible UI metadata and allows automatic discovery. Skill discovery is not authorization to edit code or write remotely.
- `github-issue-repair/scripts/run_state.py` uses only the Python standard library and stores its ledger under the repository's common Git directory.
- `github-issue-autopilot/scripts/autopilot_admin.py` idempotently installs, checks, stops, accepts, or retries a repository loop. `issue_watcher.py` owns full eligible-open-Issue scans, three-slot claims, Orca dispatch, attempt history, and Git evidence readback; remote publication remains fixed to `never`.
- Administrator-level `doctor` checks the watcher, the Orca Automation's enabled state and safety fields, and any duplicate legacy LaunchAgent without mutating them. Every poll includes older open Issues that still match the author and label policy, then relies on immutable Issue node IDs to prevent duplicate dispatch.

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
└── tests/                         # Repair-ledger and automatic-dispatch unit tests
```

### Development and Verification

Run the automated tests and Skill validators:

```sh
python3 -m unittest discover -s tests -v
python3 /path/to/skill-creator/scripts/quick_validate.py github-issue-handoff
python3 /path/to/skill-creator/scripts/quick_validate.py github-issue-repair
python3 /path/to/skill-creator/scripts/quick_validate.py github-issue-autopilot
```

Real GitHub mutations still require CLI readback:

- `gh repo view` validates repository info and Issues availability
- `gh issue create` submits the Issue (body passed via stdin or a temporary file)
- `gh issue view` reads back and verifies the result

### Maturity

Single-machine installation through native Orca Automation, SQLite idempotent claims, visible Orca child worktrees, configurable Agents, and up to three concurrent tasks are now available. Successful branches wait for human acceptance, while failed attempts preserve visible evidence instead of relaunching blindly. Automatic draft PRs and multi-runner scheduling remain disabled. A normal user process provides reduced isolation, not a strong boundary protecting all local secrets; use a low-privilege account or stronger sandbox for hostile repositories. See [ROADMAP.md](ROADMAP.md#english) for gates and expansion metrics.

### License

This project is licensed under the [MIT License](LICENSE).

[中文](#中文) · [Back to top](#github-issue-workflow)
