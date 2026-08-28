#!/usr/bin/env python3
"""Install and operate one macOS Issue Autopilot loop for the current repository."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import plistlib
import shlex
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any


AUTOMATION_TRIGGER = "*/3 * * * *"
AUTOMATION_PRECHECK_TIMEOUT_SECONDS = 60
AUTOMATION_MISSED_RUN_GRACE_MINUTES = 5
COORDINATOR_TITLE = "Issue Autopilot Coordinator"


class AdminError(RuntimeError):
    pass


def command(argv: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    is_github = Path(argv[0]).name == "gh"
    if is_github:
        environment.setdefault("GODEBUG", "http2client=0")
    attempts = 3 if is_github else 1
    for attempt in range(attempts):
        result = subprocess.run(argv, cwd=cwd, env=environment, check=False, text=True,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if result.returncode == 0 or "EOF" not in result.stderr or attempt == attempts - 1:
            return result
    return result


def checked(argv: list[str], cwd: Path | None = None) -> str:
    result = command(argv, cwd)
    if result.returncode:
        raise AdminError(result.stderr.strip() or f"command failed: {argv[0]}")
    return result.stdout.strip()


def atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def repository_root(path: Path) -> Path:
    root = Path(checked(["git", "-C", str(path), "rev-parse", "--show-toplevel"])).resolve()
    if root != path.resolve():
        raise AdminError("repo-path must be the Git worktree root")
    return root


def git_common_dir(repo: Path) -> Path:
    common = Path(checked(["git", "-C", str(repo), "rev-parse", "--git-common-dir"]))
    return (common if common.is_absolute() else repo / common).resolve()


def repository_metadata(repo: Path) -> dict[str, Any]:
    raw = checked(["gh", "repo", "view", "--json", "id,nameWithOwner,isArchived,hasIssuesEnabled"], repo)
    value = json.loads(raw)
    if value.get("isArchived") or not value.get("hasIssuesEnabled"):
        raise AdminError("repository is archived or Issues are disabled")
    return value


def stable_key(repository_id: str) -> str:
    return hashlib.sha256(repository_id.encode("utf-8")).hexdigest()[:12]


def state_root() -> Path:
    override = os.environ.get("GITHUB_ISSUE_AUTOPILOT_STATE_ROOT")
    return Path(override).expanduser().resolve() if override else Path.home() / ".local/state/github-issue-autopilot"


def launch_agents_dir() -> Path:
    override = os.environ.get("GITHUB_ISSUE_AUTOPILOT_LAUNCH_AGENTS")
    return Path(override).expanduser().resolve() if override else Path.home() / "Library/LaunchAgents"


def build_paths(repository_id: str) -> dict[str, Path | str]:
    key = stable_key(repository_id)
    root = state_root() / key
    label = f"com.niallyoung.github-issue-autopilot.{key}"
    return {
        "key": key,
        "root": root,
        "config": root / "autopilot.json",
        "database": root / "state.sqlite3",
        "stdout": root / "launchd.stdout.log",
        "stderr": root / "launchd.stderr.log",
        "runtime_admin": root / "runtime" / "autopilot_admin.py",
        "runtime_watcher": root / "runtime" / "issue_watcher.py",
        "launch_label": label,
        "plist": launch_agents_dir() / f"{label}.plist",
        "legacy_plist_backup": root / "legacy-launchd.plist",
    }


def read_config(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AdminError(f"cannot read Autopilot configuration: {exc}") from exc
    if not isinstance(value, dict):
        raise AdminError("Autopilot configuration must be a JSON object")
    return value


def installed_configs() -> list[tuple[Path, dict[str, Any]]]:
    root = state_root()
    values: list[tuple[Path, dict[str, Any]]] = []
    if not root.is_dir():
        return values
    for path in sorted(root.glob("*/autopilot.json")):
        if path.parent.name == "archives":
            continue
        try:
            config = read_config(path)
        except AdminError:
            continue
        repositories = config.get("repositories")
        if isinstance(repositories, list) and len(repositories) == 1:
            values.append((path, config))
    return values


def config_info(config_path: Path, config: dict[str, Any]) -> dict[str, Any]:
    repository = config["repositories"][0]
    return {
        "schema_version": config.get("schema_version"),
        "config": str(config_path),
        "repository": repository.get("repository"),
        "repository_id": repository.get("repository_id"),
        "repo_path": repository.get("repo_path"),
        "scheduler": config.get("scheduler", {}),
    }


def resolve_info(repo_path: Path | None = None, repository: str | None = None,
                 repository_id: str | None = None) -> dict[str, Any]:
    selected = sum(value is not None for value in (repo_path, repository, repository_id))
    if selected > 1:
        raise AdminError("select the installation by only one of repo-path, repository, or repository-id")
    if selected == 0:
        repo_path = Path.cwd()
    if repo_path is not None:
        return marker(repo_path)
    matches = []
    for config_path, config in installed_configs():
        info = config_info(config_path, config)
        if repository is not None and info["repository"] == repository:
            matches.append(info)
        if repository_id is not None and info["repository_id"] == repository_id:
            matches.append(info)
    if not matches:
        raise AdminError("no matching Autopilot installation was found")
    if len(matches) != 1:
        raise AdminError("multiple Autopilot installations matched; use repository-id")
    return matches[0]


def exact_git_root(path: Path) -> bool:
    if not path.is_dir():
        return False
    result = command(["git", "-C", str(path), "rev-parse", "--show-toplevel"])
    return result.returncode == 0 and Path(result.stdout.strip()).resolve() == path.resolve()


def ledger_summary(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {"issues": 0, "unresolved": [], "artifact_blockers": []}
    try:
        uri = f"file:{path}?mode=ro"
        connection = sqlite3.connect(uri, uri=True)
        connection.row_factory = sqlite3.Row
        issues = [dict(row) for row in connection.execute(
            "SELECT node_id, issue_number, url, status, attempts FROM issues ORDER BY issue_number"
        )]
        unresolved_statuses = {
            "queued", "claimed", "running", "retry-pending", "ready-for-review", "needs-human"
        }
        unresolved = [item for item in issues if item["status"] in unresolved_statuses]
        artifact_blockers: list[dict[str, Any]] = []
        for issue in issues:
            if issue["status"] not in {"blocked", "failed"}:
                continue
            row = connection.execute(
                "SELECT worktree_path FROM attempts WHERE node_id=? AND attempt_number=?",
                (issue["node_id"], issue["attempts"]),
            ).fetchone()
            if row and row["worktree_path"] and Path(row["worktree_path"]).exists():
                artifact_blockers.append(issue)
        return {"issues": len(issues), "unresolved": unresolved,
                "artifact_blockers": artifact_blockers}
    except sqlite3.Error as exc:
        raise AdminError(f"cannot inspect Autopilot ledger: {exc}") from exc
    finally:
        if "connection" in locals():
            connection.close()


def build_config(repo: Path, metadata: dict[str, Any], login: str, orca: str, label: str,
                 paths: dict[str, Path | str], default_agent: str,
                 allowed_agents: list[str]) -> dict[str, Any]:
    return {
        "schema_version": 3,
        "state_db": str(paths["database"]),
        "poll_interval_seconds": 180,
        "lease_timeout_seconds": 3000,
        "max_attempts": 2,
        "max_concurrent_workers": 3,
        "policy": {"scope_approval": "eligible-issue", "publication": "never", "max_risk": "medium"},
        "repositories": [{
            "repository": metadata["nameWithOwner"], "repository_id": metadata["id"],
            "repo_path": str(repo), "author": login, "labels": [label],
        }],
        "orca": {"cli": orca, "default_agent": default_agent,
                 "allowed_agents": allowed_agents, "setup": "run"},
        "scheduler": {
            "backend": "orca-automation",
            "automation_id": None,
            "name": f"Issue Autopilot · {metadata['nameWithOwner']} · {paths['key']}",
            "trigger": AUTOMATION_TRIGGER,
            "provider": default_agent,
            "workspace_mode": "existing",
            "session_mode": "fresh",
            "precheck_timeout_seconds": AUTOMATION_PRECHECK_TIMEOUT_SECONDS,
            "missed_run_grace_minutes": AUTOMATION_MISSED_RUN_GRACE_MINUTES,
        },
    }


def build_plist(paths: dict[str, Path | str], config_path: Path) -> bytes:
    value = {
        "Label": paths["launch_label"],
        "ProgramArguments": [sys.executable, str(paths["runtime_admin"]), "ensure",
                             "--config", str(config_path)],
        "RunAtLoad": True,
        "StartInterval": 180,
        "StandardOutPath": str(paths["stdout"]),
        "StandardErrorPath": str(paths["stderr"]),
    }
    return plistlib.dumps(value, fmt=plistlib.FMT_XML, sort_keys=False)


def orca_json(orca: str, *args: str) -> dict[str, Any]:
    raw = checked([orca, *args, "--json"])
    value = json.loads(raw)
    if value.get("ok") is False:
        raise AdminError((value.get("error") or {}).get("message") or "Orca command failed")
    return value


def scheduler_command(config_path: Path, operation: str) -> str:
    runtime_admin = config_path.parent / "runtime" / "autopilot_admin.py"
    return shlex.join([sys.executable, str(runtime_admin), operation, "--config", str(config_path)])


def expected_automation(config: dict[str, Any], config_path: Path,
                        enabled: bool) -> dict[str, Any]:
    scheduler = config["scheduler"]
    repository = config["repositories"][0]
    ensure = scheduler_command(config_path, "ensure")
    return {
        "name": scheduler["name"],
        "trigger": scheduler["trigger"],
        "prompt": (
            "Run the following command exactly once and return its JSON result. "
            "Do not inspect Issues, modify repository files, or perform repair work in this session.\n\n"
            f"{ensure}"
        ),
        "provider": scheduler["provider"],
        "precheck": scheduler_command(config_path, "automation-precheck"),
        "precheck_timeout_seconds": scheduler["precheck_timeout_seconds"],
        "workspace": f"path:{repository['repo_path']}",
        "workspace_mode": scheduler["workspace_mode"],
        "session_mode": scheduler["session_mode"],
        "missed_run_grace_minutes": scheduler["missed_run_grace_minutes"],
        "enabled": enabled,
    }


def automation_arguments(config: dict[str, Any], config_path: Path,
                         enabled: bool) -> list[str]:
    expected = expected_automation(config, config_path, enabled)
    return automation_spec_arguments(expected)


def automation_spec_arguments(expected: dict[str, Any]) -> list[str]:
    return [
        "--name", expected["name"],
        "--trigger", expected["trigger"],
        "--prompt", expected["prompt"],
        "--provider", expected["provider"],
        "--precheck", expected["precheck"],
        "--precheck-timeout", str(expected["precheck_timeout_seconds"]),
        "--workspace", expected["workspace"],
        "--workspace-mode", expected["workspace_mode"],
        "--fresh-session",
        "--missed-run-grace-minutes", str(expected["missed_run_grace_minutes"]),
        "--enabled" if expected["enabled"] else "--disabled",
    ]


def automation_payload(value: dict[str, Any]) -> dict[str, Any]:
    result = value.get("result", {})
    if not isinstance(result, dict):
        return {}
    automation = result.get("automation", result)
    return automation if isinstance(automation, dict) else {}


def automation_id(value: dict[str, Any]) -> str | None:
    automation = automation_payload(value)
    identifier = automation.get("id") or automation.get("automationId")
    return identifier if isinstance(identifier, str) and identifier else None


def first_value(value: dict[str, Any], *names: str) -> Any:
    for name in names:
        if name in value:
            return value[name]
    return None


def normalized_automation(value: dict[str, Any]) -> dict[str, Any]:
    automation = automation_payload(value)
    schedule = first_value(automation, "trigger", "schedule", "rrule")
    if isinstance(schedule, dict):
        schedule = first_value(schedule, "expression", "cron", "value")
    workspace = first_value(automation, "workspace", "workspaceSelector")
    if isinstance(workspace, dict):
        workspace = first_value(workspace, "selector", "id", "path")
        if isinstance(workspace, str) and workspace.startswith("/"):
            workspace = f"path:{workspace}"
    if workspace is None:
        run_context = automation.get("runContext")
        if isinstance(run_context, dict) and isinstance(run_context.get("path"), str):
            workspace = f"path:{run_context['path']}"
    session_mode = first_value(automation, "session_mode", "sessionMode")
    if session_mode is None:
        reused = first_value(automation, "reuseSession", "reuse_session")
        if isinstance(reused, bool):
            session_mode = "reuse" if reused else "fresh"
    return {
        "name": first_value(automation, "name"),
        "trigger": schedule,
        "prompt": first_value(automation, "prompt"),
        "provider": first_value(automation, "provider", "providerId", "agent", "agentId"),
        "precheck": (
            automation.get("precheck", {}).get("command")
            if isinstance(automation.get("precheck"), dict)
            else first_value(automation, "precheck", "precheckCommand")
        ),
        "precheck_timeout_seconds": (
            automation.get("precheck", {}).get("timeoutSeconds")
            if isinstance(automation.get("precheck"), dict)
            else first_value(automation, "precheck_timeout_seconds", "precheckTimeoutSeconds",
                             "precheckTimeout")
        ),
        "workspace": workspace,
        "workspace_mode": first_value(automation, "workspace_mode", "workspaceMode"),
        "session_mode": session_mode,
        "missed_run_grace_minutes": first_value(
            automation, "missed_run_grace_minutes", "missedRunGraceMinutes"
        ),
        "enabled": bool(first_value(automation, "enabled", "isEnabled")),
    }


def scheduler_health(config_path: Path) -> dict[str, Any]:
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
        scheduler = config.get("scheduler", {})
        identifier = scheduler.get("automation_id")
        if scheduler.get("backend") != "orca-automation" or not identifier:
            raise AdminError("Orca Automation scheduler is not configured")
        response = orca_json(config["orca"]["cli"], "automations", "show", identifier)
        actual = normalized_automation(response)
        expected = expected_automation(config, config_path, enabled=True)
        configuration_keys = set(expected) - {"enabled"}
        matches = all(actual.get(key) == expected[key] for key in configuration_keys)
        errors: list[str] = []
        if not actual["enabled"]:
            errors.append("Orca Automation is paused")
        if not matches:
            errors.append("Orca Automation does not match the installed configuration")
        return {"ok": bool(actual["enabled"] and matches), "backend": "orca-automation",
                "automation_id": identifier, "exists": True, "enabled": actual["enabled"],
                "configuration_matches": matches, "errors": errors}
    except (AdminError, OSError, KeyError, json.JSONDecodeError) as exc:
        return {"ok": False, "backend": "orca-automation", "automation_id": None,
                "exists": False, "enabled": False, "configuration_matches": False,
                "errors": [str(exc)]}


def workspace_error(config: dict[str, Any]) -> str | None:
    repository = config["repositories"][0]
    path = Path(repository["repo_path"])
    if not path.is_dir():
        return "repository path is missing"
    if not exact_git_root(path):
        return "repository path is not the configured Git root"
    status = orca_json(config["orca"]["cli"], "status")
    if status.get("result", {}).get("runtime", {}).get("state") != "ready":
        raise AdminError("Orca runtime is not ready")
    try:
        orca_json(config["orca"]["cli"], "repo", "show", "--repo", f"path:{path}")
    except AdminError:
        return "repository is no longer registered in Orca"
    return None


def pause_missing_workspace(config: dict[str, Any], reason: str) -> dict[str, Any]:
    scheduler = config.get("scheduler", {})
    identifier = scheduler.get("automation_id")
    if not identifier:
        raise AdminError("Orca Automation ID is missing")
    orca_json(config["orca"]["cli"], "automations", "edit", identifier, "--disabled")
    try:
        close_coordinators(config)
    except AdminError:
        pass
    return {"status": "paused-missing-workspace", "run_automation": False,
            "automation_id": identifier, "reason": reason, "state_preserved": True}


def automation_precheck(config_path: Path) -> dict[str, Any]:
    try:
        config = read_config(config_path)
        orca = config["orca"]["cli"]
        repository = config["repositories"][0]
        missing = workspace_error(config)
        if missing:
            try:
                return pause_missing_workspace(config, missing)
            except AdminError as exc:
                return {"status": "pause-failed-missing-workspace", "run_automation": False,
                        "reason": missing, "error": str(exc), "state_preserved": True}
        terminals = orca_json(orca, "terminal", "list", "--worktree",
                              f"path:{repository['repo_path']}")
        connected = [item for item in terminals.get("result", {}).get("terminals", [])
                     if item.get("title") == COORDINATOR_TITLE and item.get("connected")]
        if len(connected) == 1:
            return {"status": "healthy", "run_automation": False,
                    "terminal": connected[0].get("handle")}
        return {"status": "needs-recovery", "run_automation": True,
                "connected_coordinators": len(connected)}
    except (AdminError, OSError, KeyError, json.JSONDecodeError) as exc:
        return {"status": "needs-recovery", "run_automation": True, "error": str(exc)}


def ensure_coordinator(config_path: Path) -> dict[str, Any]:
    config = read_config(config_path)
    missing = workspace_error(config)
    if missing:
        return pause_missing_workspace(config, missing)
    orca = config["orca"]["cli"]
    status = orca_json(orca, "status")
    if status.get("result", {}).get("runtime", {}).get("state") != "ready":
        raise AdminError("Orca runtime is not ready")
    repository = config["repositories"][0]
    worktree = f"path:{repository['repo_path']}"
    terminals = orca_json(orca, "terminal", "list", "--worktree", worktree)
    existing = [item for item in terminals.get("result", {}).get("terminals", [])
                if item.get("title") == COORDINATOR_TITLE and item.get("connected")]
    if len(existing) == 1:
        return {"status": "running", "terminal": existing[0].get("handle")}
    for item in existing:
        if item.get("handle"):
            orca_json(orca, "terminal", "close", "--terminal", item["handle"])
    paths = build_paths(repository["repository_id"])
    argv = [sys.executable, str(paths["runtime_watcher"]), "run", "--config", str(config_path)]
    created = orca_json(orca, "terminal", "create", "--worktree", worktree,
                        "--title", COORDINATOR_TITLE, "--command", f"exec {shlex.join(argv)}")
    terminal = created.get("result", {}).get("terminal", created.get("result", {}))
    return {"status": "started", "terminal": terminal.get("handle")}


def ensure_label(repository: str, label: str) -> bool:
    raw = checked(["gh", "label", "list", "--repo", repository, "--limit", "1000", "--json", "name"])
    if any(item.get("name") == label for item in json.loads(raw)):
        return False
    checked(["gh", "label", "create", label, "--repo", repository, "--color", "0E8A16",
             "--description", "Ready for local GitHub Issue Autopilot"])
    return True


def list_automations(orca: str) -> list[dict[str, Any]]:
    response = orca_json(orca, "automations", "list")
    result = response.get("result", {})
    items = result.get("automations", []) if isinstance(result, dict) else []
    return [item for item in items if isinstance(item, dict)]


def configure_automation(config: dict[str, Any], config_path: Path,
                         enabled: bool) -> tuple[str, bool, dict[str, Any] | None]:
    orca = config["orca"]["cli"]
    scheduler = config["scheduler"]
    identifier = scheduler.get("automation_id")
    created = False
    previous: dict[str, Any] | None = None
    if identifier:
        try:
            previous = normalized_automation(
                orca_json(orca, "automations", "show", identifier)
            )
        except AdminError:
            identifier = None
    if not identifier:
        matches = [item for item in list_automations(orca)
                   if item.get("name") == scheduler["name"]]
        if len(matches) > 1:
            raise AdminError("multiple Orca Automations use the repository scheduler name")
        if matches:
            identifier = matches[0].get("id") or matches[0].get("automationId")
            if identifier:
                previous = normalized_automation(
                    orca_json(orca, "automations", "show", identifier)
                )
        else:
            response = orca_json(orca, "automations", "create",
                                 *automation_arguments(config, config_path, enabled=False))
            identifier = automation_id(response)
            created = True
            if not identifier:
                matches = [item for item in list_automations(orca)
                           if item.get("name") == scheduler["name"]]
                if len(matches) == 1:
                    identifier = matches[0].get("id") or matches[0].get("automationId")
    if not isinstance(identifier, str) or not identifier:
        raise AdminError("Orca did not return a unique Automation ID")
    scheduler["automation_id"] = identifier
    atomic_write(config_path, (json.dumps(config, ensure_ascii=False, indent=2) + "\n").encode())
    orca_json(orca, "automations", "edit", identifier,
              *automation_arguments(config, config_path, enabled=enabled))
    return identifier, created, previous


def close_coordinators(config: dict[str, Any]) -> int:
    orca = config["orca"]["cli"]
    repository = config["repositories"][0]
    try:
        terminals = orca_json(orca, "terminal", "list", "--worktree",
                              f"path:{repository['repo_path']}")
    except AdminError:
        return 0
    closed = 0
    for item in terminals.get("result", {}).get("terminals", []):
        if item.get("title") == COORDINATOR_TITLE and item.get("handle"):
            orca_json(orca, "terminal", "close", "--terminal", item["handle"])
            closed += 1
    return closed


def disable_legacy_launch_agent(paths: dict[str, Path | str]) -> dict[str, Any]:
    plist_path = Path(paths["plist"])
    backup_path = Path(paths["legacy_plist_backup"])
    domain = f"gui/{os.getuid()}"
    loaded = command(["launchctl", "print", f"{domain}/{paths['launch_label']}"]).returncode == 0
    if loaded:
        stopped = command(["launchctl", "bootout", domain, str(plist_path)])
        if stopped.returncode and command(
            ["launchctl", "print", f"{domain}/{paths['launch_label']}"]
        ).returncode == 0:
            raise AdminError(stopped.stderr.strip() or "failed to stop legacy LaunchAgent")
    if plist_path.exists():
        backup_path.parent.mkdir(parents=True, exist_ok=True)
        if backup_path.exists():
            backup_path.unlink()
        os.replace(plist_path, backup_path)
    return {"was_loaded": loaded, "backup": str(backup_path) if backup_path.exists() else None}


def file_snapshot(paths: list[Path]) -> dict[Path, bytes | None]:
    return {path: path.read_bytes() if path.exists() else None for path in paths}


def restore_files(snapshot: dict[Path, bytes | None]) -> None:
    for path, data in snapshot.items():
        if data is None:
            if path.exists():
                path.unlink()
        else:
            atomic_write(path, data)


def restore_legacy_launch_agent(paths: dict[str, Path | str], was_loaded: bool) -> None:
    plist_path = Path(paths["plist"])
    backup_path = Path(paths["legacy_plist_backup"])
    if not plist_path.exists() and backup_path.exists():
        plist_path.parent.mkdir(parents=True, exist_ok=True)
        os.replace(backup_path, plist_path)
    if was_loaded and plist_path.exists():
        domain = f"gui/{os.getuid()}"
        command(["launchctl", "bootout", domain, str(plist_path)])
        checked(["launchctl", "bootstrap", domain, str(plist_path)])
        checked(["launchctl", "kickstart", f"{domain}/{paths['launch_label']}"])


def install(repo_path: Path, label: str, default_agent: str, allowed_agents: list[str],
            load: bool = True) -> dict[str, Any]:
    repo = repository_root(repo_path)
    login_result = command(["gh", "api", "user", "--jq", ".login"])
    if login_result.returncode:
        raise AdminError("GitHub CLI is not authenticated")
    orca = shutil.which("orca")
    if not orca:
        raise AdminError("Orca CLI was not found")
    allowed_agents = list(dict.fromkeys([default_agent, *allowed_agents]))
    metadata = repository_metadata(repo)
    login = login_result.stdout.strip()
    paths = build_paths(metadata["id"])
    config = build_config(repo, metadata, login, str(Path(orca).resolve()), label, paths,
                          default_agent, allowed_agents)
    label_created = ensure_label(metadata["nameWithOwner"], label)
    config_path = Path(paths["config"])
    marker_path = git_common_dir(repo) / "github-issue-autopilot.json"
    if config_path.exists():
        try:
            previous_config = json.loads(config_path.read_text(encoding="utf-8"))
            previous_scheduler = previous_config.get("scheduler", {})
            if previous_scheduler.get("backend") == "orca-automation":
                config["scheduler"]["automation_id"] = previous_scheduler.get("automation_id")
        except (OSError, json.JSONDecodeError):
            pass
    managed_files = [config_path, Path(paths["runtime_admin"]), Path(paths["runtime_watcher"]),
                     marker_path, Path(paths["plist"]), Path(paths["legacy_plist_backup"])]
    snapshot = file_snapshot(managed_files)
    legacy_was_loaded = command(
        ["launchctl", "print", f"gui/{os.getuid()}/{paths['launch_label']}"]
    ).returncode == 0 if sys.platform == "darwin" else False
    created_automation: str | None = None
    previous_automation: dict[str, Any] | None = None
    identifier: str | None = None
    try:
        atomic_write(config_path, (json.dumps(config, ensure_ascii=False, indent=2) + "\n").encode())
        atomic_write(Path(paths["runtime_admin"]), Path(__file__).read_bytes())
        atomic_write(Path(paths["runtime_watcher"]), Path(__file__).resolve().with_name("issue_watcher.py").read_bytes())
        identifier, created, previous_automation = configure_automation(
            config, config_path, enabled=load
        )
        if created:
            created_automation = identifier
        marker_value = {"schema_version": 3, "config": str(config_path), "label": label,
                        "repository_id": metadata["id"], "repository": metadata["nameWithOwner"],
                        "scheduler": {"backend": "orca-automation", "automation_id": identifier}}
        atomic_write(marker_path,
                     (json.dumps(marker_value, ensure_ascii=False, indent=2) + "\n").encode())
        legacy = {"was_loaded": False, "backup": None}
        if sys.platform == "darwin":
            legacy = disable_legacy_launch_agent(paths)
        if load:
            close_coordinators(config)
            coordinator = ensure_coordinator(config_path)
        else:
            close_coordinators(config)
            coordinator = {"status": "disabled", "terminal": None}
        health = scheduler_health(config_path)
        if load and not health["ok"]:
            raise AdminError("Orca Automation failed post-install verification: "
                             + "; ".join(health["errors"]))
    except (AdminError, OSError) as exc:
        try:
            close_coordinators(config)
        except (AdminError, OSError, KeyError):
            pass
        if created_automation:
            try:
                orca_json(str(Path(orca).resolve()), "automations", "remove", created_automation)
            except (AdminError, OSError):
                pass
        elif identifier and previous_automation:
            try:
                orca_json(str(Path(orca).resolve()), "automations", "edit", identifier,
                          *automation_spec_arguments(previous_automation))
            except (AdminError, OSError):
                pass
        restore_files(snapshot)
        if sys.platform == "darwin":
            try:
                restore_legacy_launch_agent(paths, legacy_was_loaded)
            except (AdminError, OSError):
                pass
        suffix = f"; GitHub label {label} was created and remains" if label_created else ""
        raise AdminError(f"{exc}{suffix}") from exc
    return {"status": "installed", "repository": metadata["nameWithOwner"], "label": label,
            "label_created": label_created, "config": str(config_path),
            "scheduler": "orca-automation", "automation_id": identifier,
            "automation_enabled": load, "coordinator": coordinator,
            "legacy_launch_agent": legacy, "default_agent": default_agent,
            "allowed_agents": allowed_agents}


def marker(repo_path: Path) -> dict[str, Any]:
    repo = repository_root(repo_path)
    path = git_common_dir(repo) / "github-issue-autopilot.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise AdminError("Autopilot is not configured for this repository") from exc


def run_watcher(repo_path: Path, operation: str, extra: list[str] | None = None) -> dict[str, Any]:
    info = marker(repo_path)
    watcher = Path(__file__).resolve().with_name("issue_watcher.py")
    argv = [sys.executable, str(watcher), operation, "--config", info["config"], *(extra or [])]
    result = command(argv)
    try:
        watcher_result = json.loads(result.stdout or result.stderr)
    except json.JSONDecodeError as exc:
        raise AdminError(result.stderr.strip() or result.stdout.strip() or "watcher returned invalid JSON") from exc
    if result.returncode and operation != "doctor":
        raise AdminError((watcher_result.get("error") if isinstance(watcher_result, dict) else None)
                         or result.stderr.strip() or "watcher command failed")
    if not isinstance(watcher_result, dict) or watcher_result.get("status") == "error":
        raise AdminError(watcher_result.get("error", "watcher command failed")
                         if isinstance(watcher_result, dict)
                         else "watcher returned invalid JSON")
    return watcher_result


def launchctl_arguments(output: str) -> list[str]:
    arguments: list[str] = []
    reading = False
    for line in output.splitlines():
        stripped = line.strip()
        if stripped == "arguments = {":
            reading = True
        elif reading and stripped == "}":
            break
        elif reading and stripped:
            arguments.append(stripped)
    return arguments


def loaded_launch_agent_matches(output: str, expected: dict[str, Any]) -> bool:
    values: dict[str, str] = {}
    for line in output.splitlines():
        stripped = line.strip()
        if " = " in stripped:
            key, value = stripped.split(" = ", 1)
            values[key] = value
    properties = {item.strip() for item in values.get("properties", "").split("|")}
    return (
        launchctl_arguments(output) == expected["ProgramArguments"]
        and values.get("run interval") == f"{expected['StartInterval']} seconds"
        and values.get("stdout path") == expected["StandardOutPath"]
        and values.get("stderr path") == expected["StandardErrorPath"]
        and ("runatload" in properties) == bool(expected["RunAtLoad"])
    )


def launch_agent_health(info: dict[str, Any]) -> dict[str, Any]:
    paths = build_paths(info["repository_id"])
    plist_path = Path(paths["plist"])
    config_path = Path(info["config"])
    health: dict[str, Any] = {
        "ok": False,
        "label": paths["launch_label"],
        "plist": str(plist_path),
        "plist_exists": plist_path.is_file(),
        "configuration_matches": False,
        "loaded": False,
        "loaded_configuration_matches": False,
        "errors": [],
    }
    expected: dict[str, Any] | None = None
    if health["plist_exists"]:
        try:
            actual = plistlib.loads(plist_path.read_bytes())
            expected = plistlib.loads(build_plist(paths, config_path))
            health["configuration_matches"] = actual == expected
        except (OSError, plistlib.InvalidFileException):
            health["errors"].append("LaunchAgent plist is invalid")
        if not health["configuration_matches"] and not health["errors"]:
            health["errors"].append("LaunchAgent plist does not match the installed configuration")
    else:
        health["errors"].append("LaunchAgent plist is missing")
    domain = f"gui/{os.getuid()}"
    loaded = command(["launchctl", "print", f"{domain}/{paths['launch_label']}"])
    health["loaded"] = loaded.returncode == 0
    if not health["loaded"]:
        health["errors"].append("LaunchAgent is not loaded")
    elif expected is not None:
        health["loaded_configuration_matches"] = loaded_launch_agent_matches(loaded.stdout, expected)
        if not health["loaded_configuration_matches"]:
            health["errors"].append("Loaded LaunchAgent does not match the installed configuration")
    health["ok"] = bool(health["plist_exists"] and health["configuration_matches"]
                        and health["loaded"] and health["loaded_configuration_matches"])
    return health


def doctor(repo_path: Path) -> dict[str, Any]:
    info = marker(repo_path)
    result = dict(run_watcher(repo_path, "doctor"))
    config_path = Path(info["config"])
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        config = {}
    if config.get("schema_version") == 3:
        result["scheduler"] = scheduler_health(config_path)
        paths = build_paths(info["repository_id"])
        legacy_loaded = command([
            "launchctl", "print", f"gui/{os.getuid()}/{paths['launch_label']}"
        ]).returncode == 0 if sys.platform == "darwin" else False
        result["legacy_launch_agent"] = {
            "loaded": legacy_loaded,
            "backup": str(paths["legacy_plist_backup"])
            if Path(paths["legacy_plist_backup"]).exists() else None,
        }
        if legacy_loaded:
            result["scheduler"]["errors"].append("legacy LaunchAgent is still loaded")
            result["scheduler"]["ok"] = False
        result["ok"] = bool(result.get("ok") and result["scheduler"]["ok"])
    else:
        result["launch_agent"] = launch_agent_health(info)
        result["ok"] = bool(result.get("ok") and result["launch_agent"]["ok"])
    return result


def list_installations() -> dict[str, Any]:
    installations: list[dict[str, Any]] = []
    for config_path, config in installed_configs():
        info = config_info(config_path, config)
        path = Path(str(info["repo_path"]))
        ledger = ledger_summary(Path(config.get("state_db", config_path.parent / "state.sqlite3")))
        scheduler = scheduler_health(config_path)
        installations.append({
            "repository": info["repository"],
            "repository_id": info["repository_id"],
            "repo_path": str(path),
            "git_root": exact_git_root(path),
            "automation_id": info["scheduler"].get("automation_id"),
            "automation_exists": scheduler["exists"],
            "automation_enabled": scheduler["enabled"],
            "issues": ledger["issues"],
            "unresolved": len(ledger["unresolved"]),
            "artifact_blockers": len(ledger["artifact_blockers"]),
        })
    return {"status": "ok", "installations": installations}


def stop(repo_path: Path | None = None, repository: str | None = None,
         repository_id: str | None = None) -> dict[str, Any]:
    info = resolve_info(repo_path, repository, repository_id)
    config_path = Path(info["config"])
    config = read_config(config_path)
    if config.get("schema_version") == 3:
        scheduler = config.get("scheduler", {})
        identifier = scheduler.get("automation_id")
        if not identifier:
            raise AdminError("Orca Automation ID is missing")
        orca_json(config["orca"]["cli"], "automations", "edit", identifier, "--disabled")
        close_coordinators(config)
        return {"status": "stopped", "repository": info["repository"],
                "state_preserved": True, "automation_id": identifier,
                "automation_enabled": False}
    paths = build_paths(info["repository_id"])
    domain = f"gui/{os.getuid()}"
    plist_path = Path(paths["plist"])
    disabled_path = Path(paths["root"]) / "disabled.plist"
    if plist_path.exists():
        bootout = command(["launchctl", "bootout", domain, str(plist_path)])
        if bootout.returncode and command(
            ["launchctl", "print", f"{domain}/{paths['launch_label']}"]
        ).returncode == 0:
            raise AdminError(bootout.stderr.strip() or "failed to stop LaunchAgent")
        disabled_path.parent.mkdir(parents=True, exist_ok=True)
        os.replace(plist_path, disabled_path)
    try:
        config = json.loads(Path(info["config"]).read_text(encoding="utf-8"))
        orca = config.get("orca", {}).get("cli")
        if orca:
            terminals = orca_json(orca, "terminal", "list", "--worktree",
                                  f"path:{config['repositories'][0]['repo_path']}")
            for item in terminals.get("result", {}).get("terminals", []):
                if item.get("title") == COORDINATOR_TITLE and item.get("handle"):
                    orca_json(orca, "terminal", "close", "--terminal", item["handle"])
    except (AdminError, OSError, json.JSONDecodeError):
        pass
    return {"status": "stopped", "repository": info["repository"], "state_preserved": True,
            "disabled_plist": str(disabled_path) if disabled_path.exists() else None}


def uninstall(repo_path: Path | None = None, repository: str | None = None,
              repository_id: str | None = None) -> dict[str, Any]:
    info = resolve_info(repo_path, repository, repository_id)
    config_path = Path(info["config"])
    config = read_config(config_path)
    configured = config["repositories"][0]
    configured_id = configured.get("repository_id")
    if not isinstance(configured_id, str) or not configured_id:
        raise AdminError("repository ID is required for safe uninstall")
    paths = build_paths(configured_id)
    active_root = Path(paths["root"])
    if config_path.resolve() != Path(paths["config"]).resolve() or active_root.parent != state_root():
        raise AdminError("configuration is outside the repository-isolated state directory")

    stop(repository=configured["repository"])
    ledger = ledger_summary(Path(config["state_db"]))
    blockers = [*ledger["unresolved"], *ledger["artifact_blockers"]]
    if blockers:
        labels = ", ".join(f"#{item['issue_number']} ({item['status']})" for item in blockers)
        raise AdminError(f"uninstall blocked by unresolved attempts: {labels}")

    scheduler = config.get("scheduler", {})
    identifier = scheduler.get("automation_id")
    if not isinstance(identifier, str) or not identifier:
        raise AdminError("Orca Automation ID is missing")
    archive_parent = state_root() / "archives"
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    archive_path = archive_parent / f"{stamp}-{paths['key']}"
    if archive_path.exists():
        raise AdminError("archive destination already exists")
    runs = orca_json(config["orca"]["cli"], "automations", "runs", "--id", identifier)
    atomic_write(active_root / "automation-runs.json",
                 (json.dumps(runs, ensure_ascii=False, indent=2) + "\n").encode())
    archived_at = dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()
    manifest = {"schema_version": 1, "archived_at": archived_at,
                "repository": configured["repository"], "repository_id": configured_id,
                "repo_path": configured["repo_path"], "automation_id": identifier,
                "ledger": ledger}
    atomic_write(active_root / "archive-manifest.json",
                 (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode())
    orca_json(config["orca"]["cli"], "automations", "remove", identifier)

    marker_removed = False
    repo = Path(configured["repo_path"])
    if exact_git_root(repo):
        marker_path = git_common_dir(repo) / "github-issue-autopilot.json"
        try:
            marker_path.unlink(missing_ok=True)
            marker_removed = True
        except OSError:
            marker_removed = False
    archive_parent.mkdir(parents=True, exist_ok=True)
    os.replace(active_root, archive_path)
    return {"status": "uninstalled", "repository": configured["repository"],
            "automation_removed": True, "state_archived": True,
            "archive": str(archive_path), "marker_removed": marker_removed,
            "local_repository_removed": False}


def selector_arguments(parser: argparse.ArgumentParser) -> None:
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--repo-path", type=Path)
    group.add_argument("--repository")
    group.add_argument("--repository-id")


def parser() -> argparse.ArgumentParser:
    main = argparse.ArgumentParser(description=__doc__)
    commands = main.add_subparsers(dest="operation", required=True)
    install_parser = commands.add_parser("install")
    install_parser.add_argument("--repo-path", type=Path, default=Path.cwd())
    install_parser.add_argument("--label", default="agent-ready")
    install_parser.add_argument("--agent", default="codex", help="repository default Orca agent ID")
    install_parser.add_argument("--allow-agent", action="append", default=[],
                                help="additional agent:<id> label override allowed for this repository")
    install_parser.add_argument("--disabled", action="store_true",
                                help="create or update the Orca Automation without enabling it")
    install_parser.add_argument("--no-load", action="store_true",
                                help="deprecated alias for --disabled")
    for name in ("doctor", "status"):
        sub = commands.add_parser(name)
        sub.add_argument("--repo-path", type=Path, default=Path.cwd())
    commands.add_parser("list")
    stop_parser = commands.add_parser("stop")
    selector_arguments(stop_parser)
    uninstall_parser = commands.add_parser("uninstall")
    selector_arguments(uninstall_parser)
    ensure = commands.add_parser("ensure")
    ensure.add_argument("--config", type=Path, required=True)
    precheck = commands.add_parser("automation-precheck")
    precheck.add_argument("--config", type=Path, required=True)
    retry = commands.add_parser("retry")
    retry.add_argument("--repo-path", type=Path, default=Path.cwd())
    retry.add_argument("--issue-url", required=True)
    retry.add_argument("--discard-worktree", action="store_true")
    discard = commands.add_parser("discard")
    discard.add_argument("--repo-path", type=Path, default=Path.cwd())
    discard.add_argument("--issue-url", required=True)
    accept = commands.add_parser("accept")
    accept.add_argument("--repo-path", type=Path, default=Path.cwd())
    accept.add_argument("--issue-url", required=True)
    accept.add_argument("--target-branch", required=True)
    return main


def main() -> int:
    args = parser().parse_args()
    try:
        if args.operation == "install":
            result = install(args.repo_path, args.label, args.agent, args.allow_agent,
                             not (args.disabled or args.no_load))
        elif args.operation == "ensure":
            result = ensure_coordinator(args.config)
        elif args.operation == "automation-precheck":
            result = automation_precheck(args.config)
        elif args.operation == "list":
            result = list_installations()
        elif args.operation == "stop":
            result = stop(args.repo_path, args.repository, args.repository_id)
        elif args.operation == "uninstall":
            result = uninstall(args.repo_path, args.repository, args.repository_id)
        elif args.operation == "doctor":
            result = doctor(args.repo_path)
        elif args.operation == "status":
            result = run_watcher(args.repo_path, args.operation)
        elif args.operation == "retry":
            extra = ["--issue-url", args.issue_url]
            if args.discard_worktree:
                extra.append("--discard-worktree")
            result = run_watcher(args.repo_path, "retry", extra)
        elif args.operation == "discard":
            result = run_watcher(args.repo_path, "discard", ["--issue-url", args.issue_url])
        else:
            result = run_watcher(args.repo_path, "accept",
                                 ["--issue-url", args.issue_url, "--target-branch", args.target_branch])
    except (AdminError, OSError, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    if args.operation == "automation-precheck":
        return 0 if result.get("run_automation") else 1
    return 2 if args.operation == "doctor" and not result.get("ok") else 0


if __name__ == "__main__":
    raise SystemExit(main())
