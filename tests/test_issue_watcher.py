from __future__ import annotations

import datetime as dt
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
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
        ledger.finish(first["node_id"], "succeeded", summary="done")
        self.assertIsNone(ledger.claim_next())

    def test_overlapping_claims_only_return_one_run(self) -> None:
        first = self.ledger()
        second = self.ledger()
        first.enqueue(self.issue())
        first.enqueue(self.issue("I_kwDO2", 2))
        self.assertIsNotNone(first.claim_next())
        self.assertIsNone(second.claim_next())
        claimed = first.snapshot()["issues"][0]
        first.finish(claimed["node_id"], "succeeded", summary="done")
        self.assertEqual(2, second.claim_next()["issue_number"])

    def test_stale_dead_lease_is_reclaimed_within_budget(self) -> None:
        ledger = self.ledger(attempts=2)
        ledger.enqueue(self.issue())
        original = dt.datetime(2026, 8, 27, 1, 0, tzinfo=dt.timezone.utc)
        first = ledger.claim_next(original)
        self.assertEqual(1, first["attempts"])
        reclaimed = ledger.claim_next(original + dt.timedelta(seconds=21))
        self.assertEqual(2, reclaimed["attempts"])
        self.assertIsNone(ledger.claim_next(original + dt.timedelta(seconds=42)))

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
        ):
            result = WATCHER.work_once(config, ledger)
        self.assertEqual("succeeded", result["status"])
        self.assertEqual("succeeded", ledger.snapshot()["issues"][0]["status"])

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


if __name__ == "__main__":
    unittest.main()
