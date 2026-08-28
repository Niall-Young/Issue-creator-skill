from __future__ import annotations

import importlib.util
import json
import os
import plistlib
import sqlite3
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
        paths = {"database": self.root / "state.sqlite3", "key": "abc123",
                 "runtime_admin": self.root / "runtime" / "autopilot_admin.py"}
        value = ADMIN.build_config(
            self.repo, {"id": "R_one", "nameWithOwner": "owner/repo"}, "owner", "/bin/orca",
            "agent-ready", paths, "claude", ["claude", "codex"],
        )
        self.assertEqual(3, value["schema_version"])
        self.assertEqual(["agent-ready"], value["repositories"][0]["labels"])
        self.assertEqual("never", value["policy"]["publication"])
        self.assertEqual("claude", value["orca"]["default_agent"])
        self.assertEqual(["claude", "codex"], value["orca"]["allowed_agents"])
        self.assertEqual("orca-automation", value["scheduler"]["backend"])
        self.assertEqual("*/3 * * * *", value["scheduler"]["trigger"])
        self.assertEqual("fresh", value["scheduler"]["session_mode"])
        self.assertIsNone(value["scheduler"]["automation_id"])
        self.assertNotIn("activate_after", value["repositories"][0])
        self.assertNotIn("executor", value)

    def test_automation_arguments_use_existing_workspace_and_safe_precheck(self) -> None:
        paths = {"database": self.root / "state.sqlite3", "key": "abc123",
                 "runtime_admin": self.root / "runtime" / "autopilot_admin.py"}
        config_path = self.root / "autopilot.json"
        value = ADMIN.build_config(
            self.repo, {"id": "R_one", "nameWithOwner": "owner/repo"}, "owner", "/bin/orca",
            "agent-ready", paths, "codex", ["codex"],
        )
        arguments = ADMIN.automation_arguments(value, config_path, enabled=False)
        self.assertIn("*/3 * * * *", arguments)
        self.assertIn(f"path:{self.repo}", arguments)
        self.assertIn("--fresh-session", arguments)
        self.assertIn("--disabled", arguments)
        precheck = arguments[arguments.index("--precheck") + 1]
        self.assertIn("automation-precheck", precheck)
        self.assertIn(str(config_path), precheck)
        prompt = arguments[arguments.index("--prompt") + 1]
        self.assertIn("ensure", prompt)
        self.assertIn("Do not inspect Issues", prompt)

    def test_precheck_skips_only_for_one_connected_coordinator(self) -> None:
        config = self.root / "autopilot.json"
        config.write_text(json.dumps({
            "repositories": [{"repo_path": str(self.repo)}],
            "orca": {"cli": "/bin/orca"},
        }), encoding="utf-8")
        connected = {"result": {"terminals": [
            {"title": "Issue Autopilot Coordinator", "connected": True, "handle": "t1"}
        ]}}
        ready = {"result": {"runtime": {"state": "ready"}}}
        with mock.patch.object(ADMIN, "orca_json", side_effect=[
            ready, {"result": {"repo": {"path": str(self.repo)}}}, connected,
        ]):
            result = ADMIN.automation_precheck(config)
        self.assertEqual("healthy", result["status"])
        self.assertFalse(result["run_automation"])
        with mock.patch.object(ADMIN, "orca_json", side_effect=[
            ready, {"result": {"repo": {"path": str(self.repo)}}},
            {"result": {"terminals": []}},
        ]):
            result = ADMIN.automation_precheck(config)
        self.assertEqual("needs-recovery", result["status"])
        self.assertTrue(result["run_automation"])

    def test_precheck_pauses_when_repository_path_is_missing(self) -> None:
        config = self.root / "autopilot.json"
        config.write_text(json.dumps({
            "repositories": [{"repo_path": str(self.root / "missing")}],
            "orca": {"cli": "/bin/orca"},
            "scheduler": {"automation_id": "auto-1"},
        }), encoding="utf-8")
        with mock.patch.object(ADMIN, "orca_json", return_value={"result": {}}) as call:
            result = ADMIN.automation_precheck(config)
        self.assertEqual("paused-missing-workspace", result["status"])
        self.assertFalse(result["run_automation"])
        self.assertTrue(result["state_preserved"])
        self.assertIn(mock.call("/bin/orca", "automations", "edit", "auto-1", "--disabled"),
                      call.call_args_list)

    def test_repository_selector_survives_deleted_checkout(self) -> None:
        with mock.patch.dict(os.environ, {
            "GITHUB_ISSUE_AUTOPILOT_STATE_ROOT": str(self.root / "state"),
        }):
            paths = ADMIN.build_paths("R_one")
            config = {
                "schema_version": 3,
                "repositories": [{"repository": "owner/repo", "repository_id": "R_one",
                                  "repo_path": str(self.root / "deleted")}],
                "scheduler": {"backend": "orca-automation", "automation_id": "auto-1"},
            }
            Path(paths["config"]).parent.mkdir(parents=True)
            Path(paths["config"]).write_text(json.dumps(config), encoding="utf-8")
            info = ADMIN.resolve_info(repository="owner/repo")
        self.assertEqual("R_one", info["repository_id"])
        self.assertEqual(str(paths["config"]), info["config"])

    def test_uninstall_refuses_unresolved_ledger(self) -> None:
        with mock.patch.dict(os.environ, {
            "GITHUB_ISSUE_AUTOPILOT_STATE_ROOT": str(self.root / "state"),
        }):
            paths = ADMIN.build_paths("R_one")
            config = {
                "schema_version": 3, "state_db": str(paths["database"]),
                "repositories": [{"repository": "owner/repo", "repository_id": "R_one",
                                  "repo_path": str(self.repo)}],
                "orca": {"cli": "/bin/orca"},
                "scheduler": {"backend": "orca-automation", "automation_id": "auto-1"},
            }
            Path(paths["config"]).parent.mkdir(parents=True)
            Path(paths["config"]).write_text(json.dumps(config), encoding="utf-8")
            connection = sqlite3.connect(paths["database"])
            connection.executescript(
                "CREATE TABLE issues(node_id TEXT, issue_number INTEGER, url TEXT, status TEXT, attempts INTEGER);"
                "CREATE TABLE attempts(node_id TEXT, attempt_number INTEGER, worktree_path TEXT);"
                "INSERT INTO issues VALUES('node', 8, 'https://example/8', 'running', 1);"
            )
            connection.close()
            with mock.patch.object(ADMIN, "stop", return_value={"status": "stopped"}), self.assertRaisesRegex(
                ADMIN.AdminError, "#8 \(running\)"
            ):
                ADMIN.uninstall(repository="owner/repo")
        self.assertTrue(Path(paths["config"]).exists())

    def test_uninstall_removes_automation_and_archives_state(self) -> None:
        with mock.patch.dict(os.environ, {
            "GITHUB_ISSUE_AUTOPILOT_STATE_ROOT": str(self.root / "state"),
        }):
            paths = ADMIN.build_paths("R_one")
            config = {
                "schema_version": 3, "state_db": str(paths["database"]),
                "repositories": [{"repository": "owner/repo", "repository_id": "R_one",
                                  "repo_path": str(self.repo)}],
                "orca": {"cli": "/bin/orca"},
                "scheduler": {"backend": "orca-automation", "automation_id": "auto-1"},
            }
            Path(paths["config"]).parent.mkdir(parents=True)
            Path(paths["config"]).write_text(json.dumps(config), encoding="utf-8")
            connection = sqlite3.connect(paths["database"])
            connection.executescript(
                "CREATE TABLE issues(node_id TEXT, issue_number INTEGER, url TEXT, status TEXT, attempts INTEGER);"
                "CREATE TABLE attempts(node_id TEXT, attempt_number INTEGER, worktree_path TEXT);"
                "INSERT INTO issues VALUES('node', 7, 'https://example/7', 'accepted', 1);"
            )
            connection.close()
            with mock.patch.object(ADMIN, "stop", return_value={"status": "stopped"}), mock.patch.object(
                ADMIN, "orca_json", return_value={"result": {"runs": []}}
            ) as orca:
                result = ADMIN.uninstall(repository_id="R_one")
        self.assertEqual("uninstalled", result["status"])
        self.assertFalse(Path(paths["root"]).exists())
        archive = Path(result["archive"])
        self.assertTrue((archive / "state.sqlite3").is_file())
        self.assertTrue((archive / "automation-runs.json").is_file())
        self.assertTrue((archive / "archive-manifest.json").is_file())
        self.assertIn(mock.call("/bin/orca", "automations", "remove", "auto-1"),
                      orca.call_args_list)

    def test_coordinator_replaces_shell_so_exit_disconnects_terminal(self) -> None:
        config = self.root / "autopilot.json"
        config.write_text(json.dumps({
            "repositories": [{"repository_id": "R_one", "repo_path": str(self.repo)}],
            "orca": {"cli": "/bin/orca"},
        }), encoding="utf-8")
        responses = [
            {"result": {"runtime": {"state": "ready"}}},
            {"result": {"repo": {"path": str(self.repo)}}},
            {"result": {"runtime": {"state": "ready"}}},
            {"result": {"terminals": []}},
            {"result": {"terminal": {"handle": "t1"}}},
        ]
        with mock.patch.object(ADMIN, "orca_json", side_effect=responses) as call:
            result = ADMIN.ensure_coordinator(config)
        self.assertEqual("started", result["status"])
        create_args = call.call_args_list[-1].args
        command_value = create_args[create_args.index("--command") + 1]
        self.assertTrue(command_value.startswith("exec "))
        self.assertIn("issue_watcher.py run", command_value)

    def runtime_refresh_fixture(self) -> tuple[Path, dict, dict]:
        paths = ADMIN.build_paths("R_one")
        config = ADMIN.build_config(
            self.repo, {"id": "R_one", "nameWithOwner": "owner/repo"}, "owner", "/bin/orca",
            "agent-ready", paths, "codex", ["codex"],
        )
        config["scheduler"]["automation_id"] = "auto-1"
        config_path = Path(paths["config"])
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(json.dumps(config), encoding="utf-8")
        previous = ADMIN.expected_automation(config, config_path, enabled=False)
        previous["id"] = "auto-1"
        return config_path, config, {"result": {"automation": previous}}

    def test_runtime_refresh_dry_run_is_read_only(self) -> None:
        with mock.patch.dict(os.environ, {
            "GITHUB_ISSUE_AUTOPILOT_STATE_ROOT": str(self.root / "state"),
        }):
            config_path, _config, response = self.runtime_refresh_fixture()
            with mock.patch.object(ADMIN, "orca_json", return_value=response) as orca:
                result = ADMIN.refresh_runtimes(dry_run=True)
        self.assertEqual("ready", result["status"])
        self.assertEqual(1, result["installations"])
        self.assertEqual(("automations", "show"), orca.call_args.args[1:3])
        self.assertFalse((config_path.parent / "runtime" / "autopilot_admin.py").exists())

    def test_runtime_refresh_replaces_scripts_and_preserves_paused_state(self) -> None:
        with mock.patch.dict(os.environ, {
            "GITHUB_ISSUE_AUTOPILOT_STATE_ROOT": str(self.root / "state"),
        }):
            config_path, config, response = self.runtime_refresh_fixture()
            runtime = config_path.parent / "runtime"
            runtime.mkdir()
            (runtime / "autopilot_admin.py").write_text("old admin", encoding="utf-8")
            (runtime / "issue_watcher.py").write_text("old watcher", encoding="utf-8")
            with mock.patch.object(ADMIN, "orca_json", return_value=response), mock.patch.object(
                ADMIN, "close_coordinators", return_value=0
            ):
                result = ADMIN.refresh_runtimes()
        self.assertEqual("refreshed", result["status"])
        self.assertEqual(Path(ADMIN.__file__).read_bytes(),
                         (runtime / "autopilot_admin.py").read_bytes())
        self.assertEqual(Path(ADMIN.__file__).with_name("issue_watcher.py").read_bytes(),
                         (runtime / "issue_watcher.py").read_bytes())
        self.assertFalse(ADMIN.normalized_automation(response)["enabled"])

    def test_runtime_refresh_rolls_back_runtime_files_on_verification_failure(self) -> None:
        with mock.patch.dict(os.environ, {
            "GITHUB_ISSUE_AUTOPILOT_STATE_ROOT": str(self.root / "state"),
        }):
            config_path, _config, response = self.runtime_refresh_fixture()
            runtime = config_path.parent / "runtime"
            runtime.mkdir()
            admin = runtime / "autopilot_admin.py"
            watcher = runtime / "issue_watcher.py"
            admin.write_text("old admin", encoding="utf-8")
            watcher.write_text("old watcher", encoding="utf-8")
            with mock.patch.object(ADMIN, "orca_json", return_value=response), mock.patch.object(
                ADMIN, "close_coordinators", return_value=0
            ), mock.patch.object(ADMIN, "runtime_matches_source", return_value=False), self.assertRaisesRegex(
                ADMIN.AdminError, "rolled back"
            ):
                ADMIN.refresh_runtimes()
        self.assertEqual("old admin", admin.read_text())
        self.assertEqual("old watcher", watcher.read_text())

    def test_ensure_replaces_duplicate_connected_coordinators(self) -> None:
        config = self.root / "autopilot.json"
        config.write_text(json.dumps({
            "repositories": [{"repository_id": "R_one", "repo_path": str(self.repo)}],
            "orca": {"cli": "/bin/orca"},
        }), encoding="utf-8")
        responses = [
            {"result": {"runtime": {"state": "ready"}}},
            {"result": {"repo": {"path": str(self.repo)}}},
            {"result": {"runtime": {"state": "ready"}}},
            {"result": {"terminals": [
                {"title": "Issue Autopilot Coordinator", "connected": True, "handle": "old-1"},
                {"title": "Issue Autopilot Coordinator", "connected": True, "handle": "old-2"},
            ]}},
            {"result": {"close": {"handle": "old-1"}}},
            {"result": {"close": {"handle": "old-2"}}},
            {"result": {"terminal": {"handle": "new"}}},
        ]
        with mock.patch.object(ADMIN, "orca_json", side_effect=responses) as call:
            result = ADMIN.ensure_coordinator(config)
        self.assertEqual({"status": "started", "terminal": "new"}, result)
        close_calls = [item.args for item in call.call_args_list if "close" in item.args]
        self.assertEqual(2, len(close_calls))

    def test_scheduler_health_accepts_exact_enabled_automation(self) -> None:
        paths = {"database": self.root / "state.sqlite3", "key": "abc123",
                 "runtime_admin": self.root / "runtime" / "autopilot_admin.py"}
        config_path = self.root / "autopilot.json"
        config = ADMIN.build_config(
            self.repo, {"id": "R_one", "nameWithOwner": "owner/repo"}, "owner", "/bin/orca",
            "agent-ready", paths, "codex", ["codex"],
        )
        config["scheduler"]["automation_id"] = "auto-1"
        config_path.write_text(json.dumps(config), encoding="utf-8")
        automation = ADMIN.expected_automation(config, config_path, enabled=True)
        with mock.patch.object(ADMIN, "orca_json", return_value={
            "result": {"automation": {"id": "auto-1", **automation}}
        }):
            health = ADMIN.scheduler_health(config_path)
        self.assertTrue(health["ok"])
        self.assertTrue(health["enabled"])
        self.assertTrue(health["configuration_matches"])

    def test_scheduler_health_rejects_paused_or_drifted_automation(self) -> None:
        paths = {"database": self.root / "state.sqlite3", "key": "abc123",
                 "runtime_admin": self.root / "runtime" / "autopilot_admin.py"}
        config_path = self.root / "autopilot.json"
        config = ADMIN.build_config(
            self.repo, {"id": "R_one", "nameWithOwner": "owner/repo"}, "owner", "/bin/orca",
            "agent-ready", paths, "codex", ["codex"],
        )
        config["scheduler"]["automation_id"] = "auto-1"
        config_path.write_text(json.dumps(config), encoding="utf-8")
        automation = ADMIN.expected_automation(config, config_path, enabled=False)
        automation["trigger"] = "hourly"
        with mock.patch.object(ADMIN, "orca_json", return_value={
            "result": {"automation": {"id": "auto-1", **automation}}
        }):
            health = ADMIN.scheduler_health(config_path)
        self.assertFalse(health["ok"])
        self.assertFalse(health["enabled"])
        self.assertFalse(health["configuration_matches"])

    def test_paused_exact_automation_is_not_reported_as_configuration_drift(self) -> None:
        paths = {"database": self.root / "state.sqlite3", "key": "abc123",
                 "runtime_admin": self.root / "runtime" / "autopilot_admin.py"}
        config_path = self.root / "autopilot.json"
        config = ADMIN.build_config(
            self.repo, {"id": "R_one", "nameWithOwner": "owner/repo"}, "owner", "/bin/orca",
            "agent-ready", paths, "codex", ["codex"],
        )
        config["scheduler"]["automation_id"] = "auto-1"
        config_path.write_text(json.dumps(config), encoding="utf-8")
        automation = ADMIN.expected_automation(config, config_path, enabled=False)
        with mock.patch.object(ADMIN, "orca_json", return_value={
            "result": {"automation": {"id": "auto-1", **automation}}
        }):
            health = ADMIN.scheduler_health(config_path)
        self.assertFalse(health["ok"])
        self.assertFalse(health["enabled"])
        self.assertTrue(health["configuration_matches"])
        self.assertEqual(["Orca Automation is paused"], health["errors"])

    def test_normalizes_real_orca_automation_shape(self) -> None:
        normalized = ADMIN.normalized_automation({"result": {"automation": {
            "name": "Issue Autopilot", "prompt": "ensure", "agentId": "codex",
            "precheck": {"command": "precheck", "timeoutSeconds": 60},
            "runContext": {"path": str(self.repo)}, "workspaceMode": "existing",
            "reuseSession": False, "rrule": "*/3 * * * *", "enabled": True,
            "missedRunGraceMinutes": 5,
        }}})
        self.assertEqual("*/3 * * * *", normalized["trigger"])
        self.assertEqual("codex", normalized["provider"])
        self.assertEqual("precheck", normalized["precheck"])
        self.assertEqual(60, normalized["precheck_timeout_seconds"])
        self.assertEqual(f"path:{self.repo}", normalized["workspace"])
        self.assertEqual("fresh", normalized["session_mode"])

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
