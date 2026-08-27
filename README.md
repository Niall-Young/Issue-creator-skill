# GitHub Issue Workflow

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

创建 Agent 可直接接手的 GitHub Issue，自动发现新任务，并在干净 Agent 上下文中完成受控修复。

Create agent-ready GitHub Issues, detect new assignments automatically, and run controlled repairs in clean agent contexts.

[中文](#中文) | [English](#english)

---

<a id="中文"></a>
## 中文

### 项目简介

这是一个面向 Agent 开发流程的技能包，包含三个权限独立、可组合的 Skill：`github-issue-handoff` 把仓库上下文和用户意图交接成可执行 Issue；`github-issue-repair` 在隔离 worktree 中修复既有 Issue；`github-issue-autopilot` 自动发现符合本地策略的新 Issue，并为每条任务启动全新的 Agent 进程。

从旧版本升级时，请删除运行时中的 `github-issue-creator/` 安装副本，再安装 `github-issue-handoff/`，并把显式调用改为 `$github-issue-handoff`，避免新旧技能同时被发现。

### 核心能力

- `github-issue-handoff`：校验仓库、检索重复项、套用 Feature / Bug / Refactor / Research 模板，通过可执行性门禁后创建并回读中文 Issue。
- `github-issue-repair`：把 Issue 归一化为工作包，识别依赖、重复、同根因与冲突关系；仓库 URL 默认只做只读分诊。
- `github-issue-autopilot`：轮询允许的仓库，按作者、创建时间和可选标签筛选 Issue，用 SQLite WAL 原子领取任务，并调用 `$github-issue-repair`。
- 修复 worker 使用独立 worktree，不修改用户正在使用的工作树；MVP 串行执行，成熟批次最多并发 3 个低交互风险工作包。
- 独立 reviewer 核对验收标准、验证证据、范围漂移和测试弱化。
- 确定性账本记录 base/head SHA、计划、审批、状态迁移和远程回执，支持中断恢复与幂等发布。
- 交互模式仍由用户批准本地修改；Autopilot 可把符合可信本地策略的 Issue 视为一次低/中风险本地修复授权。发布 draft PR 仍需另行授权；不自动合并、关闭 Issue、评论、打标签、发布或部署。

### 快速开始

#### 环境要求

- 支持 Skill 且可无持久会话调用的 Agent CLI（如 Codex `exec --ephemeral`）
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
python3 github-issue-autopilot/scripts/issue_watcher.py once --config /absolute/path/autopilot.json
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

自动执行新 Issue：按 [`github-issue-autopilot/references/configuration.md`](github-issue-autopilot/references/configuration.md) 创建本地 JSON 配置，必须设置仓库白名单、作者、`activate_after`、本地仓库路径和 fresh-session executor。先运行 `doctor`，再用 `once` 试跑；确认后按 [`launchd.md`](github-issue-autopilot/references/launchd.md) 每三分钟执行一次。已有历史 Issue 不会越过 `activate_after` 自动入队；Issue 编辑也不会重复触发，需用 `retry` 明确重跑。

### 配置

- 三个 Skill 的 `agents/openai.yaml` 提供 OpenAI 兼容 Agent 的界面配置并允许自动发现；技能发现本身不构成修改代码或远程写入授权。
- `github-issue-repair/scripts/run_state.py` 使用 Python 标准库，在仓库 Git 公共目录中维护运行账本，无需额外依赖。
- `github-issue-autopilot/scripts/issue_watcher.py` 仅使用 Python 标准库和 `gh`，把调度状态与日志保存在受管仓库之外；默认串行，远程发布策略固定为 `never`。

### 项目结构

```
.
├── LICENSE                        # MIT 许可证
├── README.md                      # 项目说明与安装文档
├── ROADMAP.md                     # Issue 创建到修复的分阶段计划
├── github-issue-handoff/          # Issue 创建与交接 Skill
├── github-issue-autopilot/        # 自动发现、领取与 fresh-process 调度
│   ├── SKILL.md
│   ├── references/configuration.md
│   ├── references/launchd.md
│   └── scripts/issue_watcher.py
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

当前已提供单机 macOS 轮询、SQLite 幂等领取和 fresh-process 执行。默认一次只运行一个 Issue，且只自动授权低/中风险的本地实施与验证。自动 draft PR、多机调度和有限并发仍需真实仓库验收数据后再启用。普通 macOS 用户进程属于“降低风险的隔离”，不是保护全部本地秘密的强安全边界；处理不可信仓库时应使用低权限账户或更强沙箱。完整门禁和扩展指标见 [ROADMAP.md](ROADMAP.md#中文)。

### 许可证

本项目使用 [MIT License](LICENSE)。

[English](#english) · [返回顶部](#github-issue-workflow)

---

<a id="english"></a>
## English

### Overview

This package contains three composable Skills with separate permission surfaces. `github-issue-handoff` turns repository context and user intent into an executable Issue. `github-issue-repair` repairs existing Issues in isolated worktrees. `github-issue-autopilot` discovers eligible new Issues and starts each assignment in a fresh Agent process.

When upgrading, remove the installed `github-issue-creator/` copy before installing `github-issue-handoff/`, and update explicit invocations to `$github-issue-handoff` so runtimes do not discover both skills.

### Features

- `github-issue-handoff` validates repositories, detects duplicates, applies Feature / Bug / Refactor / Research templates, and creates a Chinese Issue only after its executability gate passes.
- `github-issue-repair` normalizes Issues into work packages and models dependencies, duplicates, shared root causes, and conflicts; repository URLs default to read-only triage.
- `github-issue-autopilot` polls allowlisted repositories, filters by author, creation cutoff, and optional labels, atomically claims work in a SQLite WAL ledger, and invokes `$github-issue-repair`.
- Repair workers use isolated worktrees and never edit the user's active tree. The MVP is sequential; a mature batch may run at most three low-interaction-risk packages concurrently.
- An independent reviewer checks acceptance criteria, verification evidence, scope drift, and weakened tests.
- A deterministic ledger records base/head SHAs, plans, approvals, state transitions, and remote receipts for recovery and idempotent publication.
- Interactive repairs still require user approval. Autopilot may treat an Issue matching trusted local policy as approval for one low/medium-risk local repair. Draft-PR publication remains separately authorized; the workflow never auto-merges, closes, comments, labels, releases, or deploys.

### Quick Start

#### Prerequisites

- A skill-capable Agent CLI with a non-persistent invocation mode (such as Codex `exec --ephemeral`)
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
python3 github-issue-autopilot/scripts/issue_watcher.py once --config /absolute/path/autopilot.json
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

To execute new Issues automatically, create a local JSON policy from [`configuration.md`](github-issue-autopilot/references/configuration.md). It must define the repository allowlist, author, `activate_after`, local clone, and fresh-session executor. Run `doctor`, pilot with `once`, then schedule it every three minutes using [`launchd.md`](github-issue-autopilot/references/launchd.md). Historical Issues before the cutoff are not imported, edits do not retrigger completed work, and reruns require the explicit `retry` command.

### Configuration

- Each Skill's `agents/openai.yaml` provides OpenAI-compatible UI metadata and allows automatic discovery. Skill discovery is not authorization to edit code or write remotely.
- `github-issue-repair/scripts/run_state.py` uses only the Python standard library and stores its ledger under the repository's common Git directory.
- `github-issue-autopilot/scripts/issue_watcher.py` uses only the Python standard library plus `gh`, and stores dispatcher state and logs outside managed repositories. It is sequential by default and hard-codes remote publication to `never`.

### Project Structure

```
.
├── LICENSE                        # MIT license
├── README.md                      # Project documentation and installation guide
├── ROADMAP.md                     # Phased plan from Issue creation to repair
├── github-issue-handoff/          # Issue creation and handoff Skill
├── github-issue-autopilot/        # Automatic detection, claims, and fresh-process dispatch
│   ├── SKILL.md
│   ├── references/configuration.md
│   ├── references/launchd.md
│   └── scripts/issue_watcher.py
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

Single-machine macOS polling, SQLite idempotent claims, and fresh-process execution are now available. The default is one Issue at a time, with automatic authorization limited to low/medium-risk local implementation and verification. Automatic draft PRs, multi-runner scheduling, and bounded concurrency still require acceptance evidence from real repositories. A normal macOS user process provides reduced isolation, not a strong boundary protecting all local secrets; use a low-privilege account or stronger sandbox for hostile repositories. See [ROADMAP.md](ROADMAP.md#english) for gates and expansion metrics.

### License

This project is licensed under the [MIT License](LICENSE).

[中文](#中文) · [Back to top](#github-issue-workflow)
