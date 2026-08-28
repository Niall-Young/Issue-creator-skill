#!/usr/bin/env python3
"""Install and operate one macOS Issue Autopilot loop for the current repository."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import plistlib
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any


class AdminError(RuntimeError):
    pass


def command(argv: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, cwd=cwd, check=False, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


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
        "launch_label": label,
        "plist": launch_agents_dir() / f"{label}.plist",
    }


def build_config(repo: Path, metadata: dict[str, Any], login: str, codex: str, label: str,
                 paths: dict[str, Path | str], activated_at: str) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "state_db": str(paths["database"]),
        "poll_interval_seconds": 180,
        "lease_timeout_seconds": 3000,
        "max_attempts": 2,
        "max_dispatch_per_poll": 1,
        "policy": {"scope_approval": "eligible-issue", "publication": "never", "max_risk": "medium"},
        "repositories": [{
            "repository": metadata["nameWithOwner"], "repository_id": metadata["id"],
            "repo_path": str(repo), "author": login, "activate_after": activated_at, "labels": [label],
        }],
        "executor": {"timeout_seconds": 2700, "argv": [
            codex, "exec", "--ephemeral", "--sandbox", "workspace-write", "--approve-for-me",
            "--skip-git-repo-check", "-C", "{repo_path}", "-",
        ]},
    }


def build_plist(paths: dict[str, Path | str], config_path: Path) -> bytes:
    watcher = Path(__file__).resolve().with_name("issue_watcher.py")
    value = {
        "Label": paths["launch_label"],
        "ProgramArguments": [sys.executable, str(watcher), "once", "--config", str(config_path)],
        "RunAtLoad": True,
        "StartInterval": 180,
        "StandardOutPath": str(paths["stdout"]),
        "StandardErrorPath": str(paths["stderr"]),
    }
    return plistlib.dumps(value, fmt=plistlib.FMT_XML, sort_keys=False)


def ensure_label(repository: str, label: str) -> bool:
    raw = checked(["gh", "label", "list", "--repo", repository, "--limit", "1000", "--json", "name"])
    if any(item.get("name") == label for item in json.loads(raw)):
        return False
    checked(["gh", "label", "create", label, "--repo", repository, "--color", "0E8A16",
             "--description", "Ready for local GitHub Issue Autopilot"])
    return True


def install(repo_path: Path, label: str, load: bool = True) -> dict[str, Any]:
    if sys.platform != "darwin":
        raise AdminError("automatic installation currently requires macOS launchd")
    repo = repository_root(repo_path)
    if command(["gh", "auth", "status"]).returncode:
        raise AdminError("GitHub CLI is not authenticated")
    codex = shutil.which("codex")
    if not codex:
        raise AdminError("codex executable was not found")
    metadata = repository_metadata(repo)
    login = checked(["gh", "api", "user", "--jq", ".login"])
    paths = build_paths(metadata["id"])
    activated_at = dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()
    config = build_config(repo, metadata, login, str(Path(codex).resolve()), label, paths, activated_at)
    label_created = ensure_label(metadata["nameWithOwner"], label)
    config_path, plist_path = Path(paths["config"]), Path(paths["plist"])
    try:
        atomic_write(config_path, (json.dumps(config, ensure_ascii=False, indent=2) + "\n").encode())
        atomic_write(plist_path, build_plist(paths, config_path))
        marker = {"schema_version": 1, "config": str(config_path), "label": label,
                  "repository_id": metadata["id"], "repository": metadata["nameWithOwner"]}
        atomic_write(git_common_dir(repo) / "github-issue-autopilot.json",
                     (json.dumps(marker, ensure_ascii=False, indent=2) + "\n").encode())
        checked(["plutil", "-lint", str(plist_path)])
        if load:
            domain = f"gui/{os.getuid()}"
            command(["launchctl", "bootout", domain, str(plist_path)])
            checked(["launchctl", "bootstrap", domain, str(plist_path)])
            checked(["launchctl", "kickstart", f"{domain}/{paths['launch_label']}"])
    except (AdminError, OSError) as exc:
        suffix = f"; GitHub label {label} was created and remains" if label_created else ""
        raise AdminError(f"{exc}{suffix}") from exc
    return {"status": "installed", "repository": metadata["nameWithOwner"], "label": label,
            "label_created": label_created, "config": str(config_path), "plist": str(plist_path),
            "launch_label": paths["launch_label"], "loaded": load}


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
    return json.loads(checked(argv))


def stop(repo_path: Path) -> dict[str, Any]:
    info = marker(repo_path)
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
    return {"status": "stopped", "repository": info["repository"], "state_preserved": True,
            "disabled_plist": str(disabled_path) if disabled_path.exists() else None}


def parser() -> argparse.ArgumentParser:
    main = argparse.ArgumentParser(description=__doc__)
    commands = main.add_subparsers(dest="operation", required=True)
    install_parser = commands.add_parser("install")
    install_parser.add_argument("--repo-path", type=Path, default=Path.cwd())
    install_parser.add_argument("--label", default="agent-ready")
    install_parser.add_argument("--no-load", action="store_true", help="write and validate without loading launchd")
    for name in ("doctor", "status", "stop"):
        sub = commands.add_parser(name)
        sub.add_argument("--repo-path", type=Path, default=Path.cwd())
    retry = commands.add_parser("retry")
    retry.add_argument("--repo-path", type=Path, default=Path.cwd())
    retry.add_argument("--issue-url", required=True)
    retry.add_argument("--discard-worktree", action="store_true")
    accept = commands.add_parser("accept")
    accept.add_argument("--repo-path", type=Path, default=Path.cwd())
    accept.add_argument("--issue-url", required=True)
    accept.add_argument("--target-branch", required=True)
    return main


def main() -> int:
    args = parser().parse_args()
    try:
        if args.operation == "install":
            result = install(args.repo_path, args.label, not args.no_load)
        elif args.operation == "stop":
            result = stop(args.repo_path)
        elif args.operation in {"doctor", "status"}:
            result = run_watcher(args.repo_path, args.operation)
        elif args.operation == "retry":
            extra = ["--issue-url", args.issue_url]
            if args.discard_worktree:
                extra.append("--discard-worktree")
            result = run_watcher(args.repo_path, "retry", extra)
        else:
            result = run_watcher(args.repo_path, "accept",
                                 ["--issue-url", args.issue_url, "--target-branch", args.target_branch])
    except (AdminError, OSError, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
