# Autopilot configuration

For normal Orca setup, run the idempotent administrator from the installed Skill:

```sh
python3 scripts/autopilot_admin.py install \
  --repo-path /absolute/repository/root \
  --agent codex \
  --allow-agent claude
```

It creates the missing `agent-ready` GitHub label, a repository-specific state directory under `~/.local/state/github-issue-autopilot/`, a native Orca Automation, and an untracked marker in the Git common directory. The GUI Automation checks every three minutes and invokes an Agent only when the visible `Issue Autopilot Coordinator` terminal needs recovery. That terminal owns polling and supervised worker dispatch; no hidden Agent CLI fallback is permitted.

`--agent` is the repository default. Each `--allow-agent ID` adds an agent that an Issue may select with one `agent:ID` label. No agent label uses the default; exactly one label overrides it; conflicting, disallowed, or unavailable choices stop visibly at `needs-human`.

Version 3 has this shape:

```json
{
  "schema_version": 3,
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
      "labels": ["agent-ready"]
    }
  ],
  "orca": {
    "cli": "/Users/me/.local/bin/orca",
    "default_agent": "codex",
    "allowed_agents": ["codex", "claude"],
    "setup": "run"
  },
  "scheduler": {
    "backend": "orca-automation",
    "automation_id": "generated-by-orca",
    "name": "Issue Autopilot · owner/repo · stable-key",
    "trigger": "*/3 * * * *",
    "provider": "codex",
    "workspace_mode": "existing",
    "session_mode": "fresh",
    "precheck_timeout_seconds": 60,
    "missed_run_grace_minutes": 5
  }
}
```

`author` is required, and `@me` resolves through the authenticated GitHub CLI. Every poll scans the current open Issues that match the configured author and all intake labels, including matching Issues created before installation. Immutable Issue node IDs in the SQLite ledger make repeated scans idempotent, so an Issue is queued only once unless the user explicitly retries it. Existing configurations may retain `activate_after` as ignored compatibility metadata. `repository_id` is optional but recommended to detect a renamed or transferred repository.

`max_concurrent_workers` accepts 1–3. Polling records every eligible Issue; the coordinator starts only the available number of Orca workers and fills a free slot on a later cycle. Orca is the source of truth for Task, Dispatch, agent terminal, worktree lineage, comments, and workspace status. SQLite stores immutable Issue node IDs, attempt history, and those Orca IDs for recovery.

Workers run in visible `new-child` worktrees under the configured Orca project. They update comments at investigation, implementation, and verification checkpoints. A successful worker sends Orca `worker_done` and leaves one `AUTOPILOT_RESULT` in its final transcript. The watcher reads the preserved worker transcript and independently validates the worktree, branch, repair ledger, and base/head SHAs before marking it `ready-for-review`.

Every attempt must receive a fresh Orca Task ID, Dispatch ID, worktree ID, and absolute worktree path that have never appeared in another historical attempt or Issue. The ledger rejects identity reuse before recording the new dispatch. An already-ledgered Issue may continue only through explicit `retry` (or finish through `accept`); standalone repairs, manual `git worktree` creation, shared worktrees, manual merges, and ledger backfills are not substitutes. Missing Orca evidence or an unavailable Orca runtime stops visibly at `needs-human` without a fallback.

Successful worktrees remain `in-review` until explicit acceptance. Accepting requires a clean, explicitly named target branch, merges without pushing, and closes the exact configured GitHub Issue only after the merge succeeds. If a repair was merged manually and its worktree is gone, acceptance continues only when Git proves the recorded reviewed head is already an ancestor of the checked-out target. The command reads the Issue back as closed before recording acceptance or cleaning the worktree; a closure failure remains retryable. Retrying with `--discard-worktree` removes only the exact recorded Orca worktree after its Dispatch is no longer live. Automated publication remains fixed to `never`.

The scheduler fields are installer-owned safety settings. Pausing and resuming in Orca's GUI is supported; changing the trigger, provider, workspace, prompt, or precheck makes `doctor` report configuration drift. See [`orca-automation.md`](orca-automation.md) for migration, recovery, and pause behavior.

## Repository removal and uninstall

List every active installation before removing a checkout:

```sh
python3 scripts/autopilot_admin.py list
python3 scripts/autopilot_admin.py stop --repository owner/repo
```

`list`, `stop`, and `uninstall` can select by `--repo-path`, `--repository owner/repo`, or immutable `--repository-id`. The name and ID selectors read the repository-isolated configuration under `~/.local/state/github-issue-autopilot/`, so they still work after the checkout path is gone.

If the precheck or running coordinator finds that the path is missing, is no longer the exact Git root, or is no longer registered in Orca, it disables the Automation and returns `paused-missing-workspace`. This is a fail-safe pause: the SQLite ledger, logs, branches, and worktrees are preserved.

Settle every outstanding result before uninstalling. `accept` keeps a reviewed result; `discard` explicitly abandons one attempt after proving that neither its local PID nor its Orca Dispatch is live. `discard` removes only the recorded worktree when present and records a terminal `discarded` state; it does not queue another attempt.

```sh
python3 scripts/autopilot_admin.py discard \
  --repo-path /absolute/repository/root \
  --issue-url https://github.com/owner/repo/issues/123

python3 scripts/autopilot_admin.py uninstall --repository-id R_kgDOExample
```

`uninstall` pauses first and refuses queued, claimed, running, retry-pending, review-ready, or human-action attempts. A failed or blocked attempt also blocks while its recorded worktree still exists. After the gate passes, the command exports Orca Automation run history, removes the Automation and Git marker, and atomically moves configuration, SQLite, logs, and runtime copies to `~/.local/state/github-issue-autopilot/archives/`. It never deletes the local repository.
