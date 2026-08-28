# Run automatically with Orca Automation

The preferred setup is `python3 scripts/autopilot_admin.py install --repo-path /absolute/repository/root --agent codex`. It creates or updates one repository-specific Orca Automation and shows it in Orca's Automation GUI.

The Automation runs every three minutes against the existing repository workspace with a fresh Agent session. Its bounded precheck skips the Agent while exactly one connected `Issue Autopilot Coordinator` terminal exists, so the normal three-minute health check does not consume an Agent turn. If the coordinator is missing or its state is unknown, the Automation starts an Agent whose only job is to run the deterministic `ensure` command. The coordinator then owns Issue polling and supervised worker dispatch.

The generated Automation has these fixed safety fields:

- trigger: `*/3 * * * *`
- workspace mode: existing repository worktree
- session mode: fresh
- precheck timeout: 60 seconds
- missed-run grace: 5 minutes
- prompt: run the installed `ensure` command exactly once; do not inspect Issues or repair code

The Automation ID is stored in the repository-specific schema-v3 configuration under `~/.local/state/github-issue-autopilot/` and in the untracked Git-common-dir marker. Reinstalling reconciles that ID instead of creating duplicate Automations. Immutable Issue node IDs, SQLite claims, and Orca Task/Dispatch/worktree identities remain the work-level idempotency boundaries.

Use Orca's GUI to pause or resume the loop. A schema-v3 coordinator checks the Automation before every polling cycle and exits when it is paused or deleted. Existing workers are not forcibly stopped. After resuming, the next scheduled precheck detects the missing coordinator and restores it. `autopilot_admin.py stop` provides the same pause behavior from the CLI while preserving state.

The precheck also validates that the configured path still exists as the exact Git root and remains registered in Orca. The coordinator repeats that check while running. Losing either identity disables the Automation with `paused-missing-workspace`; it does not launch a recovery Agent or delete state. Use `list` and a repository name or immutable ID selector to inspect and stop the installation even after the checkout was removed.

Run administrator-level `doctor` to inspect the watcher and scheduler together. It reads the Automation back through Orca, verifies the installed safety fields, and rejects a missing, paused, drifted, or duplicate legacy scheduler. The check never edits or resumes the Automation.

When upgrading a schema-v2 installation, `install` creates and verifies the native Automation, stops the old LaunchAgent, and moves its plist into the repository state directory as `legacy-launchd.plist`. If migration fails, the installer removes the newly created Automation, restores the previous configuration and plist, and reloads the former LaunchAgent. SQLite state is preserved throughout.

To create or reconcile the GUI entry without enabling it, use `install --disabled`. The former `--no-load` option remains as a deprecated alias for compatibility.

For permanent removal, first accept or explicitly discard every unresolved attempt, then run `autopilot_admin.py uninstall --repository-id ID`. The command exports Automation runs, removes the Automation, and archives the repository-isolated local state. Removing an Orca project or deleting a checkout is not an uninstall operation by itself.
