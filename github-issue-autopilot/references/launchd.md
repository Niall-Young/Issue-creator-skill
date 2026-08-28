# Run automatically with launchd

The preferred setup is `python3 scripts/autopilot_admin.py install --repo-path /absolute/repository/root --agent codex`. It generates, validates, loads, and kickstarts a repository-specific user LaunchAgent.

The LaunchAgent never launches hidden repair workers. Every three minutes it runs the copied administrator from the repository-specific state directory and ensures that Orca has exactly one connected `Issue Autopilot Coordinator` terminal. The coordinator polls Issues and uses Orca Orchestration to maintain up to three visible child worktrees.

Save the following as `~/Library/LaunchAgents/com.example.github-issue-autopilot.plist`, replacing every example path. Keep the configuration and SQLite state database outside managed repositories.

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.example.github-issue-autopilot</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/python3</string>
    <string>/absolute/state/path/runtime/autopilot_admin.py</string>
    <string>ensure</string>
    <string>--repo-path</string>
    <string>/absolute/path/to/repository</string>
  </array>
  <key>RunAtLoad</key>
  <true/>
  <key>StartInterval</key>
  <integer>180</integer>
  <key>StandardOutPath</key>
  <string>/absolute/path/logs/autopilot.stdout.log</string>
  <key>StandardErrorPath</key>
  <string>/absolute/path/logs/autopilot.stderr.log</string>
</dict>
</plist>
```

Validate and load it:

```sh
plutil -lint ~/Library/LaunchAgents/com.example.github-issue-autopilot.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.example.github-issue-autopilot.plist
launchctl kickstart -k gui/$(id -u)/com.example.github-issue-autopilot
```

Inspect the watcher with `status`, `orca worktree ps --json`, and `orca orchestration task-list --json`. The administrator copies its deterministic runtime scripts outside Desktop so launchd does not need direct background access to a TCC-protected checkout. To stop it without deleting state, use `autopilot_admin.py stop`; it unloads launchd and closes only the exact coordinator terminal.

Run administrator-level `doctor` to inspect the watcher and the scheduled loop together. It reads the repository-specific plist, compares it with the installed configuration, and checks `launchctl` load state. A missing, invalid, or drifted plist, or an unloaded/stopped LaunchAgent, makes `ok` false and appears under `launch_agent.errors`; the check never loads, restarts, or rewrites the service.

```sh
launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/com.example.github-issue-autopilot.plist
```

`launchd` does not start a second instance of the same ensure job while the previous tick is still running. The exact coordinator title plus SQLite Issue/Orca identities provide the additional idempotency boundary across restarts.
