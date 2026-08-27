# GitHub Issue Creator

把 GitHub 仓库链接和任务描述整理成中文、Agent 可直接执行的 GitHub Issue。

Turn a GitHub repository link and task description into a Chinese, agent-ready GitHub Issue.

[中文](#中文) | [English](#english)

---

<a id="中文"></a>
## 中文

### 项目简介

这是一个面向 Agent 工作流的技能包（Skill），供 Claude Code 等支持 skill 机制的运行时使用。它接收一条 GitHub 仓库链接和可选的任务描述，整理成一份结构完整、中文撰写、另一个 Agent 只凭 Issue 就能执行的 GitHub Issue。适合把开发 TODO 或干净上下文的交接写成可执行的 Issue。

### 核心能力

- 自动解析并校验仓库（owner/repo）与 Issues 可用性，不信任字符串解析
- 按交付物类型（Feature / Bug / Refactor / Research）套用对应的 `references/issue-templates.md` 模板
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

将本技能放入 `~/.claude/skills/`（或项目的 `.claude/skills/`），重新启动会话即可被发现：

```sh
git clone https://github.com/Niall-Young/Issue-creator-skill.git ~/.claude/skills/Issue-creator-skill
```

#### 运行

无需单独运行。在 Agent 会话中以显式方式调用技能即可：

```sh
$github-issue-creator <GitHub URL>
```

### 使用方法

给出最小示例。显式调用技能即授权创建一条 Issue：

```sh
$github-issue-creator https://github.com/owner/repo 帮我排查登录接口偶发超时的问题
```

预期结果：技能按 Bug 模板生成一份中文 Issue，通过创建前门禁与去重检查后提交，并返回标题与可点击的 Issue URL。若仓库存在安全风险，则按 Security Policy 走私有上报渠道，不创建公开 Issue。

### 配置

- `agents/openai.yaml`：提供 OpenAI 兼容 Agent 的界面配置（显示名、默认提示词），并声明允许隐式调用。技能本身无需额外配置即可使用。

### 项目结构

```
.
├── SKILL.md                       # 技能定义与执行流程
├── agents/
│   └── openai.yaml                # OpenAI 兼容 Agent 界面配置
└── references/
    └── issue-templates.md         # Feature / Bug / Refactor / Research 模板
```

### 开发与验证

本项目当前无自动化测试。验证通过 GitHub CLI 端到端进行：

- `gh repo view` 校验仓库信息与 Issues 可用性
- `gh issue create` 提交 Issue（正文经 stdin 或临时文件传递）
- `gh issue view` 回读并核对提交结果

### 许可证

仓库未声明许可证。

[English](#english) · [返回顶部](#github-issue-creator)

---

<a id="english"></a>
## English

### Overview

This is a skill package for agent workflows, used by skill-capable runtimes such as Claude Code. It takes a GitHub repository link and an optional task description, and produces a well-structured, Chinese-written GitHub Issue that another agent can execute using only the Issue. It is suited for turning development TODOs or clean-context handoffs into executable Issues.

### Features

- Parses and validates the repository (owner/repo) and Issues availability, without trusting string parsing
- Applies the matching `references/issue-templates.md` template by deliverable type (Feature / Bug / Refactor / Research)
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

Place this skill under `~/.claude/skills/` (or a project's `.claude/skills/`), then restart the session for auto-discovery:

```sh
git clone https://github.com/Niall-Young/Issue-creator-skill.git ~/.claude/skills/Issue-creator-skill
```

#### Run

Nothing to run directly. Invoke the skill explicitly in the agent session:

```sh
$github-issue-creator <GitHub URL>
```

### Usage

Minimal example. An explicit skill invocation authorizes creating one Issue:

```sh
$github-issue-creator https://github.com/owner/repo Investigate occasional login API timeouts
```

Expected result: the skill produces a Chinese Issue from the Bug template, passes the pre-creation gate and deduplication check, submits it, and returns the title with a clickable Issue URL. If the report is security-sensitive, it follows the Security Policy's private channel instead of creating a public Issue.

### Configuration

- `agents/openai.yaml`: provides the interface configuration for an OpenAI-compatible agent (display name, default prompt) and declares implicit invocation as allowed. The skill itself works without extra configuration.

### Project Structure

```
.
├── SKILL.md                       # Skill definition and workflow
├── agents/
│   └── openai.yaml                # OpenAI-compatible agent interface config
└── references/
    └── issue-templates.md         # Feature / Bug / Refactor / Research templates
```

### Development and Verification

This project has no automated tests. Verification is end-to-end via the GitHub CLI:

- `gh repo view` validates repository info and Issues availability
- `gh issue create` submits the Issue (body passed via stdin or a temporary file)
- `gh issue view` reads back and verifies the result

### License

No license is declared in the repository.

[中文](#中文) · [Back to top](#github-issue-creator)
