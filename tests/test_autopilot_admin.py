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

    def test_github_command_retries_transient_eof(self) -> None:
        failed = subprocess.CompletedProcess(["gh", "api", "user"], 1, "", "request: EOF")
        succeeded = subprocess.CompletedProcess(["gh", "api", "user"], 0, "owner\n", "")
        with mock.patch.object(ADMIN.subprocess, "run", side_effect=[failed, succeeded]) as run:
            result = ADMIN.command(["gh", "api", "user"])
        self.assertEqual(0, result.returncode)
        self.assertEqual(2, run.call_count)
        self.assertEqual("http2client=0", run.call_args.kwargs["env"]["GODEBUG"])

    def doctor_result(self, *, plist: str = "matching", loaded: bool = True,
                      loaded_config: str = "matching") -> dict:
        with mock.patch.dict(os.environ, {
            "GITHUB_ISSUE_AUTOPILOT_STATE_ROOT": str(self.root / "state"),
            "GITHUB_ISSUE_AUTOPILOT_LAUNCH_AGENTS": str(self.root / "agents"),
        }):
            paths = ADMIN.build_paths("R_one")
            config = Path(paths["config"])
            config.parent.mkdir(parents=True, exist_ok=True)
            config.write_text("{}\n", encoding="utf-8")
            if plist != "missing":
                data = plistlib.loads(ADMIN.build_plist(paths, config))
                if plist == "mismatched":
                    data["StartInterval"] = 60
                Path(paths["plist"]).parent.mkdir(parents=True, exist_ok=True)
                Path(paths["plist"]).write_bytes(plistlib.dumps(data))
            expected = plistlib.loads(ADMIN.build_plist(paths, config))["ProgramArguments"]
            if loaded_config == "mismatched":
                expected = [*expected[:-1], str(self.root / "stale-config.json")]
            interval = 60 if loaded_config == "stale-interval" else 180
            stdout = self.root / "stale.log" if loaded_config == "stale-path" else paths["stdout"]
            launchctl_output = (
                "arguments = {\n" + "\n".join(f"\t\t{item}" for item in expected) + "\n\t}\n"
                f"stdout path = {stdout}\n"
                f"stderr path = {paths['stderr']}\n"
                f"run interval = {interval} seconds\n"
                "properties = runatload | inferred program\n"
            )
            launchctl = subprocess.CompletedProcess([], 0 if loaded else 113,
                                                    launchctl_output if loaded else "", "not loaded")
            with mock.patch.object(ADMIN, "marker", return_value={
                "repository_id": "R_one", "config": str(config), "repository": "owner/repo",
            }), mock.patch.object(
                ADMIN, "run_watcher", return_value={"ok": True, "gh": True, "repositories": []}
            ), mock.patch.object(ADMIN, "command", return_value=launchctl):
                return ADMIN.doctor(self.repo)

    def test_doctor_passes_when_watcher_and_launchagent_are_healthy(self) -> None:
        result = self.doctor_result()
        self.assertTrue(result["ok"])
        self.assertTrue(result["gh"])
        self.assertEqual([], result["repositories"])
        self.assertTrue(result["launch_agent"]["plist_exists"])
        self.assertTrue(result["launch_agent"]["configuration_matches"])
        self.assertTrue(result["launch_agent"]["loaded"])
        self.assertTrue(result["launch_agent"]["loaded_configuration_matches"])

    def test_doctor_rejects_missing_plist_and_unloaded_agent(self) -> None:
        result = self.doctor_result(plist="missing", loaded=False)
        self.assertFalse(result["ok"])
        self.assertIn("LaunchAgent plist is missing", result["launch_agent"]["errors"])
        self.assertIn("LaunchAgent is not loaded", result["launch_agent"]["errors"])

    def test_doctor_rejects_existing_plist_when_agent_is_not_loaded(self) -> None:
        result = self.doctor_result(loaded=False)
        self.assertFalse(result["ok"])
        self.assertTrue(result["launch_agent"]["plist_exists"])
        self.assertTrue(result["launch_agent"]["configuration_matches"])
        self.assertIn("LaunchAgent is not loaded", result["launch_agent"]["errors"])

    def test_doctor_rejects_mismatched_plist_without_mutating_it(self) -> None:
        result = self.doctor_result(plist="mismatched")
        self.assertFalse(result["ok"])
        self.assertTrue(result["launch_agent"]["loaded"])
        self.assertFalse(result["launch_agent"]["configuration_matches"])
        self.assertIn("does not match", result["launch_agent"]["errors"][0])

    def test_doctor_rejects_stale_configuration_loaded_by_launchctl(self) -> None:
        result = self.doctor_result(loaded_config="mismatched")
        self.assertFalse(result["ok"])
        self.assertTrue(result["launch_agent"]["configuration_matches"])
        self.assertFalse(result["launch_agent"]["loaded_configuration_matches"])
        self.assertIn("Loaded LaunchAgent does not match", result["launch_agent"]["errors"][0])

    def test_doctor_rejects_stale_loaded_interval_or_log_path(self) -> None:
        for loaded_config in ("stale-interval", "stale-path"):
            with self.subTest(loaded_config=loaded_config):
                result = self.doctor_result(loaded_config=loaded_config)
                self.assertFalse(result["ok"])
                self.assertTrue(result["launch_agent"]["configuration_matches"])
                self.assertFalse(result["launch_agent"]["loaded_configuration_matches"])

    def test_stop_is_reflected_as_missing_and_unloaded_launchagent(self) -> None:
        with mock.patch.dict(os.environ, {
            "GITHUB_ISSUE_AUTOPILOT_STATE_ROOT": str(self.root / "state"),
            "GITHUB_ISSUE_AUTOPILOT_LAUNCH_AGENTS": str(self.root / "agents"),
        }):
            paths = ADMIN.build_paths("R_one")
            config = Path(paths["config"])
            config.parent.mkdir(parents=True)
            config.write_text("{}\n", encoding="utf-8")
            Path(paths["plist"]).parent.mkdir(parents=True)
            Path(paths["plist"]).write_bytes(ADMIN.build_plist(paths, config))
            info = {"repository_id": "R_one", "config": str(config), "repository": "owner/repo"}
            stopped = subprocess.CompletedProcess([], 0, "", "")
            with mock.patch.object(ADMIN, "marker", return_value=info), mock.patch.object(
                ADMIN, "command", return_value=stopped
            ):
                ADMIN.stop(self.repo)
            unloaded = subprocess.CompletedProcess([], 113, "", "not loaded")
            with mock.patch.object(ADMIN, "command", return_value=unloaded):
                health = ADMIN.launch_agent_health(info)
        self.assertFalse(health["ok"])
        self.assertFalse(health["plist_exists"])
        self.assertFalse(health["loaded"])

    def test_doctor_cli_returns_nonzero_for_unhealthy_result(self) -> None:
        with mock.patch.object(ADMIN.sys, "argv", ["autopilot_admin.py", "doctor",
                                                   "--repo-path", str(self.repo)]), mock.patch.object(
            ADMIN, "doctor", return_value={"ok": False, "launch_agent": {"loaded": False}}
        ), mock.patch("builtins.print"):
            self.assertEqual(2, ADMIN.main())


if __name__ == "__main__":
    unittest.main()
