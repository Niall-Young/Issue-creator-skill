#!/usr/bin/env python3
"""Poll eligible GitHub Issues and dispatch fresh repair-agent processes."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import signal
import shutil
import socket
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


SCHEMA_VERSION = 1
REPOSITORY = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
RECEIPT = re.compile(r'AUTOPILOT_RESULT:\s*(\{[^\r\n]+\})')
RESULT_STATES = {"succeeded", "needs-human", "blocked", "failed"}
PLACEHOLDERS = {"repository", "repo_path", "issue_url", "issue_number"}


class WatcherError(RuntimeError):
    pass


def now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def iso(value: dt.datetime | None = None) -> str:
    return (value or now()).replace(microsecond=0).isoformat()


def parse_time(value: str) -> dt.datetime:
    parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise WatcherError(f"timestamp must include a timezone: {value}")
    return parsed.astimezone(dt.timezone.utc)


def load_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise WatcherError(f"file not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise WatcherError(f"invalid JSON in {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise WatcherError(f"expected a JSON object: {path}")
    return value


def expand_path(value: str, base: Path) -> Path:
    path = Path(os.path.expandvars(os.path.expanduser(value)))
    return (path if path.is_absolute() else base / path).resolve()


def load_config(path: Path) -> dict[str, Any]:
    config = load_object(path)
    if config.get("schema_version") != SCHEMA_VERSION:
        raise WatcherError("config schema_version must be 1")
    base = path.resolve().parent
    config["state_db"] = expand_path(str(config.get("state_db", "autopilot.sqlite3")), base)
    repositories = config.get("repositories")
    if not isinstance(repositories, list) or not repositories:
        raise WatcherError("repositories must be a non-empty array")
    for item in repositories:
        if not isinstance(item, dict) or not REPOSITORY.fullmatch(str(item.get("repository", ""))):
            raise WatcherError("every repository must use owner/name")
        if not isinstance(item.get("author"), str) or not item["author"].strip():
            raise WatcherError(f"{item.get('repository')}: author is required")
        if "activate_after" not in item:
            raise WatcherError(f"{item['repository']}: activate_after is required")
        item["activate_after"] = parse_time(str(item["activate_after"]))
        item["repo_path"] = expand_path(str(item.get("repo_path", "")), base)
        if not item["repo_path"].is_dir():
            raise WatcherError(f"repo_path is not a directory: {item['repo_path']}")
        labels = item.get("labels", [])
        if not isinstance(labels, list) or any(not isinstance(label, str) or not label for label in labels):
            raise WatcherError(f"{item['repository']}: labels must be strings")
        try:
            config["state_db"].relative_to(item["repo_path"])
        except ValueError:
            pass
        else:
            raise WatcherError("state_db must be outside every managed repository")
    executor = config.get("executor")
    if not isinstance(executor, dict) or not isinstance(executor.get("argv"), list) or not executor["argv"]:
        raise WatcherError("executor.argv must be a non-empty array")
    if any(not isinstance(arg, str) or not arg for arg in executor["argv"]):
        raise WatcherError("executor.argv entries must be non-empty strings")
    timeout = int(executor.get("timeout_seconds", 2700))
    lease = int(config.get("lease_timeout_seconds", timeout + 300))
    if timeout < 1 or lease <= timeout:
        raise WatcherError("lease_timeout_seconds must exceed executor.timeout_seconds")
    config["executor"]["timeout_seconds"] = timeout
    config["lease_timeout_seconds"] = lease
    config["poll_interval_seconds"] = max(1, int(config.get("poll_interval_seconds", 180)))
    config["max_attempts"] = max(1, int(config.get("max_attempts", 2)))
    config["max_dispatch_per_poll"] = max(1, int(config.get("max_dispatch_per_poll", 1)))
    policy = config.get("policy", {})
    if policy.get("scope_approval") != "eligible-issue":
        raise WatcherError("policy.scope_approval must be eligible-issue")
    if policy.get("publication", "never") != "never":
        raise WatcherError("only policy.publication=never is supported")
    if policy.get("max_risk", "medium") not in {"low", "medium"}:
        raise WatcherError("policy.max_risk must be low or medium")
    config["policy"] = policy
    return config


class Ledger:
    def __init__(self, path: Path, lease_seconds: int, max_attempts: int) -> None:
        self.path = path
        self.lease_seconds = lease_seconds
        self.max_attempts = max_attempts
        path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path, timeout=30, isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS metadata (
              key TEXT PRIMARY KEY,
              value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS issues (
              node_id TEXT PRIMARY KEY,
              repository TEXT NOT NULL,
              issue_number INTEGER NOT NULL,
              url TEXT NOT NULL UNIQUE,
              title TEXT NOT NULL,
              created_at TEXT NOT NULL,
              issue_updated_at TEXT NOT NULL,
              status TEXT NOT NULL,
              attempts INTEGER NOT NULL DEFAULT 0,
              lease_started_at TEXT,
              lease_owner TEXT,
              pid INTEGER,
              summary TEXT,
              returncode INTEGER,
              log_path TEXT
            );
            CREATE TABLE IF NOT EXISTS events (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              node_id TEXT NOT NULL REFERENCES issues(node_id),
              at TEXT NOT NULL,
              kind TEXT NOT NULL,
              detail TEXT NOT NULL DEFAULT '{}'
            );
            """
        )
        self.connection.execute(
            "INSERT OR IGNORE INTO metadata(key, value) VALUES ('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )
        version = self.connection.execute(
            "SELECT value FROM metadata WHERE key='schema_version'"
        ).fetchone()[0]
        if version != str(SCHEMA_VERSION):
            raise WatcherError(f"unsupported ledger schema version: {version}")

    def enqueue(self, issue: dict[str, Any]) -> bool:
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            cursor = self.connection.execute(
                """INSERT OR IGNORE INTO issues
                   (node_id, repository, issue_number, url, title, created_at, issue_updated_at, status)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 'queued')""",
                (issue["id"], issue["repository"], issue["number"], issue["url"], issue.get("title", ""),
                 issue["createdAt"], issue["updatedAt"]),
            )
            if cursor.rowcount:
                self._event(issue["id"], "discovered", {"url": issue["url"]})
            self.connection.execute("COMMIT")
            return bool(cursor.rowcount)
        except Exception:
            self.connection.execute("ROLLBACK")
            raise

    def claim_next(self, at: dt.datetime | None = None) -> dict[str, Any] | None:
        current = at or now()
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            active = self.connection.execute(
                "SELECT * FROM issues WHERE status IN ('claimed', 'running')"
            ).fetchall()
            for row in active:
                live_local_process = row["lease_owner"] == socket.gethostname() and pid_alive(int(row["pid"] or 0))
                live_lease = (current - parse_time(row["lease_started_at"])).total_seconds() <= self.lease_seconds
                if live_local_process or live_lease:
                    self.connection.execute("COMMIT")
                    return None
                if row["attempts"] >= self.max_attempts:
                    self.connection.execute(
                        "UPDATE issues SET status='blocked', summary='stale lease exhausted attempt budget', pid=NULL WHERE node_id=?",
                        (row["node_id"],),
                    )
                    self._event(row["node_id"], "stale_lease_blocked", {"attempts": row["attempts"]})
            rows = self.connection.execute(
                """SELECT * FROM issues
                   WHERE status IN ('queued', 'retry-pending', 'claimed', 'running')
                     AND attempts < ? ORDER BY created_at, issue_number""",
                (self.max_attempts,),
            ).fetchall()
            chosen = None
            for row in rows:
                chosen = row
                break
            if chosen is None:
                self.connection.execute("COMMIT")
                return None
            attempts = chosen["attempts"] + 1
            self.connection.execute(
                """UPDATE issues SET status='claimed', attempts=?, lease_started_at=?, lease_owner=?, pid=NULL
                   WHERE node_id=?""",
                (attempts, iso(current), socket.gethostname(), chosen["node_id"]),
            )
            self._event(chosen["node_id"], "claimed", {"attempt": attempts})
            self.connection.execute("COMMIT")
            return dict(self.connection.execute("SELECT * FROM issues WHERE node_id=?", (chosen["node_id"],)).fetchone())
        except Exception:
            self.connection.execute("ROLLBACK")
            raise

    def running(self, node_id: str, pid: int) -> None:
        self._set(node_id, "running", pid=pid, kind="started")

    def finish(self, node_id: str, status: str, **fields: Any) -> None:
        if status not in RESULT_STATES:
            raise WatcherError(f"invalid result status: {status}")
        self._set(node_id, status, pid=None, kind="finished", **fields)

    def retry(self, url: str) -> None:
        row = self.connection.execute("SELECT * FROM issues WHERE url=?", (url,)).fetchone()
        if row is None:
            raise WatcherError(f"unknown Issue URL: {url}")
        if row["status"] not in RESULT_STATES:
            raise WatcherError("only a terminal Issue can be retried")
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            self.connection.execute(
                """UPDATE issues SET status='retry-pending', attempts=0, lease_started_at=NULL,
                   lease_owner=NULL, pid=NULL, summary=NULL, returncode=NULL, log_path=NULL WHERE node_id=?""",
                (row["node_id"],),
            )
            self._event(row["node_id"], "retry_requested", {})
            self.connection.execute("COMMIT")
        except Exception:
            self.connection.execute("ROLLBACK")
            raise

    def snapshot(self) -> dict[str, Any]:
        rows = [dict(row) for row in self.connection.execute("SELECT * FROM issues ORDER BY created_at, issue_number")]
        return {"schema_version": SCHEMA_VERSION, "issues": rows}

    def _set(self, node_id: str, status: str, kind: str, **fields: Any) -> None:
        allowed = {"pid", "summary", "returncode", "log_path"}
        if any(key not in allowed for key in fields):
            raise WatcherError("unsupported ledger field")
        assignments = ["status=?"] + [f"{key}=?" for key in fields]
        values = [status] + list(fields.values()) + [node_id]
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            self.connection.execute(f"UPDATE issues SET {', '.join(assignments)} WHERE node_id=?", values)
            self._event(node_id, kind, {"status": status})
            self.connection.execute("COMMIT")
        except Exception:
            self.connection.execute("ROLLBACK")
            raise

    def _event(self, node_id: str, kind: str, detail: dict[str, Any]) -> None:
        self.connection.execute(
            "INSERT INTO events(node_id, at, kind, detail) VALUES (?, ?, ?, ?)",
            (node_id, iso(), kind, json.dumps(detail, ensure_ascii=False, sort_keys=True)),
        )


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def signal_process_group(process: subprocess.Popen[str], signal_number: int) -> None:
    try:
        os.killpg(process.pid, signal_number)
    except ProcessLookupError:
        pass


def command(argv: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, cwd=cwd, check=False, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def github_login() -> str:
    result = command(["gh", "api", "user", "--jq", ".login"])
    if result.returncode:
        raise WatcherError(result.stderr.strip() or "gh api user failed")
    return result.stdout.strip()


def repository_metadata(repository: dict[str, Any]) -> dict[str, Any]:
    result = command([
        "gh", "repo", "view", repository["repository"],
        "--json", "id,nameWithOwner,isArchived,hasIssuesEnabled",
    ])
    if result.returncode:
        raise WatcherError(result.stderr.strip() or f"failed to inspect {repository['repository']}")
    try:
        metadata = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise WatcherError(f"invalid repository metadata: {exc}") from exc
    if metadata.get("nameWithOwner", "").lower() != repository["repository"].lower():
        raise WatcherError(f"repository canonical name mismatch: {metadata.get('nameWithOwner')}")
    if metadata.get("isArchived") or not metadata.get("hasIssuesEnabled"):
        raise WatcherError(f"repository is archived or Issues are disabled: {repository['repository']}")
    configured_id = repository.get("repository_id")
    if configured_id and configured_id != metadata.get("id"):
        raise WatcherError(f"repository node ID mismatch: {repository['repository']}")
    return metadata


def list_issues(repository: dict[str, Any], login: str) -> list[dict[str, Any]]:
    repository_metadata(repository)
    slug = repository["repository"]
    author = login if repository["author"] == "@me" else repository["author"]
    result = command([
        "gh", "issue", "list", "--repo", slug, "--state", "open", "--limit", "1000",
        "--author", author, "--json", "id,number,url,title,author,labels,createdAt,updatedAt",
    ])
    if result.returncode:
        raise WatcherError(result.stderr.strip() or f"failed to list Issues for {slug}")
    try:
        values = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise WatcherError(f"invalid gh output for {slug}: {exc}") from exc
    required_labels = set(repository.get("labels", []))
    eligible = []
    for issue in values:
        labels = {item.get("name") for item in issue.get("labels", [])}
        if ((issue.get("author") or {}).get("login", "").lower() == author.lower()
                and required_labels.issubset(labels)
                and parse_time(issue["createdAt"]) >= repository["activate_after"]):
            issue["repository"] = slug
            eligible.append(issue)
    return eligible


def poll(config: dict[str, Any], ledger: Ledger) -> dict[str, Any]:
    login = github_login()
    observed = enqueued = 0
    for repository in config["repositories"]:
        for issue in list_issues(repository, login):
            observed += 1
            enqueued += int(ledger.enqueue(issue))
    return {"observed": observed, "enqueued": enqueued}


def parse_receipt(output: str) -> tuple[str, str] | None:
    matches = RECEIPT.findall(output)
    if not matches:
        return None
    try:
        value = json.loads(matches[-1])
    except json.JSONDecodeError:
        return None
    if value.get("status") not in RESULT_STATES or not isinstance(value.get("summary"), str):
        return None
    return value["status"], value["summary"]


def prompt_for(run: dict[str, Any], config: dict[str, Any]) -> str:
    return (
        f"Use $github-issue-repair {run['url']} in GitHub Issue Autopilot mode. The trusted local dispatcher "
        f"verified that this open Issue matches its repository, author, activation date, and optional labels. "
        f"Eligibility is standing authorization for one local work package with risk no greater than "
        f"{config['policy'].get('max_risk', 'medium')}. Revalidate the Issue and repository. Use the repair skill's "
        "isolated worktree and verification gates. Stop on ambiguity, unsafe work, scope expansion, dependency upgrades, "
        "migrations, security/auth/payment changes, public API changes, destructive operations, or a broad diff. Do not "
        "push, create a PR, merge, close, comment, label, release, or deploy. Finish with exactly one line: "
        "AUTOPILOT_RESULT: followed by JSON. The status must be exactly one of succeeded, needs-human, blocked, or "
        "failed; include a concise summary."
    )


def expanded_argv(config: dict[str, Any], repository: dict[str, Any], run: dict[str, Any]) -> list[str]:
    values = {"repository": repository["repository"], "repo_path": str(repository["repo_path"]),
              "issue_url": run["url"], "issue_number": str(run["issue_number"])}
    expanded = []
    for argument in config["executor"]["argv"]:
        for name, value in values.items():
            argument = argument.replace("{" + name + "}", value)
        expanded.append(argument)
    return expanded


def work_once(config: dict[str, Any], ledger: Ledger) -> dict[str, Any] | None:
    run = ledger.claim_next()
    if run is None:
        return None
    repository = next((item for item in config["repositories"] if item["repository"] == run["repository"]), None)
    if repository is None:
        ledger.finish(run["node_id"], "blocked", summary="repository removed from policy")
        return {"url": run["url"], "status": "blocked"}
    current = {item["id"]: item for item in list_issues(repository, github_login())}
    if run["node_id"] not in current:
        ledger.finish(run["node_id"], "blocked", summary="Issue is no longer open or eligible")
        return {"url": run["url"], "status": "blocked"}
    logs = config["state_db"].parent / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    log_path = logs / f"{run['repository'].replace('/', '-')}-{run['issue_number']}-attempt-{run['attempts']}.log"
    try:
        process = subprocess.Popen(
            expanded_argv(config, repository, run), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, cwd=repository["repo_path"], start_new_session=True,
        )
    except OSError as exc:
        ledger.finish(run["node_id"], "failed", summary=str(exc), log_path=str(log_path))
        return {"url": run["url"], "status": "failed", "summary": str(exc)}
    ledger.running(run["node_id"], process.pid)
    try:
        output, _ = process.communicate(prompt_for(run, config), timeout=config["executor"]["timeout_seconds"])
    except subprocess.TimeoutExpired:
        signal_process_group(process, signal.SIGTERM)
        try:
            output, _ = process.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            signal_process_group(process, signal.SIGKILL)
            output, _ = process.communicate()
        log_path.write_text(output, encoding="utf-8")
        ledger.finish(run["node_id"], "failed", summary="executor timed out", log_path=str(log_path))
        return {"url": run["url"], "status": "failed", "summary": "executor timed out"}
    log_path.write_text(output, encoding="utf-8")
    receipt = parse_receipt(output)
    status, summary = receipt or ("needs-human", "executor returned no valid AUTOPILOT_RESULT receipt")
    if process.returncode:
        status, summary = "failed", f"executor exited {process.returncode}; {summary}"
    ledger.finish(run["node_id"], status, summary=summary, returncode=process.returncode, log_path=str(log_path))
    return {"url": run["url"], "status": status, "summary": summary, "log": str(log_path)}


def doctor(config: dict[str, Any]) -> dict[str, Any]:
    executable = config["executor"]["argv"][0]
    result: dict[str, Any] = {
        "gh": shutil.which("gh") is not None,
        "executor": shutil.which(executable) is not None if "/" not in executable else Path(executable).is_file(),
        "repositories": [],
    }
    result["gh_auth"] = result["gh"] and command(["gh", "auth", "status"]).returncode == 0
    for item in config["repositories"]:
        git = command(["git", "-C", str(item["repo_path"]), "rev-parse", "--show-toplevel"])
        check = {"repository": item["repository"], "repo_path": str(item["repo_path"]),
                 "git_root": git.returncode == 0 and Path(git.stdout.strip()).resolve() == item["repo_path"]}
        try:
            repository_metadata(item)
            check["github"] = True
        except WatcherError as exc:
            check["github"] = False
            check["error"] = str(exc)
        result["repositories"].append(check)
    result["ok"] = bool(result["gh"] and result["gh_auth"] and result["executor"]
                        and all(item["git_root"] and item["github"] for item in result["repositories"]))
    return result


def parser() -> argparse.ArgumentParser:
    main = argparse.ArgumentParser(description=__doc__)
    commands = main.add_subparsers(dest="command_name", required=True)
    for name in ("doctor", "poll", "work", "once", "run", "status", "retry"):
        sub = commands.add_parser(name)
        sub.add_argument("--config", type=Path, required=True)
        if name == "retry":
            sub.add_argument("--issue-url", required=True)
    return main


def main() -> int:
    args = parser().parse_args()
    try:
        config = load_config(args.config)
        ledger = Ledger(config["state_db"], config["lease_timeout_seconds"], config["max_attempts"])
        if args.command_name == "doctor":
            result = doctor(config)
            code = 0 if result["ok"] else 2
        elif args.command_name == "status":
            result, code = ledger.snapshot(), 0
        elif args.command_name == "retry":
            ledger.retry(args.issue_url)
            result, code = {"status": "retry-pending", "url": args.issue_url}, 0
        elif args.command_name == "poll":
            result, code = poll(config, ledger), 0
        elif args.command_name == "work":
            result, code = {"result": work_once(config, ledger)}, 0
        elif args.command_name == "once":
            detected = poll(config, ledger)
            completed = []
            for _ in range(config["max_dispatch_per_poll"]):
                item = work_once(config, ledger)
                if item is None:
                    break
                completed.append(item)
            result, code = {"detection": detected, "completed": completed}, 0
        else:
            while True:
                print(json.dumps({"detection": poll(config, ledger), "result": work_once(config, ledger)}, ensure_ascii=False), flush=True)
                time.sleep(config["poll_interval_seconds"])
    except (WatcherError, OSError, ValueError, sqlite3.Error) as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
