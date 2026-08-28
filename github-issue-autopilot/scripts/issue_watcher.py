#!/usr/bin/env python3
"""Poll new GitHub Issues and dispatch one isolated repair attempt at a time."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

CONFIG_VERSION = 1
LEDGER_VERSION = 2
REPOSITORY = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
RECEIPT = re.compile(r"AUTOPILOT_RESULT:\s*(\{[^\r\n]+\})")
CHILD_STATES = {"succeeded", "ready-for-review", "needs-human", "blocked", "failed"}
STOPPED_STATES = {"ready-for-review", "needs-human", "blocked", "failed"}


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
    if config.get("schema_version") != CONFIG_VERSION:
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
    executor["timeout_seconds"] = timeout
    config["lease_timeout_seconds"] = lease
    config["poll_interval_seconds"] = max(1, int(config.get("poll_interval_seconds", 180)))
    config["max_dispatch_per_poll"] = 1
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
              summary TEXT, returncode INTEGER, log_path TEXT,
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

    def claim_next(self, at: dt.datetime | None = None) -> dict[str, Any] | None:
        current = at or now()
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            for row in self.connection.execute(
                "SELECT * FROM issues WHERE status IN ('claimed', 'running') ORDER BY created_at"
            ).fetchall():
                local_live = row["lease_owner"] == socket.gethostname() and pid_alive(int(row["pid"] or 0))
                lease_live = bool(row["lease_started_at"]) and (
                    current - parse_time(row["lease_started_at"])
                ).total_seconds() <= self.lease_seconds
                if local_live or lease_live:
                    self.connection.execute("COMMIT")
                    return None
                self._finish_stale(row)
            chosen = self.connection.execute(
                """SELECT * FROM issues WHERE status IN ('queued', 'retry-pending')
                   ORDER BY created_at, issue_number LIMIT 1"""
            ).fetchone()
            if chosen is None:
                self.connection.execute("COMMIT")
                return None
            attempt = int(self.connection.execute(
                "SELECT COALESCE(MAX(attempt_number), 0) + 1 FROM attempts WHERE node_id=?",
                (chosen["node_id"],),
            ).fetchone()[0])
            started, owner = iso(current), socket.gethostname()
            self.connection.execute(
                """UPDATE issues SET status='claimed', attempts=?, lease_started_at=?, lease_owner=?,
                   pid=NULL, summary=NULL, returncode=NULL, log_path=NULL WHERE node_id=?""",
                (attempt, started, owner, chosen["node_id"]),
            )
            self.connection.execute(
                "INSERT INTO attempts(node_id, attempt_number, status, lease_started_at, lease_owner) "
                "VALUES (?, ?, 'claimed', ?, ?)", (chosen["node_id"], attempt, started, owner),
            )
            self._event(chosen["node_id"], "claimed", {"attempt": attempt})
            self.connection.execute("COMMIT")
            result = dict(self.connection.execute("SELECT * FROM issues WHERE node_id=?",
                                                  (chosen["node_id"],)).fetchone())
            result["attempt_number"] = attempt
            return result
        except Exception:
            self.connection.execute("ROLLBACK")
            raise

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

    def finish(self, node_id: str, attempt: int, status: str, **fields: Any) -> None:
        if status == "succeeded":
            status = "ready-for-review"
        if status not in STOPPED_STATES:
            raise WatcherError(f"invalid result status: {status}")
        fields["pid"] = None
        self._update(node_id, attempt, status, fields, "finished")

    def _update(self, node_id: str, attempt: int, status: str, fields: dict[str, Any], kind: str) -> None:
        allowed = {"pid", "run_id", "worktree_path", "branch", "base_sha", "head_sha",
                   "summary", "returncode", "log_path"}
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

    def retry(self, url: str, repo: Path, discard: bool) -> int:
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
        if discard:
            cleanup_attempt(repo, attempt, force=True)
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

    def accept(self, url: str, repo: Path, target_branch: str) -> dict[str, str]:
        issue = self.connection.execute("SELECT * FROM issues WHERE url=?", (url,)).fetchone()
        if issue is None or issue["status"] != "ready-for-review":
            raise WatcherError("Issue must be ready-for-review before acceptance")
        attempt = dict(self.connection.execute(
            "SELECT * FROM attempts WHERE node_id=? AND attempt_number=?", (issue["node_id"], issue["attempts"])
        ).fetchone())
        validate_attempt(repo, attempt)
        if git(repo, "branch", "--show-current") != target_branch:
            raise WatcherError(f"target branch {target_branch} is not checked out")
        if git(repo, "status", "--porcelain"):
            raise WatcherError("target worktree must be clean before merging")
        result = command(["git", "-C", str(repo), "merge", "--no-ff", "--no-edit", attempt["head_sha"]])
        if result.returncode:
            raise WatcherError(result.stderr.strip() or "local merge failed")
        self._update(issue["node_id"], issue["attempts"], "accepted", {"summary": "merged locally"}, "accepted")
        response = {"status": "accepted", "target_branch": target_branch, "head": git(repo, "rev-parse", "HEAD")}
        try:
            cleanup_attempt(repo, attempt, force=False)
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
    return subprocess.run(argv, cwd=cwd, check=False, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


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
    if not attempt["branch"].startswith("repair/") or git(worktree, "branch", "--show-current") != attempt["branch"]:
        raise WatcherError("receipt branch does not match a repair worktree")
    if git(worktree, "rev-parse", "HEAD") != attempt["head_sha"]:
        raise WatcherError("receipt head SHA does not match the worktree")
    if git(repo, "rev-parse", f"{attempt['base_sha']}^{{commit}}") != attempt["base_sha"]:
        raise WatcherError("receipt base SHA is invalid")
    state = repair_run_state(repo, attempt["run_id"])
    if (state.get("base_sha") != attempt["base_sha"]
            or state.get("state") not in {"REVIEW", "AWAIT_PUBLICATION_APPROVAL"}):
        raise WatcherError("repair run ledger does not confirm a reviewed local result")


def repair_run_state(repo: Path, run_id: str) -> dict[str, Any]:
    try:
        uuid.UUID(run_id)
    except ValueError as exc:
        raise WatcherError("receipt run ID is not a UUID") from exc
    common = Path(git(repo, "rev-parse", "--git-common-dir"))
    common = common if common.is_absolute() else repo / common
    state_path = common.resolve() / "issue-repair" / "runs" / run_id / "state.json"
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        raise WatcherError("repair run ledger is missing or invalid") from exc
    if state.get("run_id") != run_id or Path(state.get("repository", "")).resolve() != repo.resolve():
        raise WatcherError("repair run ledger belongs to a different run or repository")
    return state


def cleanup_attempt(repo: Path, attempt: dict[str, Any], force: bool) -> None:
    worktree_value, branch = attempt.get("worktree_path"), attempt.get("branch")
    if not worktree_value and not branch:
        return
    if not worktree_value or not branch or not attempt.get("run_id"):
        raise WatcherError("recorded attempt lacks a complete cleanup identity")
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


def signal_process_group(process: subprocess.Popen[str], number: int) -> None:
    try:
        os.killpg(process.pid, number)
    except ProcessLookupError:
        pass


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
    lower = created_after or repository["activate_after"]
    eligible = []
    for issue in values:
        created = parse_time(issue["createdAt"])
        actual_labels = {item.get("name") for item in issue.get("labels", [])}
        if ((issue.get("author") or {}).get("login", "").lower() == author.lower()
                and labels.issubset(actual_labels) and created > lower
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
    if not matches:
        return None
    try:
        value = json.loads(matches[-1])
    except json.JSONDecodeError:
        return None
    return value if value.get("status") in CHILD_STATES and isinstance(value.get("summary"), str) else None


def verified_receipt(receipt: dict[str, Any] | None, repository: dict[str, Any], run: dict[str, Any]) -> tuple[str, str, dict[str, Any]]:
    if receipt is None:
        return "needs-human", "executor returned no valid AUTOPILOT_RESULT receipt", {}
    status = "ready-for-review" if receipt["status"] in {"succeeded", "ready-for-review"} else receipt["status"]
    identity = {key: receipt.get(key) for key in ("run_id", "worktree_path", "branch")}
    expected = {key: run.get(key) for key in identity}
    if identity != expected:
        return "needs-human", "receipt does not match the coordinator-assigned run, worktree, and branch", {}
    fields = {"base_sha": receipt.get("base_sha"), "head_sha": receipt.get("head_sha")}
    if status == "ready-for-review":
        try:
            validate_attempt(repository["repo_path"], {**expected, **fields})
        except WatcherError as exc:
            return "needs-human", f"unverified success receipt: {exc}", fields
    return status, receipt["summary"], fields if status == "ready-for-review" else {}


def prompt_for(run: dict[str, Any], config: dict[str, Any]) -> str:
    return (
        f"Use $github-issue-repair {run['url']} in GitHub Issue Autopilot mode for attempt "
        f"{run['attempt_number']}. Use repair run ID {run['run_id']}, branch {run['branch']}, and the exact absolute "
        f"worktree path {run['worktree_path']}. The dispatcher verified this newly created open Issue against its repository, "
        "author, polling window, and labels. Implement one bounded local package at risk no greater than "
        f"{config['policy'].get('max_risk', 'medium')} in an isolated repair/ worktree and verify it independently. "
        "Stop on ambiguity, unsafe work, expansion, dependency upgrades, migrations, security/auth/payment changes, "
        "public API changes, destructive operations, or a broad diff. Never push, create a PR, merge, close, comment, "
        "label, release, or deploy. Finish with one AUTOPILOT_RESULT JSON line. Success uses ready-for-review and "
        "includes summary, run_id, absolute worktree_path, branch, base_sha, and head_sha. Other statuses are "
        "needs-human, blocked, or failed."
    )


def expanded_argv(config: dict[str, Any], repository: dict[str, Any], run: dict[str, Any]) -> list[str]:
    values = {"repository": repository["repository"], "repo_path": str(repository["repo_path"]),
              "issue_url": run["url"], "issue_number": str(run["issue_number"]),
              "attempt_number": str(run["attempt_number"])}
    expanded = []
    for original in config["executor"]["argv"]:
        argument = original
        for name, value in values.items():
            argument = argument.replace("{" + name + "}", value)
        expanded.append(argument)
    return expanded


def work_once(config: dict[str, Any], ledger: Ledger) -> dict[str, Any] | None:
    run = ledger.claim_next()
    if run is None:
        return None
    attempt = run["attempt_number"]
    repository = next((item for item in config["repositories"] if item["repository"] == run["repository"]), None)
    if repository is None:
        ledger.finish(run["node_id"], attempt, "blocked", summary="repository removed from policy")
        return {"url": run["url"], "status": "blocked"}
    current = {item["id"]: item for item in list_issues(repository, github_login())}
    if run["node_id"] not in current:
        ledger.finish(run["node_id"], attempt, "blocked", summary="Issue is no longer open or eligible")
        return {"url": run["url"], "status": "blocked"}
    run_id = str(uuid.uuid4())
    branch = f"repair/issue-{run['issue_number']}-attempt-{attempt}"
    slug = run["repository"].replace("/", "-")
    worktree = (config["state_db"].parent / "worktrees" /
                f"{slug}-issue-{run['issue_number']}-attempt-{attempt}").resolve()
    if worktree.exists() or command(
        ["git", "-C", str(repository["repo_path"]), "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"]
    ).returncode == 0:
        summary = "coordinator-assigned worktree or branch already exists; inspect it before retrying"
        ledger.finish(run["node_id"], attempt, "needs-human", summary=summary)
        return {"url": run["url"], "attempt": attempt, "status": "needs-human", "summary": summary}
    ledger.assign_artifacts(run["node_id"], attempt, run_id, worktree, branch)
    run.update({"run_id": run_id, "worktree_path": str(worktree), "branch": branch})
    logs = config["state_db"].parent / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    log_path = logs / f"{run['repository'].replace('/', '-')}-{run['issue_number']}-attempt-{attempt}.log"
    try:
        process = subprocess.Popen(expanded_argv(config, repository, run), stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                   cwd=repository["repo_path"], start_new_session=True)
    except OSError as exc:
        ledger.finish(run["node_id"], attempt, "failed", summary=str(exc), log_path=str(log_path))
        return {"url": run["url"], "status": "failed", "summary": str(exc)}
    ledger.running(run["node_id"], attempt, process.pid)
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
        ledger.finish(run["node_id"], attempt, "failed", summary="executor timed out", log_path=str(log_path))
        return {"url": run["url"], "status": "failed", "summary": "executor timed out"}
    log_path.write_text(output, encoding="utf-8")
    status, summary, evidence = verified_receipt(parse_receipt(output), repository, run)
    if process.returncode:
        status, summary = "failed", f"executor exited {process.returncode}; {summary}"
    ledger.finish(run["node_id"], attempt, status, summary=summary, returncode=process.returncode,
                  log_path=str(log_path), **evidence)
    return {"url": run["url"], "attempt": attempt, "status": status, "summary": summary,
            "log": str(log_path), **evidence}


def doctor(config: dict[str, Any]) -> dict[str, Any]:
    executable = config["executor"]["argv"][0]
    result: dict[str, Any] = {"gh": shutil.which("gh") is not None,
        "executor": shutil.which(executable) is not None if "/" not in executable else Path(executable).is_file(),
        "repositories": []}
    result["gh_auth"] = result["gh"] and command(["gh", "auth", "status"]).returncode == 0
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
    result["ok"] = bool(result["gh"] and result["gh_auth"] and result["executor"]
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
            number = ledger.retry(args.issue_url, repository["repo_path"], args.discard_worktree)
            result, code = {"status": "retry-pending", "url": args.issue_url, "next_attempt": number}, 0
        elif args.command_name == "accept":
            repository = repository_for_issue(config, args.issue_url)
            result, code = ledger.accept(args.issue_url, repository["repo_path"], args.target_branch), 0
        elif args.command_name == "poll":
            result, code = poll(config, ledger), 0
        elif args.command_name == "work":
            result, code = {"result": work_once(config, ledger)}, 0
        elif args.command_name == "once":
            result, code = {"detection": poll(config, ledger), "completed": []}, 0
            completed = work_once(config, ledger)
            if completed is not None:
                result["completed"].append(completed)
        else:
            while True:
                print(json.dumps({"detection": poll(config, ledger), "result": work_once(config, ledger)},
                                 ensure_ascii=False), flush=True)
                time.sleep(config["poll_interval_seconds"])
    except (WatcherError, OSError, ValueError, json.JSONDecodeError, sqlite3.Error) as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
