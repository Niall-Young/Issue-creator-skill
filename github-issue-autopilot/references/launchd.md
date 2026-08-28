# Run automatically with launchd

The preferred setup is `python3 scripts/autopilot_admin.py install --repo-path /absolute/repository/root`. It generates, validates, loads, and kickstarts a repository-specific user LaunchAgent. The manual template below is for unsupported executors or debugging.

Each LaunchAgent runs one detection-and-execution tick every three minutes. `launchd` does not overlap the same job, while the SQLite PID/lease gate also prevents a manual second watcher from dispatching duplicate work.

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
    <string>/absolute/path/github-issue-autopilot/scripts/issue_watcher.py</string>
    <string>once</string>
    <string>--config</string>
    <string>/absolute/path/autopilot.json</string>
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

Inspect the watcher with `status` and the configured log files. To stop it without deleting state:

```sh
launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/com.example.github-issue-autopilot.plist
```

`launchd` does not start a second instance of the same job while the previous tick is still running. SQLite claims provide the additional idempotency boundary across manual and scheduled invocations.
