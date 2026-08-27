# GitHub Issue Handoff

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

把 GitHub 仓库链接和任务描述整理成中文、Agent 可直接接手的 GitHub Issue。

Turn a GitHub repository link and task description into a Chinese, agent-ready GitHub Issue handoff.

[中文](#中文) | [English](#english)

---

<a id="中文"></a>
## 中文

### 项目简介

这是一个面向 Agent 工作流的技能包（Skill），供 Claude Code 等支持 skill 机制的运行时使用。它接收一条 GitHub 仓库链接和可选的任务描述，整理成一份结构完整、中文撰写、另一个 Agent 只凭 Issue 就能接手的 GitHub Issue。`Handoff` 强调它是一份经过验证的任务交接，而不只是创建一条记录。

当前版本只负责创建和回读 Issue，不会读取既有 Issue 后自动修改代码。后续自动修复能力将作为权限独立的 `github-issue-repair` 工作流开发，详见 [演进计划](ROADMAP.md#中文)。

从旧版本升级时，请删除运行时中的 `github-issue-creator/` 安装副本，再安装 `github-issue-handoff/`，并把显式调用改为 `$github-issue-handoff`，避免新旧技能同时被发现。

### 核心能力

- 自动解析并校验仓库（owner/repo）与 Issues 可用性，不信任字符串解析
- 按交付物类型（Feature / Bug / Refactor / Research）套用对应的 `github-issue-handoff/references/issue-templates.md` 模板
- 可执行性门禁：任务目标、仓库上下文、范围、验收标准、验证方式缺一不可，无法确认的事实标注 `待确认`
- 去重与安全检查：搜索已有 Issue，安全敏感报告改走仓库 Security Policy 的私有渠道
- 保持原子性：一个 Issue 对应一个可独立验证的成果，多任务先拆分再确认
- 创建后回读校验（`gh issue view`），确认标题与正文完整
- Issue 文案全中文，代码、命令、路径、日志保持原文
- 不擅自添加标签、指派、里程碑或项目

### 快速开始

#### 环境要求

- 支持 skill 机制的 Agent 运行时（如 Claude Code）
- 已认证的 GitHub CLI（`gh`）

#### 安装

推荐将下面的提示词直接复制到你的 Agent 会话中，让 Agent 根据其运行环境完成安装：

```text
帮我安装这个 skill：https://github.com/Niall-Young/Issue-creator-skill
```

Agent 可能会根据运行环境请求必要的授权，或说明无法自动安装时的限制。

如需手动安装，可克隆仓库，再将其中的 `github-issue-handoff/` 技能目录复制到你的 Agent 运行时所使用的 skill 目录。以下命令以 Claude Code 的 `~/.claude/skills/` 为例；安装后请重新启动会话：

```sh
git clone https://github.com/Niall-Young/Issue-creator-skill.git
cp -R Issue-creator-skill/github-issue-handoff ~/.claude/skills/github-issue-handoff
```

#### 运行

无需单独运行。在 Agent 会话中以显式方式调用技能即可：

```sh
$github-issue-handoff <GitHub URL>
```

### 使用方法

给出最小示例。显式调用技能即授权创建一条 Issue：

```sh
$github-issue-handoff https://github.com/owner/repo 帮我排查登录接口偶发超时的问题
```

预期结果：技能按 Bug 模板生成一份中文 Issue，通过创建前门禁与去重检查后提交，并返回标题与可点击的 Issue URL。若仓库存在安全风险，则按 Security Policy 走私有上报渠道，不创建公开 Issue。

### 配置

- `github-issue-handoff/agents/openai.yaml`：提供 OpenAI 兼容 Agent 的界面配置（显示名、默认提示词），并声明允许隐式调用。技能本身无需额外配置即可使用。

### 项目结构

```
.
├── LICENSE                        # MIT 许可证
├── README.md                      # 项目说明与安装文档
├── ROADMAP.md                     # Issue 创建到修复的分阶段计划
└── github-issue-handoff/          # 可独立安装的技能目录
    ├── SKILL.md                   # 技能定义与执行流程
    ├── agents/
    │   └── openai.yaml            # OpenAI 兼容 Agent 界面配置
    └── references/
        └── issue-templates.md     # Feature / Bug / Refactor / Research 模板
```

### 开发与验证

本项目当前无自动化测试。验证通过 GitHub CLI 端到端进行：

- `gh repo view` 校验仓库信息与 Issues 可用性
- `gh issue create` 提交 Issue（正文经 stdin 或临时文件传递）
- `gh issue view` 回读并核对提交结果

### 下一阶段

新增独立的 `github-issue-repair`，先完成单 Issue、单工作包、串行执行的安全闭环，再根据真实验收数据逐步开放仓库级分诊和有限并发。Issue 创建不会隐式触发代码修改、远程推送或 PR。完整阶段、门禁和验收指标见 [ROADMAP.md](ROADMAP.md#中文)。

### 许可证

本项目使用 [MIT License](LICENSE)。

[English](#english) · [返回顶部](#github-issue-handoff)

---

<a id="english"></a>
## English

### Overview

This is a skill package for agent workflows, used by skill-capable runtimes such as Claude Code. It takes a GitHub repository link and an optional task description, and produces a well-structured, Chinese-written GitHub Issue that another agent can pick up using only the Issue. `Handoff` emphasizes a verified task transfer, not merely creating a record.

The current release only creates and reads back Issues. It does not read an existing Issue and modify code automatically. That higher-permission capability is planned as a separate `github-issue-repair` workflow; see the [roadmap](ROADMAP.md#english).

When upgrading, remove the installed `github-issue-creator/` copy before installing `github-issue-handoff/`, and update explicit invocations to `$github-issue-handoff` so runtimes do not discover both skills.

### Features

- Parses and validates the repository (owner/repo) and Issues availability, without trusting string parsing
- Applies the matching `github-issue-handoff/references/issue-templates.md` template by deliverable type (Feature / Bug / Refactor / Research)
- Executability gate: task goal, repository context, scope, acceptance criteria, and validation are required; unverifiable facts are marked `待确认`
- Deduplication and safety checks: searches existing Issues; security-sensitive reports go through the repository's private Security Policy channel
- Keeps tasks atomic: one Issue per independently verifiable outcome; splits multi-task requests before confirmation
- Reads back the created Issue with `gh issue view` to confirm the title and body are complete
- Issue prose is fully Chinese; code, commands, paths, and logs stay in their original form
- Does not add labels, assignees, milestones, or projects unless explicitly requested

### Quick Start

#### Prerequisites

- A skill-capable agent runtime (such as Claude Code)
- An authenticated GitHub CLI (`gh`)

#### Install

For the easiest setup, copy the following prompt directly into your agent session and let the agent install the skill for its runtime:

```text
Please install this skill for me: https://github.com/Niall-Young/Issue-creator-skill
```

Depending on the runtime and its permissions, the agent may request authorization or explain why it cannot install the skill automatically.

For manual installation, clone the repository and copy its `github-issue-handoff/` directory into the skill directory used by your agent runtime. The commands below use Claude Code's `~/.claude/skills/` as an example; restart the session after installation:

```sh
git clone https://github.com/Niall-Young/Issue-creator-skill.git
cp -R Issue-creator-skill/github-issue-handoff ~/.claude/skills/github-issue-handoff
```

#### Run

Nothing to run directly. Invoke the skill explicitly in the agent session:

```sh
$github-issue-handoff <GitHub URL>
```

### Usage

Minimal example. An explicit skill invocation authorizes creating one Issue:

```sh
$github-issue-handoff https://github.com/owner/repo Investigate occasional login API timeouts
```

Expected result: the skill produces a Chinese Issue from the Bug template, passes the pre-creation gate and deduplication check, submits it, and returns the title with a clickable Issue URL. If the report is security-sensitive, it follows the Security Policy's private channel instead of creating a public Issue.

### Configuration

- `github-issue-handoff/agents/openai.yaml`: provides the interface configuration for an OpenAI-compatible agent (display name, default prompt) and declares implicit invocation as allowed. The skill itself works without extra configuration.

### Project Structure

```
.
├── LICENSE                        # MIT license
├── README.md                      # Project documentation and installation guide
├── ROADMAP.md                     # Phased plan from Issue creation to repair
└── github-issue-handoff/          # Independently installable skill directory
    ├── SKILL.md                   # Skill definition and workflow
    ├── agents/
    │   └── openai.yaml            # OpenAI-compatible agent interface config
    └── references/
        └── issue-templates.md     # Feature / Bug / Refactor / Research templates
```

### Development and Verification

This project has no automated tests. Verification is end-to-end via the GitHub CLI:

- `gh repo view` validates repository info and Issues availability
- `gh issue create` submits the Issue (body passed via stdin or a temporary file)
- `gh issue view` reads back and verifies the result

### Next Stage

Add a separate `github-issue-repair` workflow. It will first prove a safe, sequential loop for one Issue and one work package, then earn repository-level triage and bounded concurrency through measured results. Issue creation will never implicitly trigger code changes, remote pushes, or PR publication. See [ROADMAP.md](ROADMAP.md#english) for phases, gates, and success metrics.

### License

This project is licensed under the [MIT License](LICENSE).

[中文](#中文) · [Back to top](#github-issue-handoff)
