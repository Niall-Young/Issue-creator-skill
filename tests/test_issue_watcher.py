from __future__ import annotations

import datetime as dt
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "github-issue-autopilot" / "scripts" / "issue_watcher.py"
SPEC = importlib.util.spec_from_file_location("issue_watcher", SCRIPT)
WATCHER = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(WATCHER)


class IssueWatcherTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        subprocess.run(["git", "-C", str(self.repo), "init", "-b", "main"], check=True, stdout=subprocess.PIPE)
        subprocess.run(["git", "-C", str(self.repo), "config", "user.name", "Test"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "config", "user.email", "test@example.com"], check=True)
        subprocess.run(
            ["git", "-C", str(self.repo), "commit", "--allow-empty", "-m", "baseline"],
            check=True, stdout=subprocess.PIPE,
        )
        self.db = self.root / "state.sqlite3"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    @staticmethod
    def issue(node_id: str = "I_kwDO1", number: int = 1) -> dict:
        return {
            "id": node_id,
            "repository": "owner/repo",
            "number": number,
            "url": f"https://github.com/owner/repo/issues/{number}",
            "title": "Repair the thing",
            "createdAt": "2026-08-27T01:00:00Z",
            "updatedAt": "2026-08-27T01:01:00Z",
        }

    def ledger(self, attempts: int = 2) -> object:
        return WATCHER.Ledger(self.db, lease_seconds=20, max_attempts=attempts)

    def repair_evidence(self, worktree: Path, branch: str, base: str, head: str) -> dict:
        run_id = str(uuid.uuid4())
        common = subprocess.run(
            ["git", "-C", str(self.repo), "rev-parse", "--git-common-dir"], check=True, text=True,
            stdout=subprocess.PIPE,
        ).stdout.strip()
        common_path = Path(common) if Path(common).is_absolute() else self.repo / common
        state = common_path.resolve() / "issue-repair" / "runs" / run_id / "state.json"
        state.parent.mkdir(parents=True)
        state.write_text(json.dumps({
            "run_id": run_id, "repository": str(self.repo.resolve()), "base_sha": base,
            "state": "AWAIT_PUBLICATION_APPROVAL",
        }), encoding="utf-8")
        return {"run_id": run_id, "worktree_path": str(worktree), "branch": branch,
                "base_sha": base, "head_sha": head}

    def config(self) -> dict:
        return {
            "schema_version": 1,
            "state_db": self.db,
            "lease_timeout_seconds": 20,
            "max_attempts": 2,
            "max_dispatch_per_poll": 1,
            "policy": {"scope_approval": "eligible-issue", "publication": "never", "max_risk": "medium"},
            "repositories": [{
                "repository": "owner/repo",
                "repo_path": self.repo,
                "author": "@me",
                "labels": [],
                "activate_after": dt.datetime(2026, 8, 27, tzinfo=dt.timezone.utc),
            }],
            "executor": {
                "timeout_seconds": 10,
                "argv": [
                    sys.executable,
                    "-c",
                    "import sys; sys.stdin.read(); print('AUTOPILOT_RESULT: {\"status\":\"succeeded\",\"summary\":\"verified\"}')",
                ],
            },
        }

    def test_node_id_is_idempotent_and_edits_do_not_retrigger(self) -> None:
        ledger = self.ledger()
        issue = self.issue()
        self.assertTrue(ledger.enqueue(issue))
        edited = dict(issue, title="Edited title", updatedAt="2026-08-27T02:00:00Z")
        self.assertFalse(ledger.enqueue(edited))
        first = ledger.claim_next()
        self.assertIsNotNone(first)
        ledger.finish(first["node_id"], first["attempt_number"], "succeeded", summary="done")
        self.assertIsNone(ledger.claim_next())
        self.assertEqual("ready-for-review", ledger.snapshot()["issues"][0]["status"])

    def test_overlapping_claims_only_return_one_run(self) -> None:
        first = self.ledger()
        second = self.ledger()
        first.enqueue(self.issue())
        first.enqueue(self.issue("I_kwDO2", 2))
        self.assertIsNotNone(first.claim_next())
        self.assertIsNone(second.claim_next())
        claimed = first.snapshot()["issues"][0]
        first.finish(claimed["node_id"], claimed["attempts"], "succeeded", summary="done")
        self.assertEqual(2, second.claim_next()["issue_number"])

    def test_stale_dead_lease_stops_for_human_instead_of_relaunching(self) -> None:
        ledger = self.ledger(attempts=2)
        ledger.enqueue(self.issue())
        original = dt.datetime(2026, 8, 27, 1, 0, tzinfo=dt.timezone.utc)
        first = ledger.claim_next(original)
        self.assertEqual(1, first["attempt_number"])
        self.assertIsNone(ledger.claim_next(original + dt.timedelta(seconds=21)))
        snapshot = ledger.snapshot()["issues"][0]
        self.assertEqual("needs-human", snapshot["status"])
        self.assertEqual(1, len(snapshot["attempt_history"]))

    def test_issue_filters_apply_author_label_and_activation_time(self) -> None:
        repository = self.config()["repositories"][0]
        repository["labels"] = ["agent-ready"]
        values = [
            {**self.issue("new", 1), "author": {"login": "owner"}, "labels": [{"name": "agent-ready"}]},
            {**self.issue("old", 2), "createdAt": "2026-08-26T23:59:00Z", "author": {"login": "owner"}, "labels": [{"name": "agent-ready"}]},
            {**self.issue("other", 3), "author": {"login": "someone"}, "labels": [{"name": "agent-ready"}]},
            {**self.issue("unlabeled", 4), "author": {"login": "owner"}, "labels": []},
        ]
        metadata = {"id": "R_1", "nameWithOwner": "owner/repo", "isArchived": False, "hasIssuesEnabled": True}
        calls = [
            subprocess.CompletedProcess([], 0, json.dumps(metadata), ""),
            subprocess.CompletedProcess([], 0, json.dumps(values), ""),
        ]
        with mock.patch.object(WATCHER, "command", side_effect=calls):
            eligible = WATCHER.list_issues(repository, "owner")
        self.assertEqual(["new"], [issue["id"] for issue in eligible])

    def test_work_once_requires_a_valid_receipt_for_success(self) -> None:
        config = self.config()
        ledger = self.ledger()
        issue = self.issue()
        ledger.enqueue(issue)
        eligible = dict(issue, author={"login": "owner"}, labels=[])
        with mock.patch.object(WATCHER, "github_login", return_value="owner"), mock.patch.object(
            WATCHER, "list_issues", return_value=[eligible]
        ), mock.patch.object(
            WATCHER, "verified_receipt", return_value=("ready-for-review", "verified", {})
        ):
            result = WATCHER.work_once(config, ledger)
        self.assertEqual("ready-for-review", result["status"])
        self.assertEqual("ready-for-review", ledger.snapshot()["issues"][0]["status"])

    def test_nonzero_executor_exit_cannot_be_reported_as_success(self) -> None:
        config = self.config()
        config["executor"]["argv"] = [
            sys.executable,
            "-c",
            "print('AUTOPILOT_RESULT: {\"status\":\"succeeded\",\"summary\":\"claimed success\"}'); raise SystemExit(3)",
        ]
        ledger = self.ledger()
        issue = self.issue()
        ledger.enqueue(issue)
        eligible = dict(issue, author={"login": "owner"}, labels=[])
        with mock.patch.object(WATCHER, "github_login", return_value="owner"), mock.patch.object(
            WATCHER, "list_issues", return_value=[eligible]
        ):
            result = WATCHER.work_once(config, ledger)
        self.assertEqual("failed", result["status"])
        self.assertIn("exited 3", result["summary"])

    def test_load_config_rejects_missing_activation_cutoff(self) -> None:
        path = self.root / "config.json"
        path.write_text(json.dumps({
            "schema_version": 1,
            "state_db": str(self.db),
            "policy": {"scope_approval": "eligible-issue", "publication": "never"},
            "repositories": [{"repository": "owner/repo", "repo_path": str(self.repo), "author": "@me"}],
            "executor": {"timeout_seconds": 10, "argv": [sys.executable]},
            "lease_timeout_seconds": 20,
        }), encoding="utf-8")
        with self.assertRaisesRegex(WATCHER.WatcherError, "activate_after"):
            WATCHER.load_config(path)

    def doctor_with_executor(self, executable: str) -> dict:
        config = self.config()
        config["executor"]["argv"][0] = executable
        config["repositories"][0]["repo_path"] = self.repo.resolve()
        healthy = subprocess.CompletedProcess([], 0, str(self.repo.resolve()), "")
        with mock.patch.object(WATCHER.shutil, "which", return_value="/usr/bin/gh"), mock.patch.object(
            WATCHER, "command", return_value=healthy
        ), mock.patch.object(WATCHER, "repository_metadata"):
            return WATCHER.doctor(config)

    def test_doctor_accepts_executable_absolute_executor(self) -> None:
        executable = self.root / "executor"
        executable.write_text("#!/bin/sh\n", encoding="utf-8")
        executable.chmod(0o700)
        result = self.doctor_with_executor(str(executable))
        self.assertTrue(result["executor"])
        self.assertTrue(result["ok"])

    def test_doctor_rejects_non_executable_absolute_executor(self) -> None:
        executable = self.root / "executor"
        executable.write_text("#!/bin/sh\n", encoding="utf-8")
        executable.chmod(0o600)
        result = self.doctor_with_executor(str(executable))
        self.assertFalse(result["executor"])
        self.assertFalse(result["ok"])

    def test_doctor_rejects_missing_absolute_executor(self) -> None:
        result = self.doctor_with_executor(str(self.root / "missing-executor"))
        self.assertFalse(result["executor"])
        self.assertFalse(result["ok"])

    def test_doctor_keeps_path_lookup_for_bare_executor_command(self) -> None:
        config = self.config()
        config["executor"]["argv"][0] = "agent-cli"
        config["repositories"][0]["repo_path"] = self.repo.resolve()
        healthy = subprocess.CompletedProcess([], 0, str(self.repo.resolve()), "")
        with mock.patch.object(
            WATCHER.shutil, "which", side_effect=lambda value: f"/usr/bin/{value}"
        ) as which, mock.patch.object(WATCHER, "command", return_value=healthy), mock.patch.object(
            WATCHER, "repository_metadata"
        ):
            result = WATCHER.doctor(config)
        self.assertTrue(result["executor"])
        which.assert_any_call("agent-cli")

    def test_success_receipt_must_resolve_to_registered_repair_worktree(self) -> None:
        worktree = self.root / "repair-worktree"
        subprocess.run(
            ["git", "-C", str(self.repo), "worktree", "add", "-b", "repair/issue-1-attempt-1",
             str(worktree), "HEAD"], check=True, stdout=subprocess.PIPE,
        )
        sha = subprocess.run(
            ["git", "-C", str(worktree), "rev-parse", "HEAD"], check=True, text=True,
            stdout=subprocess.PIPE,
        ).stdout.strip()
        evidence = self.repair_evidence(worktree, "repair/issue-1-attempt-1", sha, sha)
        WATCHER.validate_attempt(self.repo, evidence)
        invalid = dict(evidence, branch="main")
        with self.assertRaisesRegex(WATCHER.WatcherError, "repair worktree"):
            WATCHER.validate_attempt(self.repo, invalid)

    def test_receipt_identity_must_match_coordinator_assignment(self) -> None:
        repository = {"repo_path": self.repo}
        run = {"run_id": "assigned", "worktree_path": "/assigned", "branch": "repair/assigned"}
        receipt = {"status": "ready-for-review", "summary": "done", "run_id": "other",
                   "worktree_path": "/assigned", "branch": "repair/assigned",
                   "base_sha": "base", "head_sha": "head"}
        status, summary, evidence = WATCHER.verified_receipt(receipt, repository, run)
        self.assertEqual("needs-human", status)
        self.assertIn("coordinator-assigned", summary)
        self.assertEqual({}, evidence)

    def test_explicit_discard_rejects_old_attempt_and_creates_new_number(self) -> None:
        ledger = self.ledger()
        ledger.enqueue(self.issue())
        claimed = ledger.claim_next()
        worktree = self.root / "discard-me"
        branch = "repair/issue-1-attempt-1"
        subprocess.run(
            ["git", "-C", str(self.repo), "worktree", "add", "-b", branch, str(worktree), "HEAD"],
            check=True, stdout=subprocess.PIPE,
        )
        sha = subprocess.run(
            ["git", "-C", str(worktree), "rev-parse", "HEAD"], check=True, text=True,
            stdout=subprocess.PIPE,
        ).stdout.strip()
        evidence = self.repair_evidence(worktree, branch, sha, sha)
        ledger.finish(claimed["node_id"], claimed["attempt_number"], "ready-for-review",
                      summary="review me", **evidence)
        self.assertEqual(2, ledger.retry(self.issue()["url"], self.repo, True))
        self.assertFalse(worktree.exists())
        retried = ledger.claim_next()
        self.assertEqual(2, retried["attempt_number"])
        history = ledger.snapshot()["issues"][0]["attempt_history"]
        self.assertEqual("rejected", history[0]["status"])

    def test_discard_refuses_receipt_branch_that_does_not_match_worktree(self) -> None:
        ledger = self.ledger()
        ledger.enqueue(self.issue())
        claimed = ledger.claim_next()
        worktree = self.root / "keep-me"
        branch = "repair/actual"
        subprocess.run(
            ["git", "-C", str(self.repo), "worktree", "add", "-b", branch, str(worktree), "HEAD"],
            check=True, stdout=subprocess.PIPE,
        )
        sha = subprocess.run(
            ["git", "-C", str(worktree), "rev-parse", "HEAD"], check=True, text=True,
            stdout=subprocess.PIPE,
        ).stdout.strip()
        evidence = self.repair_evidence(worktree, "repair/not-actual", sha, sha)
        ledger.finish(claimed["node_id"], claimed["attempt_number"], "needs-human",
                      summary="bad receipt", **evidence)
        with self.assertRaisesRegex(WATCHER.WatcherError, "do not match"):
            ledger.retry(self.issue()["url"], self.repo, True)
        self.assertTrue(worktree.exists())

    def test_explicit_accept_merges_recorded_head_and_cleans_worktree(self) -> None:
        ledger = self.ledger()
        ledger.enqueue(self.issue())
        claimed = ledger.claim_next()
        base = subprocess.run(
            ["git", "-C", str(self.repo), "rev-parse", "HEAD"], check=True, text=True,
            stdout=subprocess.PIPE,
        ).stdout.strip()
        worktree = self.root / "accept-me"
        branch = "repair/issue-1-attempt-1"
        subprocess.run(
            ["git", "-C", str(self.repo), "worktree", "add", "-b", branch, str(worktree), "HEAD"],
            check=True, stdout=subprocess.PIPE,
        )
        change = worktree / "fixed.txt"
        change.write_text("fixed\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(worktree), "add", "fixed.txt"], check=True)
        subprocess.run(["git", "-C", str(worktree), "commit", "-m", "fix"], check=True,
                       stdout=subprocess.PIPE)
        head = subprocess.run(
            ["git", "-C", str(worktree), "rev-parse", "HEAD"], check=True, text=True,
            stdout=subprocess.PIPE,
        ).stdout.strip()
        evidence = self.repair_evidence(worktree, branch, base, head)
        ledger.finish(claimed["node_id"], claimed["attempt_number"], "ready-for-review",
                      summary="review me", **evidence)
        result = ledger.accept(self.issue()["url"], self.repo, "main")
        self.assertEqual("accepted", result["status"])
        self.assertTrue((self.repo / "fixed.txt").is_file())
        self.assertFalse(worktree.exists())
        self.assertEqual("accepted", ledger.snapshot()["issues"][0]["status"])


if __name__ == "__main__":
    unittest.main()
