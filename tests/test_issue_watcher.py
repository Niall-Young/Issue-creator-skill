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

    def test_github_command_retries_transient_eof(self) -> None:
        failed = subprocess.CompletedProcess(["gh", "api", "user"], 1, "", "request: EOF")
        succeeded = subprocess.CompletedProcess(["gh", "api", "user"], 0, "owner\n", "")
        with mock.patch.object(WATCHER.subprocess, "run", side_effect=[failed, succeeded]) as run:
            result = WATCHER.command(["gh", "api", "user"])
        self.assertEqual(0, result.returncode)
        self.assertEqual(2, run.call_count)
        self.assertEqual("http2client=0", run.call_args.kwargs["env"]["GODEBUG"])

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
            "schema_version": 2,
            "state_db": self.db,
            "lease_timeout_seconds": 20,
            "max_attempts": 2,
            "max_concurrent_workers": 3,
            "policy": {"scope_approval": "eligible-issue", "publication": "never", "max_risk": "medium"},
            "repositories": [{
                "repository": "owner/repo",
                "repo_path": self.repo,
                "author": "@me",
                "labels": [],
                "activate_after": dt.datetime(2026, 8, 27, tzinfo=dt.timezone.utc),
            }],
            "orca": {"cli": sys.executable, "default_agent": "codex",
                     "allowed_agents": ["codex", "claude"], "setup": "run"},
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

    def test_claim_many_starts_three_and_leaves_the_fourth_queued(self) -> None:
        ledger = self.ledger()
        for number in range(1, 5):
            ledger.enqueue(self.issue(f"I_kwDO{number}", number))
        claimed = ledger.claim_many(3)
        self.assertEqual([1, 2, 3], [item["issue_number"] for item in claimed])
        self.assertEqual("queued", ledger.snapshot()["issues"][3]["status"])

    def test_issue_agent_label_overrides_repository_default(self) -> None:
        config = self.config()
        self.assertEqual("codex", WATCHER.select_agent({"labels": []}, config))
        self.assertEqual("claude", WATCHER.select_agent(
            {"labels": [{"name": "agent:claude"}]}, config))
        with self.assertRaisesRegex(WATCHER.WatcherError, "multiple agent labels"):
            WATCHER.select_agent(
                {"labels": [{"name": "agent:codex"}, {"name": "agent:claude"}]}, config)

    def test_dispatch_creates_orca_child_worker_with_selected_agent(self) -> None:
        config = self.config()
        ledger = self.ledger()
        issue = self.issue()
        ledger.enqueue(issue)
        claimed = ledger.claim_next()
        eligible = dict(issue, author={"login": "owner"}, labels=[])
        with mock.patch.object(WATCHER, "github_login", return_value="owner"), mock.patch.object(
            WATCHER, "list_issues", return_value=[eligible]
        ), mock.patch.object(
            WATCHER, "create_orca_task", return_value="task-1"
        ), mock.patch.object(WATCHER, "orca_call", side_effect=[
            {"ok": True, "result": {"dispatch": {"id": "dispatch-1"}, "worktree": {
                "id": "repo::/tmp/issue-1", "path": "/tmp/issue-1", "branch": "refs/heads/issue-1"
            }}},
            {"ok": True, "result": {}},
        ]):
            result = WATCHER.dispatch_one(config, ledger, "run-1", claimed)
        self.assertEqual("running", result["status"])
        attempt = ledger.snapshot()["issues"][0]["attempt_history"][0]
        self.assertEqual("codex", attempt["agent_id"])
        self.assertEqual("dispatch-1", attempt["orca_dispatch_id"])

    def test_unavailable_agent_still_creates_visible_blocked_worktree(self) -> None:
        config = self.config()
        config["orca"]["allowed_agents"] = ["codex"]
        ledger = self.ledger()
        issue = self.issue()
        ledger.enqueue(issue)
        claimed = ledger.claim_next()
        eligible = dict(issue, author={"login": "owner"}, labels=[{"name": "agent:claude"}])
        with mock.patch.object(WATCHER, "github_login", return_value="owner"), mock.patch.object(
            WATCHER, "list_issues", return_value=[eligible]
        ), mock.patch.object(
            WATCHER, "create_orca_task", return_value="task-1"
        ), mock.patch.object(WATCHER, "create_blocked_worktree", return_value={
            "id": "repo::/tmp/blocked", "path": "/tmp/blocked", "branch": "refs/heads/blocked"
        }):
            result = WATCHER.dispatch_one(config, ledger, "run-1", claimed)
        self.assertEqual("needs-human", result["status"])
        attempt = ledger.snapshot()["issues"][0]["attempt_history"][0]
        self.assertEqual("task-1", attempt["orca_task_id"])
        self.assertEqual("repo::/tmp/blocked", attempt["orca_worktree_id"])

    def test_completed_orca_worker_requires_verified_receipt(self) -> None:
        config = self.config()
        ledger = self.ledger()
        ledger.enqueue(self.issue())
        claimed = ledger.claim_next()
        worktree = self.root / "orca-worktree"
        branch = "issue-1-attempt-1"
        subprocess.run(
            ["git", "-C", str(self.repo), "worktree", "add", "-b", branch, str(worktree), "HEAD"],
            check=True, stdout=subprocess.PIPE,
        )
        sha = subprocess.run(["git", "-C", str(worktree), "rev-parse", "HEAD"], check=True,
                             text=True, stdout=subprocess.PIPE).stdout.strip()
        evidence = self.repair_evidence(worktree, branch, sha, sha)
        ledger.assign_orca(
            claimed["node_id"], claimed["attempt_number"], run_id=evidence["run_id"], agent_id="codex",
            orca_task_id="task-1", orca_dispatch_id="dispatch-1", orca_worktree_id="repo::worktree",
            worktree_path=str(worktree), branch=branch,
        )
        receipt = "AUTOPILOT_RESULT: " + json.dumps({
            "status": "ready-for-review", "summary": "verified", **evidence,
        })
        with mock.patch.object(WATCHER, "orca_call", side_effect=[
            {"ok": True, "result": {"dispatch": {"status": "completed", "outcome": "succeeded"}}},
            {"ok": True, "result": {"rows": [{"text": receipt}]}},
            {"ok": True, "result": {}},
            {"ok": True, "result": {}},
        ]):
            completed = WATCHER.reconcile_workers(config, ledger)
        self.assertEqual("ready-for-review", completed[0]["status"], completed)
        self.assertEqual("ready-for-review", ledger.snapshot()["issues"][0]["status"])

    def test_load_config_rejects_missing_activation_cutoff(self) -> None:
        path = self.root / "config.json"
        path.write_text(json.dumps({
            "schema_version": 2,
            "state_db": str(self.db),
            "policy": {"scope_approval": "eligible-issue", "publication": "never"},
            "repositories": [{"repository": "owner/repo", "repo_path": str(self.repo), "author": "@me"}],
            "orca": {"cli": sys.executable, "default_agent": "codex", "allowed_agents": ["codex"]},
            "lease_timeout_seconds": 20,
        }), encoding="utf-8")
        with self.assertRaisesRegex(WATCHER.WatcherError, "activate_after"):
            WATCHER.load_config(path)

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
