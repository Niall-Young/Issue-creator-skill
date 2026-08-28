from __future__ import annotations

import importlib.util
import json
import os
import plistlib
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "github-issue-autopilot" / "scripts" / "autopilot_admin.py"
SPEC = importlib.util.spec_from_file_location("autopilot_admin", SCRIPT)
ADMIN = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(ADMIN)


class AutopilotAdminTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        subprocess.run(["git", "-C", str(self.repo), "init", "-b", "main"], check=True,
                       stdout=subprocess.PIPE)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_paths_are_stable_and_repository_isolated(self) -> None:
        with mock.patch.dict(os.environ, {
            "GITHUB_ISSUE_AUTOPILOT_STATE_ROOT": str(self.root / "state"),
            "GITHUB_ISSUE_AUTOPILOT_LAUNCH_AGENTS": str(self.root / "agents"),
        }):
            first = ADMIN.build_paths("R_one")
            again = ADMIN.build_paths("R_one")
            other = ADMIN.build_paths("R_two")
        self.assertEqual(first, again)
        self.assertNotEqual(first["key"], other["key"])
        self.assertTrue(str(first["config"]).startswith(str((self.root / "state").resolve())))

    def test_generated_plist_runs_one_tick_with_absolute_paths(self) -> None:
        paths = {
            "launch_label": "com.example.autopilot.123",
            "stdout": self.root / "stdout.log",
            "stderr": self.root / "stderr.log",
            "runtime_admin": self.root / "runtime" / "autopilot_admin.py",
        }
        config = self.root / "config.json"
        value = plistlib.loads(ADMIN.build_plist(paths, config))
        self.assertEqual(180, value["StartInterval"])
        self.assertIn("ensure", value["ProgramArguments"])
        self.assertEqual(str(config), value["ProgramArguments"][-1])
        plist_path = self.root / "autopilot.plist"
        plist_path.write_bytes(ADMIN.build_plist(paths, config))
        lint = subprocess.run(["plutil", "-lint", str(plist_path)], text=True, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE)
        self.assertEqual(0, lint.returncode, lint.stderr)

    def test_config_uses_orca_and_user_selected_default_agent(self) -> None:
        paths = {"database": self.root / "state.sqlite3"}
        value = ADMIN.build_config(
            self.repo, {"id": "R_one", "nameWithOwner": "owner/repo"}, "owner", "/bin/orca",
            "agent-ready", paths, "2026-08-28T00:00:00+00:00", "claude", ["claude", "codex"],
        )
        self.assertEqual(["agent-ready"], value["repositories"][0]["labels"])
        self.assertEqual("never", value["policy"]["publication"])
        self.assertEqual("claude", value["orca"]["default_agent"])
        self.assertEqual(["claude", "codex"], value["orca"]["allowed_agents"])
        self.assertNotIn("executor", value)


if __name__ == "__main__":
    unittest.main()
