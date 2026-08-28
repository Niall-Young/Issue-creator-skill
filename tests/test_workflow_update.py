from __future__ import annotations

import hashlib
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
UPDATE_SCRIPT = ROOT / "github-issue-workflow-update" / "scripts" / "update_workflow.py"
BUILD_SCRIPT = ROOT / "scripts" / "build_release.py"


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


UPDATE = load_module("workflow_update", UPDATE_SCRIPT)
BUILD = load_module("build_release", BUILD_SCRIPT)


class WorkflowUpdateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def release_asset(self) -> tuple[dict, bytes, bytes]:
        output = self.root / "dist"
        archive, checksum = BUILD.build("v0.1.0", "a" * 40, output)
        release = {
            "tag": "v0.1.0", "version": "0.1.0", "archive_name": archive.name,
            "archive_url": "https://example.test/archive",
            "checksum_url": "https://example.test/checksum",
        }
        return release, archive.read_bytes(), checksum.read_bytes()

    def legacy_skills(self) -> Path:
        skills = self.root / "skills"
        skills.mkdir()
        for name in UPDATE.MANAGED_SKILLS:
            target = skills / name
            target.mkdir()
            (target / "SKILL.md").write_text(f"local {name}\n", encoding="utf-8")
        (skills / "github-issue-workflow-update" / "VERSION").write_text("0.0.9\n")
        return skills

    def test_release_bundle_round_trip(self) -> None:
        release, archive, checksum = self.release_asset()
        expected = UPDATE.parse_checksum(checksum, release["archive_name"])
        self.assertEqual(hashlib.sha256(archive).hexdigest(), expected)
        bundle = UPDATE.unpack_and_validate(archive, self.root / "unpacked", release)
        self.assertEqual("0.1.0", (bundle / "github-issue-workflow-update" / "VERSION").read_text().strip())
        self.assertTrue((bundle / "github-issue-autopilot" / "scripts" / "autopilot_admin.py").is_file())

    def test_unpack_rejects_path_traversal(self) -> None:
        import gzip
        import io
        import tarfile

        buffer = io.BytesIO()
        with gzip.GzipFile(fileobj=buffer, mode="wb", mtime=0) as compressed:
            with tarfile.open(fileobj=compressed, mode="w") as archive:
                info = tarfile.TarInfo("../escape")
                info.size = 1
                archive.addfile(info, io.BytesIO(b"x"))
        release = {"version": "0.1.0", "tag": "v0.1.0"}
        with self.assertRaisesRegex(UPDATE.UpdateError, "unsafe archive path"):
            UPDATE.unpack_and_validate(buffer.getvalue(), self.root / "unpacked", release)

    def test_apply_overwrites_managed_skills_and_refreshes_runtimes(self) -> None:
        release, archive, checksum = self.release_asset()
        skills = self.legacy_skills()
        responses = [
            {"status": "ready", "installations": 2},
            {"status": "refreshed", "installations": 2},
        ]
        with mock.patch.object(UPDATE, "latest_release", return_value=release), mock.patch.object(
            UPDATE, "request_bytes", side_effect=[archive, checksum]
        ), mock.patch.object(UPDATE, "run_runtime_refresh", side_effect=responses):
            result = UPDATE.apply(skills)
        self.assertEqual("updated", result["status"])
        self.assertEqual(2, result["autopilot_refreshed"])
        self.assertEqual("0.1.0", UPDATE.installed_version(skills))
        self.assertNotIn("local github-issue-handoff",
                         (skills / "github-issue-handoff" / "SKILL.md").read_text())

    def test_apply_reinstalls_same_version_to_remove_local_drift(self) -> None:
        release, archive, checksum = self.release_asset()
        skills = self.legacy_skills()
        (skills / "github-issue-workflow-update" / "VERSION").write_text("0.1.0\n")
        with mock.patch.object(UPDATE, "latest_release", return_value=release), mock.patch.object(
            UPDATE, "request_bytes", side_effect=[archive, checksum]
        ), mock.patch.object(UPDATE, "run_runtime_refresh", side_effect=[
            {"status": "ready", "installations": 0},
            {"status": "refreshed", "installations": 0},
        ]):
            result = UPDATE.apply(skills)
        self.assertEqual("updated", result["status"])
        self.assertNotIn("local github-issue-handoff",
                         (skills / "github-issue-handoff" / "SKILL.md").read_text())

    def test_apply_restores_every_skill_when_runtime_refresh_fails(self) -> None:
        release, archive, checksum = self.release_asset()
        skills = self.legacy_skills()
        with mock.patch.object(UPDATE, "latest_release", return_value=release), mock.patch.object(
            UPDATE, "request_bytes", side_effect=[archive, checksum]
        ), mock.patch.object(UPDATE, "run_runtime_refresh", side_effect=[
            {"status": "ready", "installations": 1},
            UPDATE.UpdateError("runtime verification failed"),
        ]):
            with self.assertRaisesRegex(UPDATE.UpdateError, "rolled back"):
                UPDATE.apply(skills)
        self.assertEqual("0.0.9", UPDATE.installed_version(skills))
        for name in UPDATE.MANAGED_SKILLS:
            self.assertEqual(f"local {name}\n", (skills / name / "SKILL.md").read_text())

    def test_apply_refuses_to_replace_a_source_checkout(self) -> None:
        release, _archive, _checksum = self.release_asset()
        checkout = self.legacy_skills()
        (checkout / ".git").mkdir()
        with mock.patch.object(UPDATE, "latest_release", return_value=release), self.assertRaisesRegex(
            UPDATE.UpdateError, "source checkout"
        ):
            UPDATE.apply(checkout)

    def test_latest_release_requires_stable_assets(self) -> None:
        metadata = {"tag_name": "v0.1.0", "draft": False, "prerelease": True, "assets": []}
        with mock.patch.object(UPDATE, "request_bytes", return_value=json.dumps(metadata).encode()), self.assertRaisesRegex(
            UPDATE.UpdateError, "stable published release"
        ):
            UPDATE.latest_release()

    def test_latest_release_accepts_only_official_asset_urls(self) -> None:
        prefix = ("https://github.com/Niall-Young/github-issue-workflow/"
                  "releases/download/v0.1.0/")
        archive = "github-issue-workflow-v0.1.0.tar.gz"
        metadata = {
            "tag_name": "v0.1.0", "draft": False, "prerelease": False,
            "assets": [
                {"name": archive, "browser_download_url": prefix + archive},
                {"name": archive + ".sha256",
                 "browser_download_url": prefix + archive + ".sha256"},
            ],
        }
        with mock.patch.object(UPDATE, "request_bytes", return_value=json.dumps(metadata).encode()):
            result = UPDATE.latest_release()
        self.assertEqual("0.1.0", result["version"])

        metadata["assets"][0]["browser_download_url"] = "https://example.test/archive"
        with mock.patch.object(UPDATE, "request_bytes", return_value=json.dumps(metadata).encode()), self.assertRaisesRegex(
            UPDATE.UpdateError, "outside the official repository"
        ):
            UPDATE.latest_release()


if __name__ == "__main__":
    unittest.main()
