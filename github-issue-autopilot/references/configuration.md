# Autopilot configuration

For normal macOS/Orca setup, run the idempotent administrator from the installed Skill:

```sh
python3 scripts/autopilot_admin.py install \
  --repo-path /absolute/repository/root \
  --agent codex \
  --allow-agent claude
```

It creates the missing `agent-ready` GitHub label, a repository-specific state directory under `~/.local/state/github-issue-autopilot/`, a validated user LaunchAgent, and an untracked marker in the Git common directory. The LaunchAgent only ensures that Orca contains one visible `Issue Autopilot Coordinator` terminal. That terminal owns polling and supervised worker dispatch; no hidden Agent CLI fallback is permitted.

`--agent` is the repository default. Each `--allow-agent ID` adds an agent that an Issue may select with one `agent:ID` label. No agent label uses the default; exactly one label overrides it; conflicting, disallowed, or unavailable choices stop visibly at `needs-human`.

Version 2 has this shape:

```json
{
  "schema_version": 2,
  "state_db": "/Users/me/.local/state/github-issue-autopilot/state.sqlite3",
  "poll_interval_seconds": 180,
  "lease_timeout_seconds": 3000,
  "max_attempts": 2,
  "max_concurrent_workers": 3,
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
  "orca": {
    "cli": "/Users/me/.local/bin/orca",
    "default_agent": "codex",
    "allowed_agents": ["codex", "claude"],
    "setup": "run"
  }
}
```

`author` and `activate_after` are required. `@me` resolves through the authenticated GitHub CLI. The activation cutoff and advancing per-repository poll cursor prevent the first run from sweeping an old backlog or a later label edit from importing an older Issue. `repository_id` is optional but recommended to detect a renamed or transferred repository. Every configured intake label must be present.

`max_concurrent_workers` accepts 1–3. Polling records every eligible Issue; the coordinator starts only the available number of Orca workers and fills a free slot on a later cycle. Orca is the source of truth for Task, Dispatch, agent terminal, worktree lineage, comments, and workspace status. SQLite stores immutable Issue node IDs, attempt history, and those Orca IDs for recovery.

Workers run in visible `new-child` worktrees under the configured Orca project. They update comments at investigation, implementation, and verification checkpoints. A successful worker sends Orca `worker_done` and leaves one `AUTOPILOT_RESULT` in its final transcript. The watcher reads the preserved worker transcript and independently validates the worktree, branch, repair ledger, and base/head SHAs before marking it `ready-for-review`.

Successful worktrees remain `in-review` until explicit local acceptance. Accepting requires a clean, explicitly named target branch and never pushes. Retrying with `--discard-worktree` removes only the exact recorded Orca worktree after its Dispatch is no longer live. Remote publication remains fixed to `never`.
