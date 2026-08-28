---
name: github-issue-workflow-update
description: Update the installed GitHub Issue Workflow skill bundle from its latest stable GitHub Release and refresh installed Autopilot runtimes. Use when the user asks to update, upgrade, or check updates for issue-workflow; do not use for updating unrelated skills or a source checkout.
---

# GitHub Issue Workflow Update

Use the deterministic updater bundled with this Skill. It manages only the four official
`Niall-Young/github-issue-workflow` Skill directories in the current Agent runtime.

## Check

When the user asks whether an update exists or explicitly says to check for updates, run:

```sh
python3 scripts/update_workflow.py check
```

Checking is read-only. Report the installed and latest stable versions from the JSON result.

## Update

When the user explicitly asks to update or upgrade issue-workflow, run:

```sh
python3 scripts/update_workflow.py apply
```

Do not ask for a second conversational confirmation. A host permission prompt may still require
the user to approve filesystem or network access.

The command replaces locally modified managed Skill files with the official stable Release. It
updates only the current runtime, refreshes every installed Autopilot runtime, preserves Autopilot
configuration, SQLite state, logs, attempts, and worktrees, and rolls the complete operation back
if any managed component cannot be verified. It never updates a source checkout, another Skill,
or another Agent runtime.

After success, report the previous version, installed version, and refreshed Autopilot count. If
the current conversation does not reload Skill instructions, advise starting a new conversation.
On `blocked` or `rolled-back`, state the concise error and confirm that no partial update remains.
On the exceptional `partial-update` result, do not claim rollback succeeded; report the affected
component and preserve the command result for manual recovery.
