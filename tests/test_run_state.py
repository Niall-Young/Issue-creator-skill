from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
import uuid
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "github-issue-repair" / "scripts" / "run_state.py"


class RunStateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.repo = Path(self.temporary.name) / "repo"
        self.repo.mkdir()
        self.git("init", "-b", "main")
        self.git("config", "user.name", "Test User")
        self.git("config", "user.email", "test@example.com")
        (self.repo / "README.md").write_text("fixture\n", encoding="utf-8")
        self.git("add", "README.md")
        self.git("commit", "-m", "fixture")
        self.base_sha = self.git("rev-parse", "HEAD").stdout.strip()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def git(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", "-C", str(self.repo), *args],
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    def cli(self, *args: str, ok: bool = True) -> dict:
        result = subprocess.run(
            ["python3", str(SCRIPT), *args],
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if ok and result.returncode != 0:
            self.fail(result.stderr)
        if not ok and result.returncode == 0:
            self.fail("command unexpectedly succeeded")
        return json.loads(result.stdout if ok else result.stderr)

    def init_run(self) -> tuple[str, dict]:
        run_id = str(uuid.uuid4())
        result = self.cli(
            "init",
            "--repo",
            str(self.repo),
            "--source-url",
            "https://github.com/owner/repo/issues/1",
            "--base-sha",
            self.base_sha,
            "--run-id",
            run_id,
        )
        return run_id, result["run"]

    def transition(self, run_id: str, state: str, ok: bool = True) -> dict:
        return self.cli(
            "transition",
            "--repo",
            str(self.repo),
            "--run-id",
            run_id,
            "--to",
            state,
            ok=ok,
        )

    def write_plan(self, packages: list[dict], name: str = "plan.json") -> Path:
        path = Path(self.temporary.name) / name
        path.write_text(json.dumps({"packages": packages}), encoding="utf-8")
        return path

    @staticmethod
    def package(package_id: str = "pkg-a", depends_on: list[str] | None = None) -> dict:
        return {
            "id": package_id,
            "title": f"Repair {package_id}",
            "status": "PROPOSED",
            "risk": "low",
            "depends_on": depends_on or [],
            "acceptance_criteria": [f"{package_id} works"],
            "verification": [f"test {package_id}"],
        }

    def test_init_is_idempotent_and_rejects_conflicting_input(self) -> None:
        run_id, first = self.init_run()
        second = self.cli(
            "init",
            "--repo",
            str(self.repo),
            "--source-url",
            first["source_url"],
            "--base-sha",
            first["base_sha"],
            "--run-id",
            run_id,
        )
        self.assertEqual(first, second["run"])

        error = self.cli(
            "init",
            "--repo",
            str(self.repo),
            "--source-url",
            "https://github.com/owner/repo/issues/2",
            "--base-sha",
            first["base_sha"],
            "--run-id",
            run_id,
            ok=False,
        )
        self.assertIn("different input", error["error"])

    def test_scope_and_publication_approvals_gate_writes(self) -> None:
        run_id, _ = self.init_run()
        self.transition(run_id, "TRIAGE")
        self.transition(run_id, "AWAIT_SCOPE_APPROVAL")
        error = self.transition(run_id, "PREPARE", ok=False)
        self.assertIn("scope approval", error["error"])

        self.cli(
            "approve",
            "--repo",
            str(self.repo),
            "--run-id",
            run_id,
            "--kind",
            "scope",
            "--actor",
            "user",
        )
        for state in ("PREPARE", "IMPLEMENT", "VERIFY", "REVIEW", "AWAIT_PUBLICATION_APPROVAL"):
            self.transition(run_id, state)
        error = self.transition(run_id, "PUSH_AND_DRAFT_PR", ok=False)
        self.assertIn("publication approval", error["error"])

        self.cli(
            "approve",
            "--repo",
            str(self.repo),
            "--run-id",
            run_id,
            "--kind",
            "publication",
            "--actor",
            "user",
        )
        final = self.transition(run_id, "PUSH_AND_DRAFT_PR")
        self.assertEqual("PUSH_AND_DRAFT_PR", final["run"]["state"])

    def test_plan_rejects_cycles(self) -> None:
        run_id, _ = self.init_run()
        path = self.write_plan(
            [self.package("pkg-a", ["pkg-b"]), self.package("pkg-b", ["pkg-a"])]
        )
        error = self.cli(
            "plan",
            "--repo",
            str(self.repo),
            "--run-id",
            run_id,
            "--file",
            str(path),
            ok=False,
        )
        self.assertIn("cycle", error["error"])

    def test_replan_replaces_packages_and_package_approval_is_gated(self) -> None:
        run_id, _ = self.init_run()
        first = self.write_plan([self.package("pkg-a")], "first.json")
        self.cli("plan", "--repo", str(self.repo), "--run-id", run_id, "--file", str(first))
        self.transition(run_id, "TRIAGE")
        self.transition(run_id, "AWAIT_SCOPE_APPROVAL")
        error = self.cli(
            "package",
            "--repo",
            str(self.repo),
            "--run-id",
            run_id,
            "--package-id",
            "pkg-a",
            "--to",
            "APPROVED",
            ok=False,
        )
        self.assertIn("scope approval", error["error"])

        self.cli("approve", "--repo", str(self.repo), "--run-id", run_id, "--kind", "scope", "--actor", "user")
        approved = self.cli(
            "package",
            "--repo",
            str(self.repo),
            "--run-id",
            run_id,
            "--package-id",
            "pkg-a",
            "--to",
            "APPROVED",
        )
        self.assertEqual("APPROVED", approved["run"]["plan"]["packages"][0]["status"])

        self.transition(run_id, "PREPARE")
        self.transition(run_id, "NEEDS_REPLAN")
        replacement = self.write_plan([self.package("pkg-b")], "replacement.json")
        replanned = self.cli(
            "plan", "--repo", str(self.repo), "--run-id", run_id, "--file", str(replacement)
        )
        self.assertEqual("pkg-b", replanned["run"]["plan"]["packages"][0]["id"])

    def test_receipts_are_idempotent_and_conflicts_fail(self) -> None:
        run_id, _ = self.init_run()
        self.transition(run_id, "TRIAGE")
        self.transition(run_id, "AWAIT_SCOPE_APPROVAL")
        self.cli("approve", "--repo", str(self.repo), "--run-id", run_id, "--kind", "scope", "--actor", "user")
        for state in ("PREPARE", "IMPLEMENT", "VERIFY", "REVIEW", "AWAIT_PUBLICATION_APPROVAL"):
            self.transition(run_id, state)
        self.cli("approve", "--repo", str(self.repo), "--run-id", run_id, "--kind", "publication", "--actor", "user")
        self.transition(run_id, "PUSH_AND_DRAFT_PR")

        command = (
            "receipt",
            "--repo",
            str(self.repo),
            "--run-id",
            run_id,
            "--kind",
            "draft-pr",
            "--key",
            "pr:pkg-1",
            "--value",
            "https://github.com/owner/repo/pull/1",
        )
        first = self.cli(*command)
        second = self.cli(*command)
        self.assertEqual(first["run"], second["run"])

        conflicting = list(command)
        conflicting[-1] = "https://github.com/owner/repo/pull/2"
        error = self.cli(*conflicting, ok=False)
        self.assertIn("conflicting receipt", error["error"])

    def test_linked_worktree_uses_common_ledger(self) -> None:
        worktree = Path(self.temporary.name) / "worktree"
        self.git("worktree", "add", "-b", "repair/pkg", str(worktree), "HEAD")

        run_id, expected = self.init_run()
        result = self.cli("show", "--repo", str(worktree), "--run-id", run_id)
        self.assertEqual(expected, result["run"])


if __name__ == "__main__":
    unittest.main()
