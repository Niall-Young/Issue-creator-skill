# Autopilot configuration

For normal macOS/Codex setup, run the idempotent administrator from the installed Skill:

```sh
python3 scripts/autopilot_admin.py install --repo-path /absolute/repository/root
```

It creates the missing `agent-ready` GitHub label, a repository-specific state directory under `~/.local/state/github-issue-autopilot/`, a validated user LaunchAgent, and an untracked marker in the Git common directory. Use the manual JSON form below for other executors or platforms.

Use an absolute path for manual JSON configuration and keep it outside the repository. Version 1 has this shape:

```json
{
  "schema_version": 1,
  "state_db": "/Users/me/.local/state/github-issue-autopilot/state.sqlite3",
  "poll_interval_seconds": 180,
  "lease_timeout_seconds": 3000,
  "max_attempts": 2,
  "max_dispatch_per_poll": 1,
  "policy": {
    "scope_approval": "eligible-issue",
    "publication": "never",
    "max_risk": "medium"
  },
  "repositories": [
    {
      "repository": "owner/repo",
      "repository_id": "R_kgDOExample",
      "repo_path": "/absolute/path/to/local/clone",
      "author": "@me",
      "activate_after": "2026-08-27T00:00:00Z",
      "labels": ["agent-ready"]
    }
  ],
  "executor": {
    "timeout_seconds": 2700,
    "argv": [
      "/Users/me/.local/bin/codex",
      "exec",
      "--ephemeral",
      "--approve-for-me",
      "--skip-git-repo-check",
      "-C",
      "{repo_path}",
      "-"
    ]
  }
}
```

`author` and `activate_after` are required. `@me` is resolved through the authenticated GitHub CLI. The activation cutoff and advancing per-repository poll cursor prevent the first run from sweeping an old backlog or a later label edit from importing an older Issue. `repository_id` is optional but recommended to detect a renamed or transferred repository. Every configured label must be present; the administrator defaults to `agent-ready`.

Supported `argv` placeholders are `{repository}`, `{repo_path}`, `{issue_url}`, and `{issue_number}`. The generated prompt is sent on stdin; Issue title or body never becomes an argument. The executable must be a fresh-session command such as Codex `exec --ephemeral` or Claude Code with session persistence disabled.

Use an absolute executor path when running under `launchd`, whose default `PATH` is intentionally small. The Codex example uses `--approve-for-me`, which selects the workspace-write sandbox and routes ordinary approval requests through automatic review. Do not add a separate `--sandbox` argument: Codex rejects that option when combined with `--approve-for-me`. Automatic approval does not authorize the remote operations forbidden by the Autopilot policy.

`lease_timeout_seconds` must be greater than `executor.timeout_seconds`. `publication` currently accepts only `never`; automatic draft-PR publication should be added only with a separate credential and receipt design. The state database and logs are local runtime artifacts, not repository files.

The watcher stores immutable GitHub Issue node IDs and separate attempt rows in a SQLite WAL ledger. `open` matters only at initial discovery and pre-execution revalidation. Repeated polls, body edits, later label changes, reopened Issues, running workers, and work waiting for review do not enqueue another run. A dead stale worker stops at `needs-human`; use an explicit retry for another numbered attempt.

Successful attempts remain `ready-for-review` with their isolated worktree and branch. Accepting one requires a clean, explicitly named local target branch and never pushes. Rejecting one with `retry --discard-worktree` removes only the exact recorded worktree and `repair/` branch, records the old attempt as rejected, and queues a new attempt number.

The generated child prompt identifies the verified eligibility policy, invokes `$github-issue-repair` with the canonical Issue URL, forbids remote writes, and requires a structured `AUTOPILOT_RESULT` containing the run ID, worktree, branch, and base/head SHAs. The watcher validates those artifacts with Git before declaring the attempt ready for review. On an ordinary macOS user account this is reduced isolation: a fresh process and Agent sandbox do not by themselves protect every unrelated local secret. Use a dedicated low-privilege account or stronger sandbox for hostile repositories.
