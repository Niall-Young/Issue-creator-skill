#!/usr/bin/env python3
"""Poll GitHub Issues and dispatch visible, supervised Orca worktrees."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

CONFIG_VERSION = 2
LEDGER_VERSION = 3
REPOSITORY = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
RECEIPT = re.compile(r"(?m)^AUTOPILOT_RESULT: (\{[^\r\n]+\})[ \t]*$")
CHILD_STATES = {"succeeded", "ready-for-review", "needs-human", "blocked", "failed"}
STOPPED_STATES = {"ready-for-review", "needs-human", "blocked", "failed"}


class WatcherError(RuntimeError):
    pass


class OrcaCommandError(WatcherError):
    def __init__(self, message: str, response: dict[str, Any]) -> None:
        super().__init__(message)
        self.response = response


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
    if config.get("schema_version") != CONFIG_VERSION:
        raise WatcherError("config schema_version must be 2")
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
    orca = config.get("orca")
    if not isinstance(orca, dict) or not isinstance(orca.get("cli"), str) or not orca["cli"]:
        raise WatcherError("orca.cli is required")
    default_agent = orca.get("default_agent")
    allowed_agents = orca.get("allowed_agents")
    if not isinstance(default_agent, str) or not default_agent:
        raise WatcherError("orca.default_agent is required")
    if (not isinstance(allowed_agents, list) or not allowed_agents
            or any(not isinstance(agent, str) or not agent for agent in allowed_agents)):
        raise WatcherError("orca.allowed_agents must be a non-empty string array")
    if default_agent not in allowed_agents:
        raise WatcherError("orca.default_agent must be included in orca.allowed_agents")
    orca["setup"] = orca.get("setup", "run")
    if orca["setup"] not in {"run", "skip", "inherit"}:
        raise WatcherError("orca.setup must be run, skip, or inherit")
    lease = int(config.get("lease_timeout_seconds", 3000))
    if lease < 1:
        raise WatcherError("lease_timeout_seconds must be positive")
    config["lease_timeout_seconds"] = lease
    config["poll_interval_seconds"] = max(1, int(config.get("poll_interval_seconds", 180)))
    config["max_concurrent_workers"] = max(1, min(3, int(config.get("max_concurrent_workers", 3))))
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
    """SQLite queue with immutable Issue identities and per-attempt history."""

    def __init__(self, path: Path, lease_seconds: int, max_attempts: int = 2) -> None:
        self.path = path
        self.lease_seconds = lease_seconds
        self.max_attempts = max_attempts  # compatible with v1 configs; retries are now explicit
        path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path, timeout=30, isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA foreign_keys=ON")
        self._initialize()

    def _initialize(self) -> None:
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS issues (
              node_id TEXT PRIMARY KEY, repository TEXT NOT NULL, issue_number INTEGER NOT NULL,
              url TEXT NOT NULL UNIQUE, title TEXT NOT NULL, created_at TEXT NOT NULL,
              issue_updated_at TEXT NOT NULL, status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
              lease_started_at TEXT, lease_owner TEXT, pid INTEGER, summary TEXT,
              returncode INTEGER, log_path TEXT
            );
            CREATE TABLE IF NOT EXISTS events (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              node_id TEXT NOT NULL REFERENCES issues(node_id), at TEXT NOT NULL,
              kind TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '{}'
            );
            CREATE TABLE IF NOT EXISTS attempts (
              node_id TEXT NOT NULL REFERENCES issues(node_id), attempt_number INTEGER NOT NULL,
              status TEXT NOT NULL, lease_started_at TEXT, lease_owner TEXT, pid INTEGER,
              run_id TEXT, worktree_path TEXT, branch TEXT, base_sha TEXT, head_sha TEXT,
              summary TEXT, returncode INTEGER, log_path TEXT, agent_id TEXT,
              orca_task_id TEXT, orca_dispatch_id TEXT, orca_worktree_id TEXT,
              PRIMARY KEY(node_id, attempt_number)
            );
            """
        )
        row = self.connection.execute("SELECT value FROM metadata WHERE key='schema_version'").fetchone()
        if row is None:
            self.connection.execute("INSERT INTO metadata(key, value) VALUES ('schema_version', ?)",
                                    (str(LEDGER_VERSION),))
        elif row[0] == "1":
            self._migrate_v1()
            self._migrate_v2()
            self.connection.execute("UPDATE metadata SET value=? WHERE key='schema_version'",
                                    (str(LEDGER_VERSION),))
        elif row[0] == "2":
            self._migrate_v2()
            self.connection.execute("UPDATE metadata SET value=? WHERE key='schema_version'",
                                    (str(LEDGER_VERSION),))
        elif row[0] != str(LEDGER_VERSION):
            raise WatcherError(f"unsupported ledger schema version: {row[0]}")

    def _migrate_v1(self) -> None:
        for row in self.connection.execute("SELECT * FROM issues WHERE attempts > 0").fetchall():
            status = "ready-for-review" if row["status"] == "succeeded" else row["status"]
            self.connection.execute(
                """INSERT OR IGNORE INTO attempts
                   (node_id, attempt_number, status, lease_started_at, lease_owner, pid,
                    summary, returncode, log_path) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (row["node_id"], row["attempts"], status, row["lease_started_at"], row["lease_owner"],
                 row["pid"], row["summary"], row["returncode"], row["log_path"]),
            )
            self.connection.execute("UPDATE issues SET status=? WHERE node_id=?", (status, row["node_id"]))

    def _migrate_v2(self) -> None:
        existing = {row[1] for row in self.connection.execute("PRAGMA table_info(attempts)")}
        for name in ("agent_id", "orca_task_id", "orca_dispatch_id", "orca_worktree_id"):
            if name not in existing:
                self.connection.execute(f"ALTER TABLE attempts ADD COLUMN {name} TEXT")

    def metadata(self, key: str) -> str | None:
        row = self.connection.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
        return str(row[0]) if row else None

    def set_metadata(self, key: str, value: str) -> None:
        self.connection.execute(
            "INSERT INTO metadata(key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value),
        )

    def cursor(self, repository: str, default: dt.datetime) -> dt.datetime:
        key = f"poll_cursor:{repository.lower()}"
        row = self.connection.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
        return parse_time(row[0]) if row else default

    def advance_cursor(self, repository: str, value: dt.datetime) -> None:
        key = f"poll_cursor:{repository.lower()}"
        self.connection.execute(
            "INSERT INTO metadata(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, iso(value)),
        )

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

    def claim_many(self, limit: int, at: dt.datetime | None = None) -> list[dict[str, Any]]:
        current = at or now()
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            for row in self.connection.execute(
                "SELECT * FROM issues WHERE status='claimed' ORDER BY created_at"
            ).fetchall():
                local_live = row["lease_owner"] == socket.gethostname() and pid_alive(int(row["pid"] or 0))
                lease_live = bool(row["lease_started_at"]) and (
                    current - parse_time(row["lease_started_at"])
                ).total_seconds() <= self.lease_seconds
                if local_live or lease_live:
                    continue
                self._finish_stale(row)
            active = int(self.connection.execute(
                "SELECT COUNT(*) FROM issues WHERE status IN ('claimed', 'running')"
            ).fetchone()[0])
            available = max(0, limit - active)
            chosen = self.connection.execute(
                """SELECT * FROM issues WHERE status IN ('queued', 'retry-pending')
                   ORDER BY created_at, issue_number LIMIT ?""", (available,)
            ).fetchall()
            if not chosen:
                self.connection.execute("COMMIT")
                return []
            claimed: list[dict[str, Any]] = []
            for row in chosen:
                attempt = int(self.connection.execute(
                    "SELECT COALESCE(MAX(attempt_number), 0) + 1 FROM attempts WHERE node_id=?",
                    (row["node_id"],),
                ).fetchone()[0])
                started, owner = iso(current), socket.gethostname()
                self.connection.execute(
                    """UPDATE issues SET status='claimed', attempts=?, lease_started_at=?, lease_owner=?,
                       pid=NULL, summary=NULL, returncode=NULL, log_path=NULL WHERE node_id=?""",
                    (attempt, started, owner, row["node_id"]),
                )
                self.connection.execute(
                    "INSERT INTO attempts(node_id, attempt_number, status, lease_started_at, lease_owner) "
                    "VALUES (?, ?, 'claimed', ?, ?)", (row["node_id"], attempt, started, owner),
                )
                self._event(row["node_id"], "claimed", {"attempt": attempt})
                value = dict(row)
                value["attempt_number"] = attempt
                claimed.append(value)
            self.connection.execute("COMMIT")
            return claimed
        except Exception:
            self.connection.execute("ROLLBACK")
            raise

    def claim_next(self, at: dt.datetime | None = None) -> dict[str, Any] | None:
        values = self.claim_many(1, at)
        return values[0] if values else None

    def _finish_stale(self, row: sqlite3.Row) -> None:
        summary = "worker lease expired; inspect the recorded attempt before retrying"
        self.connection.execute("UPDATE issues SET status='needs-human', summary=?, pid=NULL WHERE node_id=?",
                                (summary, row["node_id"]))
        self.connection.execute(
            "UPDATE attempts SET status='needs-human', summary=?, pid=NULL WHERE node_id=? AND attempt_number=?",
            (summary, row["node_id"], row["attempts"]),
        )
        self._event(row["node_id"], "stale_worker", {"attempt": row["attempts"]})

    def running(self, node_id: str, attempt: int, pid: int) -> None:
        self._update(node_id, attempt, "running", {"pid": pid}, "started")

    def assign_artifacts(self, node_id: str, attempt: int, run_id: str, worktree: Path, branch: str) -> None:
        self._update(node_id, attempt, "claimed", {
            "run_id": run_id, "worktree_path": str(worktree), "branch": branch,
        }, "artifacts_assigned")

    def assign_orca(self, node_id: str, attempt: int, **fields: Any) -> None:
        self._update(node_id, attempt, "running", fields, "orca_dispatched")

    def active_attempts(self) -> list[dict[str, Any]]:
        return [dict(row) for row in self.connection.execute(
            """SELECT attempts.*, issues.repository, issues.issue_number, issues.url, issues.title
               FROM attempts JOIN issues USING(node_id)
               WHERE attempts.status='running' ORDER BY issues.created_at, issues.issue_number"""
        )]

    def finish(self, node_id: str, attempt: int, status: str, **fields: Any) -> None:
        if status == "succeeded":
            status = "ready-for-review"
        if status not in STOPPED_STATES:
            raise WatcherError(f"invalid result status: {status}")
        fields["pid"] = None
        self._update(node_id, attempt, status, fields, "finished")

    def _update(self, node_id: str, attempt: int, status: str, fields: dict[str, Any], kind: str) -> None:
        allowed = {"pid", "run_id", "worktree_path", "branch", "base_sha", "head_sha",
                   "summary", "returncode", "log_path", "agent_id", "orca_task_id",
                   "orca_dispatch_id", "orca_worktree_id"}
        if any(key not in allowed for key in fields):
            raise WatcherError("unsupported attempt field")
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            assignments = ["status=?", *[f"{key}=?" for key in fields]]
            cursor = self.connection.execute(
                f"UPDATE attempts SET {', '.join(assignments)} WHERE node_id=? AND attempt_number=?",
                [status, *fields.values(), node_id, attempt],
            )
            if not cursor.rowcount:
                raise WatcherError("attempt not found")
            issue_fields = {key: value for key, value in fields.items()
                            if key in {"pid", "summary", "returncode", "log_path"}}
            issue_assignments = ["status=?", *[f"{key}=?" for key in issue_fields]]
            self.connection.execute(
                f"UPDATE issues SET {', '.join(issue_assignments)} WHERE node_id=?",
                [status, *issue_fields.values(), node_id],
            )
            self._event(node_id, kind, {"attempt": attempt, "status": status})
            self.connection.execute("COMMIT")
        except Exception:
            self.connection.execute("ROLLBACK")
            raise

    def retry(self, url: str, repo: Path, discard: bool, orca_cli: str | None = None) -> int:
        issue = self.connection.execute("SELECT * FROM issues WHERE url=?", (url,)).fetchone()
        if issue is None:
            raise WatcherError(f"unknown Issue URL: {url}")
        if issue["status"] not in STOPPED_STATES:
            raise WatcherError("only a stopped Issue can be retried")
        row = self.connection.execute(
            "SELECT * FROM attempts WHERE node_id=? AND attempt_number=?", (issue["node_id"], issue["attempts"])
        ).fetchone()
        attempt = dict(row) if row else {}
        if pid_alive(int(attempt.get("pid") or 0)):
            raise WatcherError("the recorded worker is still running")
        if attempt.get("orca_dispatch_id") and orca_cli:
            try:
                state = json_command([orca_cli, "orchestration", "worker-show", "--dispatch",
                                      attempt["orca_dispatch_id"], "--json"])
            except WatcherError:
                state = {}
            if state and worker_state(state) == "running":
                raise WatcherError("the recorded Orca worker is still running")
        if discard:
            cleanup_attempt(repo, attempt, force=True, orca_cli=orca_cli)
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            if row:
                self.connection.execute(
                    "UPDATE attempts SET status='rejected', pid=NULL WHERE node_id=? AND attempt_number=?",
                    (issue["node_id"], issue["attempts"]),
                )
            self.connection.execute(
                """UPDATE issues SET status='retry-pending', lease_started_at=NULL, lease_owner=NULL,
                   pid=NULL, summary=NULL, returncode=NULL, log_path=NULL WHERE node_id=?""", (issue["node_id"],),
            )
            self._event(issue["node_id"], "retry_requested", {"discarded": discard})
            self.connection.execute("COMMIT")
        except Exception:
            self.connection.execute("ROLLBACK")
            raise
        return int(issue["attempts"]) + 1

    def accept(self, url: str, repo: Path, target_branch: str, orca_cli: str | None = None) -> dict[str, str]:
        issue = self.connection.execute("SELECT * FROM issues WHERE url=?", (url,)).fetchone()
        if issue is None or issue["status"] != "ready-for-review":
            raise WatcherError("Issue must be ready-for-review before acceptance")
        attempt = dict(self.connection.execute(
            "SELECT * FROM attempts WHERE node_id=? AND attempt_number=?", (issue["node_id"], issue["attempts"])
        ).fetchone())
        if git(repo, "branch", "--show-current") != target_branch:
            raise WatcherError(f"target branch {target_branch} is not checked out")
        if git(repo, "status", "--porcelain"):
            raise WatcherError("target worktree must be clean before merging")
        worktree_value = attempt.get("worktree_path")
        registered = (isinstance(worktree_value, str) and Path(worktree_value).is_absolute()
                      and Path(worktree_value).resolve() in worktree_paths(repo))
        if registered:
            validate_attempt(repo, attempt)
            result = command(["git", "-C", str(repo), "merge", "--no-ff", "--no-edit",
                              attempt["head_sha"]])
            if result.returncode:
                raise WatcherError(result.stderr.strip() or "local merge failed")
        else:
            validate_already_merged_attempt(repo, attempt, url)
        close_github_issue(url)
        self._update(issue["node_id"], issue["attempts"], "accepted",
                     {"summary": "merged locally and closed Issue"}, "accepted")
        response = {"status": "accepted", "target_branch": target_branch,
                    "head": git(repo, "rev-parse", "HEAD"), "issue": "closed"}
        try:
            cleanup_attempt(repo, attempt, force=False, orca_cli=orca_cli)
        except WatcherError as exc:
            response["cleanup_warning"] = str(exc)
        return response

    def snapshot(self) -> dict[str, Any]:
        issues = [dict(row) for row in self.connection.execute(
            "SELECT * FROM issues ORDER BY created_at, issue_number"
        )]
        for issue in issues:
            issue["attempt_history"] = [dict(row) for row in self.connection.execute(
                "SELECT * FROM attempts WHERE node_id=? ORDER BY attempt_number", (issue["node_id"],)
            )]
        return {"schema_version": LEDGER_VERSION, "issues": issues}

    def _event(self, node_id: str, kind: str, detail: dict[str, Any]) -> None:
        self.connection.execute("INSERT INTO events(node_id, at, kind, detail) VALUES (?, ?, ?, ?)",
                                (node_id, iso(), kind, json.dumps(detail, ensure_ascii=False, sort_keys=True)))


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


def github_issue_state(url: str) -> str:
    result = command(["gh", "issue", "view", url, "--json", "state,url"])
    if result.returncode:
        raise WatcherError(result.stderr.strip() or "could not read GitHub Issue state")
    try:
        value = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise WatcherError("gh issue view returned invalid JSON") from exc
    if not isinstance(value, dict) or value.get("url") != url or value.get("state") not in {"OPEN", "CLOSED"}:
        raise WatcherError("gh issue view returned an unexpected Issue")
    return str(value["state"])


def close_github_issue(url: str) -> None:
    if github_issue_state(url) == "CLOSED":
        return
    result = command(["gh", "issue", "close", url, "--reason", "completed"])
    if result.returncode and github_issue_state(url) != "CLOSED":
        raise WatcherError(result.stderr.strip() or "GitHub Issue close failed")
    if github_issue_state(url) != "CLOSED":
        raise WatcherError("GitHub Issue remained open after close")


def json_command(argv: list[str], cwd: Path | None = None) -> dict[str, Any]:
    result = command(argv, cwd)
    try:
        value = json.loads(result.stdout or result.stderr)
    except json.JSONDecodeError as exc:
        raise WatcherError(result.stderr.strip() or result.stdout.strip() or f"{argv[0]} returned invalid JSON") from exc
    if result.returncode or value.get("ok") is False:
        error = value.get("error") or {}
        raise WatcherError(error.get("message") or result.stderr.strip() or f"{argv[0]} command failed")
    return value


def nested_value(value: Any, *paths: tuple[str, ...]) -> Any:
    for path in paths:
        current = value
        for key in path:
            if not isinstance(current, dict) or key not in current:
                break
            current = current[key]
        else:
            if current is not None:
                return current
    return None


def string_content(value: Any) -> str:
    values: list[str] = []
    if isinstance(value, str):
        values.append(value)
    elif isinstance(value, dict):
        for item in value.values():
            values.append(string_content(item))
    elif isinstance(value, list):
        for item in value:
            values.append(string_content(item))
    return "\n".join(item for item in values if item)


def named_values(value: Any, names: set[str]) -> list[str]:
    found: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            if key in names and isinstance(item, str):
                found.append(item.lower())
            found.extend(named_values(item, names))
    elif isinstance(value, list):
        for item in value:
            found.extend(named_values(item, names))
    return found


def find_worktree(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        identity = value.get("id") or value.get("worktreeId")
        path = value.get("path") or value.get("worktreePath")
        if isinstance(identity, str) and isinstance(path, str) and "::" in identity:
            return value
        for item in value.values():
            found = find_worktree(item)
            if found:
                return found
    elif isinstance(value, list):
        for item in value:
            found = find_worktree(item)
            if found:
                return found
    return {}


def find_worktree_id(value: Any) -> str | None:
    if isinstance(value, dict):
        for key in ("worktreeId", "worktree_id"):
            identity = value.get(key)
            if isinstance(identity, str) and "::" in identity:
                return identity
        if value.get("kind") == "worktree":
            identity = value.get("id")
            if isinstance(identity, str) and "::" in identity:
                return identity
        for item in value.values():
            found = find_worktree_id(item)
            if found:
                return found
    elif isinstance(value, list):
        for item in value:
            found = find_worktree_id(item)
            if found:
                return found
    return None


def find_named_string(value: Any, names: set[str]) -> str | None:
    if isinstance(value, dict):
        for key, item in value.items():
            if key in names and isinstance(item, str) and item:
                return item
            found = find_named_string(item, names)
            if found:
                return found
    elif isinstance(value, list):
        for item in value:
            found = find_named_string(item, names)
            if found:
                return found
    return None


def orca_call(config: dict[str, Any], *args: str) -> dict[str, Any]:
    argv = [config["orca"]["cli"], *args, "--json"]
    result = command(argv)
    try:
        value = json.loads(result.stdout or result.stderr)
    except json.JSONDecodeError as exc:
        raise OrcaCommandError(result.stderr.strip() or "Orca returned invalid JSON", {}) from exc
    if result.returncode or value.get("ok") is False:
        error = value.get("error") or {}
        raise OrcaCommandError(error.get("message") or result.stderr.strip() or "Orca command failed", value)
    return value


def git(repo: Path, *args: str) -> str:
    result = command(["git", "-C", str(repo), *args])
    if result.returncode:
        raise WatcherError(result.stderr.strip() or "Git command failed")
    return result.stdout.strip()


def worktree_paths(repo: Path) -> set[Path]:
    return {Path(line.removeprefix("worktree ")).resolve()
            for line in git(repo, "worktree", "list", "--porcelain").splitlines()
            if line.startswith("worktree ")}


def validate_attempt(repo: Path, attempt: dict[str, Any]) -> None:
    required = ("run_id", "worktree_path", "branch", "base_sha", "head_sha")
    if any(not isinstance(attempt.get(key), str) or not attempt[key] for key in required):
        raise WatcherError("successful receipt is missing worktree evidence")
    worktree = Path(attempt["worktree_path"])
    if not worktree.is_absolute() or worktree.resolve() == repo.resolve() or worktree.resolve() not in worktree_paths(repo):
        raise WatcherError("receipt worktree is not a registered isolated worktree")
    if ((not attempt.get("orca_worktree_id") and not attempt["branch"].startswith("repair/"))
            or git(worktree, "branch", "--show-current") != attempt["branch"]):
        raise WatcherError("receipt branch does not match a repair worktree")
    if git(worktree, "rev-parse", "HEAD") != attempt["head_sha"]:
        raise WatcherError("receipt head SHA does not match the worktree")
    if git(worktree, "status", "--porcelain"):
        raise WatcherError("receipt worktree is not clean")
    if git(repo, "rev-parse", f"{attempt['base_sha']}^{{commit}}") != attempt["base_sha"]:
        raise WatcherError("receipt base SHA is invalid")
    state = repair_run_state(repo, attempt["run_id"])
    if (state.get("base_sha") != attempt["base_sha"]
            or state.get("state") not in {"REVIEW", "AWAIT_PUBLICATION_APPROVAL"}):
        raise WatcherError("repair run ledger does not confirm a reviewed local result")


def validate_already_merged_attempt(repo: Path, attempt: dict[str, Any], url: str) -> None:
    required = ("run_id", "worktree_path", "branch", "base_sha", "head_sha")
    if any(not isinstance(attempt.get(key), str) or not attempt[key] for key in required):
        raise WatcherError("successful receipt is missing worktree evidence")
    for name in ("base_sha", "head_sha"):
        if git(repo, "rev-parse", f"{attempt[name]}^{{commit}}") != attempt[name]:
            raise WatcherError(f"receipt {name.removesuffix('_sha')} SHA is invalid")
    state = load_repair_run_state(repo, attempt["run_id"])
    commit_receipts = [receipt.get("value") for receipt in state.get("receipts", {}).values()
                       if isinstance(receipt, dict) and receipt.get("kind") == "commit"]
    if (state.get("base_sha") != attempt["base_sha"]
            or state.get("source_url") != url
            or attempt["head_sha"] not in commit_receipts
            or state.get("state") not in {"REVIEW", "AWAIT_PUBLICATION_APPROVAL"}):
        raise WatcherError("repair run ledger does not confirm a reviewed local result")
    merged = command(["git", "-C", str(repo), "merge-base", "--is-ancestor",
                      attempt["head_sha"], "HEAD"])
    if merged.returncode:
        raise WatcherError("recorded head is not merged into the target branch")


def repair_run_state(repo: Path, run_id: str) -> dict[str, Any]:
    state = load_repair_run_state(repo, run_id)
    common = git_common_dir(repo)
    recorded_repo = Path(state.get("repository", "")).resolve()
    try:
        same_git_repository = git_common_dir(recorded_repo) == common
    except (WatcherError, OSError):
        same_git_repository = False
    if recorded_repo != repo.resolve() and not same_git_repository:
        raise WatcherError("repair run ledger belongs to a different run or repository")
    return state


def load_repair_run_state(repo: Path, run_id: str) -> dict[str, Any]:
    try:
        uuid.UUID(run_id)
    except ValueError as exc:
        raise WatcherError("receipt run ID is not a UUID") from exc
    common = git_common_dir(repo)
    state_path = common / "issue-repair" / "runs" / run_id / "state.json"
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        raise WatcherError("repair run ledger is missing or invalid") from exc
    if state.get("run_id") != run_id:
        raise WatcherError("repair run ledger belongs to a different run")
    return state


def git_common_dir(repo: Path) -> Path:
    value = Path(git(repo, "rev-parse", "--git-common-dir"))
    return (value if value.is_absolute() else repo / value).resolve()


def cleanup_attempt(repo: Path, attempt: dict[str, Any], force: bool, orca_cli: str | None = None) -> None:
    worktree_value, branch = attempt.get("worktree_path"), attempt.get("branch")
    if not worktree_value and not branch:
        return
    if not worktree_value or not branch or not attempt.get("run_id"):
        raise WatcherError("recorded attempt lacks a complete cleanup identity")
    orca_worktree_id = attempt.get("orca_worktree_id")
    if orca_worktree_id and orca_cli:
        result = json_command([orca_cli, "worktree", "rm", "--worktree", f"id:{orca_worktree_id}",
                               "--force", "--json"])
        if result.get("ok") is False:
            raise WatcherError("Orca refused to remove the recorded worktree")
        return
    if not branch.startswith("repair/"):
        raise WatcherError("refusing to delete a non-repair branch")
    worktree = Path(worktree_value)
    if not worktree.is_absolute() or worktree.resolve() == repo.resolve():
        raise WatcherError("refusing to remove an unsafe worktree path")
    registered = worktree.resolve() in worktree_paths(repo)
    branch_exists = command(
        ["git", "-C", str(repo), "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"]
    ).returncode == 0
    if not registered and not branch_exists:
        return
    if not registered:
        raise WatcherError("recorded worktree is no longer registered; inspect it manually")
    if git(worktree, "branch", "--show-current") != branch:
        raise WatcherError("recorded worktree and branch do not match")
    if worktree_value:
        argv = ["git", "-C", str(repo), "worktree", "remove"]
        if force:
            argv.append("--force")
        result = command([*argv, str(worktree)])
        if result.returncode:
            raise WatcherError(result.stderr.strip() or "failed to remove worktree")
    if branch:
        if branch_exists:
            result = command(["git", "-C", str(repo), "branch", "-D" if force else "-d", branch])
            if result.returncode:
                raise WatcherError(result.stderr.strip() or "failed to delete repair branch")


def github_login() -> str:
    result = command(["gh", "api", "user", "--jq", ".login"])
    if result.returncode:
        raise WatcherError(result.stderr.strip() or "gh api user failed")
    return result.stdout.strip()


def repository_metadata(repository: dict[str, Any]) -> dict[str, Any]:
    result = command(["gh", "repo", "view", repository["repository"], "--json",
                      "id,nameWithOwner,isArchived,hasIssuesEnabled"])
    if result.returncode:
        raise WatcherError(result.stderr.strip() or f"failed to inspect {repository['repository']}")
    metadata = json.loads(result.stdout)
    if metadata.get("nameWithOwner", "").lower() != repository["repository"].lower():
        raise WatcherError(f"repository canonical name mismatch: {metadata.get('nameWithOwner')}")
    if metadata.get("isArchived") or not metadata.get("hasIssuesEnabled"):
        raise WatcherError(f"repository is archived or Issues are disabled: {repository['repository']}")
    if repository.get("repository_id") and repository["repository_id"] != metadata.get("id"):
        raise WatcherError(f"repository node ID mismatch: {repository['repository']}")
    return metadata


def list_issues(repository: dict[str, Any], login: str, created_after: dt.datetime | None = None,
                created_through: dt.datetime | None = None) -> list[dict[str, Any]]:
    repository_metadata(repository)
    slug = repository["repository"]
    author = login if repository["author"] == "@me" else repository["author"]
    result = command(["gh", "issue", "list", "--repo", slug, "--state", "open", "--limit", "1000",
                      "--author", author, "--json", "id,number,url,title,author,labels,createdAt,updatedAt"])
    if result.returncode:
        raise WatcherError(result.stderr.strip() or f"failed to list Issues for {slug}")
    values = json.loads(result.stdout)
    labels = set(repository.get("labels", []))
    activation = repository["activate_after"]
    lower = created_after or activation
    eligible = []
    for issue in values:
        created = parse_time(issue["createdAt"])
        actual_labels = {item.get("name") for item in issue.get("labels", [])}
        if ((issue.get("author") or {}).get("login", "").lower() == author.lower()
                and labels.issubset(actual_labels) and created > activation and created >= lower
                and (created_through is None or created <= created_through)):
            issue["repository"] = slug
            eligible.append(issue)
    return eligible


def poll(config: dict[str, Any], ledger: Ledger) -> dict[str, Any]:
    login, observed, enqueued = github_login(), 0, 0
    for repository in config["repositories"]:
        boundary = now()
        cursor = ledger.cursor(repository["repository"], repository["activate_after"])
        for issue in list_issues(repository, login, cursor, boundary):
            observed += 1
            enqueued += int(ledger.enqueue(issue))
        ledger.advance_cursor(repository["repository"], boundary)
    return {"observed": observed, "enqueued": enqueued}


def parse_receipt(output: str) -> dict[str, Any] | None:
    matches = RECEIPT.findall(output)
    for payload in reversed(matches):
        try:
            value = json.loads(payload)
        except json.JSONDecodeError:
            continue
        if (isinstance(value, dict) and value.get("status") in CHILD_STATES
                and isinstance(value.get("summary"), str)):
            return value
    return None


def invalid_receipt_summary(output: str) -> str:
    candidates = [line for line in output.splitlines() if "AUTOPILOT_RESULT" in line]
    if not candidates:
        return "Orca worker returned no valid AUTOPILOT_RESULT receipt"
    line = candidates[-1]
    if line.startswith("AUTOPILOT_RESULT "):
        return "invalid AUTOPILOT_RESULT receipt: missing required colon after the prefix"
    if line.startswith("AUTOPILOT_RESULT:"):
        if not line.startswith("AUTOPILOT_RESULT: "):
            return "invalid AUTOPILOT_RESULT receipt: expected the literal prefix `AUTOPILOT_RESULT: `"
        try:
            json.loads(line.removeprefix("AUTOPILOT_RESULT: "))
        except json.JSONDecodeError:
            return "invalid AUTOPILOT_RESULT receipt: payload is invalid JSON"
    return "invalid AUTOPILOT_RESULT receipt: expected one exact standalone receipt line"


def verified_receipt(receipt: dict[str, Any] | None, repository: dict[str, Any],
                     run: dict[str, Any], output: str = "") -> tuple[str, str, dict[str, Any]]:
    if receipt is None:
        return "needs-human", invalid_receipt_summary(output), {}
    status = "ready-for-review" if receipt["status"] in {"succeeded", "ready-for-review"} else receipt["status"]
    identity = {key: receipt.get(key) for key in ("run_id", "worktree_path", "branch")}
    expected = {key: run.get(key) for key in identity}
    if identity != expected:
        return "needs-human", "receipt does not match the coordinator-assigned run, worktree, and branch", {}
    fields = {"base_sha": receipt.get("base_sha"), "head_sha": receipt.get("head_sha")}
    if status == "ready-for-review":
        try:
            validate_attempt(repository["repo_path"], {
                **expected, **fields, "orca_worktree_id": run.get("orca_worktree_id")
            })
        except WatcherError as exc:
            return "needs-human", f"unverified success receipt: {exc}", fields
    return status, receipt["summary"], fields if status == "ready-for-review" else {}


def prompt_for(run: dict[str, Any], config: dict[str, Any]) -> str:
    return (
        f"Use $github-issue-repair {run['url']} in GitHub Issue Autopilot mode for attempt "
        f"{run['attempt_number']}. Use repair run ID {run['run_id']} and the current Orca-managed worktree and branch. "
        "The dispatcher verified this newly created open Issue against its repository, "
        "author, polling window, and labels. Implement one bounded local package at risk no greater than "
        f"{config['policy'].get('max_risk', 'medium')} in the isolated Orca worktree and verify it independently. "
        "Update the active Orca worktree comment at investigation, implementation, and test checkpoints. "
        "Stop on ambiguity, unsafe work, expansion, dependency upgrades, migrations, security/auth/payment changes, "
        "public API changes, destructive operations, or a broad diff. Never push, create a PR, merge, close, comment, "
        "label, release, or deploy. Commit only the isolated task changes. Finish with exactly one single-line receipt "
        "using the literal prefix `AUTOPILOT_RESULT: `. Example (replace the placeholders):\n"
        "AUTOPILOT_RESULT: "
        '{"status":"needs-human","summary":"explain why","run_id":"<assigned run ID>",'
        '"worktree_path":"<absolute worktree path>","branch":"<assigned branch>"}\n'
        "Then send the Orca worker_done required by the injected dispatch. Success uses ready-for-review and includes "
        "summary, run_id, absolute worktree_path, branch, base_sha, and head_sha. Other statuses are needs-human, "
        "blocked, or failed."
    )


def select_agent(issue: dict[str, Any], config: dict[str, Any]) -> str:
    labels = sorted({item.get("name", "") for item in issue.get("labels", [])
                     if isinstance(item, dict) and item.get("name", "").startswith("agent:")})
    if len(labels) > 1:
        raise WatcherError(f"multiple agent labels conflict: {', '.join(labels)}")
    agent = labels[0].split(":", 1)[1] if labels else config["orca"]["default_agent"]
    if not agent or agent not in config["orca"]["allowed_agents"]:
        raise WatcherError(f"agent is not allowed for this repository: {agent or '<empty>'}")
    return agent


def ensure_orca_run(config: dict[str, Any], ledger: Ledger) -> str:
    run_id = ledger.metadata("orca_run_id")
    if run_id:
        orca_call(config, "orchestration", "run-use", "--id", run_id)
        return run_id
    response = orca_call(config, "orchestration", "run-create", "--objective",
                         "Supervise eligible GitHub Issue Autopilot repair worktrees")
    run_id = nested_value(response, ("result", "run", "id"), ("result", "runId"), ("result", "id"))
    if not isinstance(run_id, str) or not run_id:
        raise WatcherError("Orca did not return an orchestration Run ID")
    ledger.set_metadata("orca_run_id", run_id)
    return run_id


def create_orca_task(config: dict[str, Any], run_id: str, run: dict[str, Any]) -> str:
    response = orca_call(
        config, "orchestration", "task-create", "--run", run_id,
        "--task-title", f"#{run['issue_number']} {run['title']}",
        "--display-name", f"#{run['issue_number']} {run['title']}",
        "--spec", prompt_for(run, config),
    )
    task_id = nested_value(response, ("result", "task", "id"), ("result", "taskId"), ("result", "id"))
    if not isinstance(task_id, str) or not task_id:
        raise WatcherError("Orca did not return a Task ID")
    return task_id


def create_blocked_worktree(config: dict[str, Any], repository: dict[str, Any], run: dict[str, Any],
                            summary: str) -> dict[str, Any]:
    response = orca_call(
        config, "worktree", "create", "--repo", f"path:{repository['repo_path']}",
        "--name", f"issue-{run['issue_number']}-attempt-{run['attempt_number']}",
        "--parent-worktree", f"path:{repository['repo_path']}", "--issue", str(run["issue_number"]),
        "--comment", f"Needs human: {summary}", "--setup", config["orca"]["setup"],
    )
    worktree = nested_value(response, ("result", "worktree"), ("result",))
    return worktree if isinstance(worktree, dict) else {}


def dispatch_one(config: dict[str, Any], ledger: Ledger, run_id: str, run: dict[str, Any]) -> dict[str, Any]:
    attempt = run["attempt_number"]
    repository = next((item for item in config["repositories"] if item["repository"] == run["repository"]), None)
    if repository is None:
        ledger.finish(run["node_id"], attempt, "blocked", summary="repository removed from policy")
        return {"url": run["url"], "status": "blocked"}
    repair_run_id = str(uuid.uuid4())
    run["run_id"] = repair_run_id
    try:
        historical_cutoff = dt.datetime.min.replace(tzinfo=dt.timezone.utc)
        current = {item["id"]: item for item in list_issues(
            repository, github_login(), created_after=historical_cutoff
        )}
        issue = current.get(run["node_id"])
        if issue is None:
            ledger.finish(run["node_id"], attempt, "blocked", summary="Issue is no longer open or eligible",
                          run_id=repair_run_id)
            return {"url": run["url"], "status": "blocked"}
        task_id = create_orca_task(config, run_id, run)
        agent = select_agent(issue, config)
        response = orca_call(
            config, "orchestration", "worker-start", "--run", run_id, "--task", task_id,
            "--worktree", "new-child", "--repo", f"path:{repository['repo_path']}",
            "--name", f"issue-{run['issue_number']}-attempt-{attempt}",
            "--display-name", f"#{run['issue_number']} {run['title']}",
            "--comment", f"Starting {agent}", "--agent", agent, "--setup", config["orca"]["setup"],
            "--timeout-ms", "60000",
        )
        dispatch_id = nested_value(response, ("result", "dispatch", "id"),
                                   ("result", "dispatchId"), ("result", "worker", "dispatchId"))
        worktree = nested_value(response, ("result", "worktree"), ("result", "worker", "worktree"),
                                ("result", "placement", "worktree"))
        if not isinstance(dispatch_id, str) or not dispatch_id or not isinstance(worktree, dict):
            raise WatcherError("Orca worker receipt omitted its Dispatch or worktree identity")
        worktree_id = worktree.get("id") or worktree.get("worktreeId")
        worktree_path = worktree.get("path") or worktree.get("worktreePath")
        branch = str(worktree.get("branch", "")).removeprefix("refs/heads/")
        if not all(isinstance(value, str) and value for value in (worktree_id, worktree_path, branch)):
            raise WatcherError("Orca worker receipt omitted worktree path or branch")
        orca_call(config, "worktree", "set", "--worktree", f"id:{worktree_id}",
                  "--issue", str(run["issue_number"]), "--workspace-status", "in-progress",
                  "--comment", f"{agent}: investigating")
        ledger.assign_orca(
            run["node_id"], attempt, run_id=repair_run_id, agent_id=agent,
            orca_task_id=task_id, orca_dispatch_id=dispatch_id, orca_worktree_id=worktree_id,
            worktree_path=worktree_path, branch=branch,
        )
        return {"url": run["url"], "attempt": attempt, "status": "running", "agent": agent,
                "orca_task_id": task_id, "orca_dispatch_id": dispatch_id,
                "orca_worktree_id": worktree_id}
    except WatcherError as exc:
        summary = str(exc)
        task_id = locals().get("task_id")
        residual_dispatch = (find_named_string(exc.response, {"dispatchId", "dispatch_id"})
                             if isinstance(exc, OrcaCommandError) else None)
        stalled = (find_named_string(exc.response, {"lastError", "last_error"})
                   if isinstance(exc, OrcaCommandError) else None) == "agent_prompt_stalled"
        blocked = find_worktree(exc.response) if isinstance(exc, OrcaCommandError) else {}
        if not blocked and isinstance(exc, OrcaCommandError):
            residual_worktree_id = find_worktree_id(exc.response)
            if residual_worktree_id:
                residual_path = Path(residual_worktree_id.split("::", 1)[1])
                if residual_path.is_dir():
                    blocked = {"id": residual_worktree_id, "path": str(residual_path),
                               "branch": git(residual_path, "branch", "--show-current")}
        if stalled and residual_dispatch and blocked and isinstance(task_id, str):
            worktree_id = blocked.get("id") or blocked.get("worktreeId")
            worktree_path = blocked.get("path") or blocked.get("worktreePath")
            branch = str(blocked.get("branch", "")).removeprefix("refs/heads/")
            if all(isinstance(value, str) and value
                   for value in (worktree_id, worktree_path, branch)):
                ledger.assign_orca(
                    run["node_id"], attempt, run_id=repair_run_id, agent_id=agent,
                    orca_task_id=task_id, orca_dispatch_id=residual_dispatch,
                    orca_worktree_id=worktree_id, worktree_path=worktree_path, branch=branch,
                )
                return {"url": run["url"], "attempt": attempt, "status": "running",
                        "agent": agent, "orca_task_id": task_id,
                        "orca_dispatch_id": residual_dispatch, "orca_worktree_id": worktree_id,
                        "warning": "Orca readiness timed out after the Agent received its prompt"}
        if not blocked:
            try:
                blocked = create_blocked_worktree(config, repository, run, summary)
            except WatcherError:
                blocked = {}
        worktree_id = blocked.get("id") or blocked.get("worktreeId")
        fields = {"summary": summary, "run_id": repair_run_id}
        if isinstance(task_id, str):
            fields["orca_task_id"] = task_id
        if residual_dispatch:
            fields["orca_dispatch_id"] = residual_dispatch
        if isinstance(worktree_id, str):
            fields["orca_worktree_id"] = worktree_id
            fields["worktree_path"] = blocked.get("path") or blocked.get("worktreePath")
            fields["branch"] = str(blocked.get("branch", "")).removeprefix("refs/heads/")
        ledger.finish(run["node_id"], attempt, "needs-human", **fields)
        return {"url": run["url"], "attempt": attempt, "status": "needs-human", "summary": summary,
                "orca_worktree_id": worktree_id}


def worker_state(value: dict[str, Any]) -> str:
    states = set(named_values(value, {"outcome", "status", "state", "taskStatus", "dispatchStatus"}))
    if states.intersection({"failed", "stopped", "blocked", "cancelled"}):
        return "failed"
    if states.intersection({"succeeded", "completed", "settled"}):
        return "completed"
    return "running"


def reconcile_workers(config: dict[str, Any], ledger: Ledger) -> list[dict[str, Any]]:
    completed: list[dict[str, Any]] = []
    for attempt in ledger.active_attempts():
        dispatch_id = attempt.get("orca_dispatch_id")
        if not dispatch_id:
            continue
        try:
            show = orca_call(config, "orchestration", "worker-show", "--dispatch", dispatch_id)
        except WatcherError:
            continue
        state = worker_state(show)
        if state == "running":
            continue
        receipt = None
        receipt_output = ""
        if (state == "failed" and
                find_named_string(show, {"lastError", "last_error"}) == "agent_prompt_stalled"):
            try:
                output = orca_call(config, "orchestration", "worker-read", "--dispatch", dispatch_id,
                                   "--source", "auto", "--limit", "200")
                receipt_output = string_content(output)
                receipt = parse_receipt(receipt_output)
            except WatcherError:
                continue
            if receipt is None:
                if not any(line.startswith("AUTOPILOT_RESULT")
                           for line in receipt_output.splitlines()):
                    continue
            state = "completed"
        status, summary, evidence = "failed", "Orca worker failed or stopped", {}
        if state == "completed":
            if receipt is None and not receipt_output:
                output = orca_call(config, "orchestration", "worker-read", "--dispatch", dispatch_id,
                                   "--source", "auto", "--limit", "200")
                receipt_output = string_content(output)
                receipt = parse_receipt(receipt_output)
            repository = next(item for item in config["repositories"]
                              if item["repository"] == attempt["repository"])
            status, summary, evidence = verified_receipt(receipt, repository, attempt, output=receipt_output)
        ledger.finish(attempt["node_id"], attempt["attempt_number"], status, summary=summary, **evidence)
        if attempt.get("orca_worktree_id"):
            orca_call(config, "worktree", "set", "--worktree", f"id:{attempt['orca_worktree_id']}",
                      "--workspace-status", "in-review", "--comment", f"{status}: {summary[:180]}")
        orca_call(config, "orchestration", "worker-release", "--dispatch", dispatch_id)
        completed.append({"url": attempt["url"], "status": status, "summary": summary})
    return completed


def work_available(config: dict[str, Any], ledger: Ledger, run_id: str) -> list[dict[str, Any]]:
    return [dispatch_one(config, ledger, run_id, run)
            for run in ledger.claim_many(config["max_concurrent_workers"])]


def doctor(config: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {"gh": shutil.which("gh") is not None,
        "orca": Path(config["orca"]["cli"]).is_file() and os.access(config["orca"]["cli"], os.X_OK),
        "repositories": []}
    result["gh_auth"] = result["gh"] and command(
        ["gh", "api", "user", "--silent"]
    ).returncode == 0
    result["orca_runtime"] = False
    if result["orca"]:
        try:
            status = orca_call(config, "status")
            result["orca_runtime"] = nested_value(status, ("result", "runtime", "state")) == "ready"
        except WatcherError:
            pass
    for item in config["repositories"]:
        root = command(["git", "-C", str(item["repo_path"]), "rev-parse", "--show-toplevel"])
        check = {"repository": item["repository"], "repo_path": str(item["repo_path"]),
                 "git_root": root.returncode == 0 and Path(root.stdout.strip()).resolve() == item["repo_path"]}
        try:
            repository_metadata(item)
            check["github"] = True
        except WatcherError as exc:
            check["github"], check["error"] = False, str(exc)
        result["repositories"].append(check)
    result["ok"] = bool(result["gh"] and result["gh_auth"] and result["orca"] and result["orca_runtime"]
                        and all(item["git_root"] and item["github"] for item in result["repositories"]))
    return result


def repository_for_issue(config: dict[str, Any], url: str) -> dict[str, Any]:
    repository = next((item for item in config["repositories"]
                       if url.startswith(f"https://github.com/{item['repository']}/issues/")), None)
    if repository is None:
        raise WatcherError("Issue URL does not belong to a configured repository")
    return repository


def parser() -> argparse.ArgumentParser:
    main = argparse.ArgumentParser(description=__doc__)
    commands = main.add_subparsers(dest="command_name", required=True)
    for name in ("doctor", "poll", "work", "once", "run", "status", "retry", "accept"):
        sub = commands.add_parser(name)
        sub.add_argument("--config", type=Path, required=True)
        if name in {"retry", "accept"}:
            sub.add_argument("--issue-url", required=True)
        if name == "retry":
            sub.add_argument("--discard-worktree", action="store_true")
        if name == "accept":
            sub.add_argument("--target-branch", required=True)
    return main


def main() -> int:
    args = parser().parse_args()
    try:
        config = load_config(args.config)
        ledger = Ledger(config["state_db"], config["lease_timeout_seconds"], int(config.get("max_attempts", 2)))
        if args.command_name == "doctor":
            result, code = doctor(config), 0
            code = 0 if result["ok"] else 2
        elif args.command_name == "status":
            result, code = ledger.snapshot(), 0
        elif args.command_name == "retry":
            repository = repository_for_issue(config, args.issue_url)
            number = ledger.retry(args.issue_url, repository["repo_path"], args.discard_worktree,
                                  config["orca"]["cli"])
            result, code = {"status": "retry-pending", "url": args.issue_url, "next_attempt": number}, 0
        elif args.command_name == "accept":
            repository = repository_for_issue(config, args.issue_url)
            result, code = ledger.accept(args.issue_url, repository["repo_path"], args.target_branch,
                                         config["orca"]["cli"]), 0
        elif args.command_name == "poll":
            result, code = poll(config, ledger), 0
        elif args.command_name == "work":
            run_id = ensure_orca_run(config, ledger)
            result, code = {"completed": reconcile_workers(config, ledger),
                            "started": work_available(config, ledger, run_id)}, 0
        elif args.command_name == "once":
            run_id = ensure_orca_run(config, ledger)
            result, code = {"detection": poll(config, ledger),
                            "completed": reconcile_workers(config, ledger)}, 0
            result["started"] = work_available(config, ledger, run_id)
        else:
            run_id = None
            while True:
                try:
                    run_id = run_id or ensure_orca_run(config, ledger)
                    value = {"detection": poll(config, ledger),
                             "completed": reconcile_workers(config, ledger)}
                    value["started"] = work_available(config, ledger, run_id)
                    print(json.dumps(value, ensure_ascii=False), flush=True)
                except (WatcherError, OSError, ValueError, json.JSONDecodeError, sqlite3.Error) as exc:
                    print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False),
                          file=sys.stderr, flush=True)
                time.sleep(config["poll_interval_seconds"])
    except (WatcherError, OSError, ValueError, json.JSONDecodeError, sqlite3.Error) as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
