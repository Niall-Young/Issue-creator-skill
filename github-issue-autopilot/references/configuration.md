# Autopilot configuration

Use an absolute path for the JSON configuration and keep it outside the repository when it contains machine-specific paths. Version 1 has this shape:

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
      "labels": []
    }
  ],
  "executor": {
    "timeout_seconds": 2700,
    "argv": [
      "/Users/me/.local/bin/codex",
      "exec",
      "--ephemeral",
      "--sandbox",
      "workspace-write",
      "--approve-for-me",
      "--skip-git-repo-check",
      "-C",
      "{repo_path}",
      "-"
    ]
  }
}
```

`author` and `activate_after` are required. `@me` is resolved through the authenticated GitHub CLI. The activation cutoff prevents the first run from sweeping an old backlog. `repository_id` is optional but recommended to detect a renamed or transferred repository. Every configured label must be present. Use a dedicated label when not every owner-authored Issue should start work.

Supported `argv` placeholders are `{repository}`, `{repo_path}`, `{issue_url}`, and `{issue_number}`. The generated prompt is sent on stdin; Issue title or body never becomes an argument. The executable must be a fresh-session command such as Codex `exec --ephemeral` or Claude Code with session persistence disabled.

Use an absolute executor path when running under `launchd`, whose default `PATH` is intentionally small. The Codex example uses `--approve-for-me` so an unattended run can pass ordinary workspace-write approvals through automatic review; it does not authorize the remote operations forbidden by the Autopilot policy.

`lease_timeout_seconds` must be greater than `executor.timeout_seconds`. `publication` currently accepts only `never`; automatic draft-PR publication should be added only with a separate credential and receipt design. The state database and logs are local runtime artifacts, not repository files.

The watcher stores immutable GitHub Issue node IDs in a SQLite WAL ledger. Repeated polls and body edits do not enqueue another automatic run. Use the explicit `retry` command for another attempt. A stale lease can be reclaimed only after its process is gone and the attempt budget permits it.

The generated child prompt identifies the verified eligibility policy, invokes `$github-issue-repair` with the canonical Issue URL, forbids remote writes, and requires an `AUTOPILOT_RESULT` receipt. The repair agent still validates the repository and Issue with `gh` and stops when the task exceeds policy. On an ordinary macOS user account this is reduced isolation: a fresh process and Agent sandbox do not by themselves protect every unrelated local secret. Use a dedicated low-privilege account or stronger sandbox for hostile repositories.
