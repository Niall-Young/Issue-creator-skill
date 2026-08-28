#!/usr/bin/env python3
"""Check and transactionally install the latest stable GitHub Issue Workflow release."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.error
import urllib.request
from pathlib import Path, PurePosixPath
from typing import Any


REPOSITORY = "Niall-Young/github-issue-workflow"
RELEASE_API = f"https://api.github.com/repos/{REPOSITORY}/releases/latest"
PRODUCT = "github-issue-workflow"
MANAGED_SKILLS = (
    "github-issue-handoff",
    "github-issue-repair",
    "github-issue-autopilot",
    "github-issue-workflow-update",
)
VERSION_PATTERN = re.compile(r"^(?:v)?(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
MAX_DOWNLOAD_BYTES = 50 * 1024 * 1024
MAX_UNPACKED_BYTES = 100 * 1024 * 1024
MAX_ARCHIVE_MEMBERS = 5000


class UpdateError(RuntimeError):
    def __init__(self, message: str, partial_update: bool = False) -> None:
        super().__init__(message)
        self.partial_update = partial_update


def version_tuple(value: str) -> tuple[int, int, int]:
    match = VERSION_PATTERN.fullmatch(value.strip())
    if not match:
        raise UpdateError(f"invalid semantic version: {value}")
    return tuple(int(part) for part in match.groups())  # type: ignore[return-value]


def version_text(value: str) -> str:
    major, minor, patch = version_tuple(value)
    return f"{major}.{minor}.{patch}"


def request_bytes(url: str, accept: str = "application/octet-stream") -> bytes:
    headers = {"Accept": accept, "User-Agent": "github-issue-workflow-update/1"}
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            length = response.headers.get("Content-Length")
            if length and int(length) > MAX_DOWNLOAD_BYTES:
                raise UpdateError("release asset exceeds the download limit")
            data = response.read(MAX_DOWNLOAD_BYTES + 1)
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        raise UpdateError(f"cannot download {url}: {exc}") from exc
    if len(data) > MAX_DOWNLOAD_BYTES:
        raise UpdateError("release asset exceeds the download limit")
    return data


def latest_release() -> dict[str, Any]:
    try:
        value = json.loads(request_bytes(RELEASE_API, "application/vnd.github+json"))
    except json.JSONDecodeError as exc:
        raise UpdateError("GitHub returned invalid release metadata") from exc
    if not isinstance(value, dict) or value.get("draft") or value.get("prerelease"):
        raise UpdateError("latest GitHub release is not a stable published release")
    tag = value.get("tag_name")
    version = version_text(str(tag))
    assets = value.get("assets")
    if not isinstance(assets, list):
        raise UpdateError("release metadata has no assets")
    archive_name = f"{PRODUCT}-v{version}.tar.gz"
    checksum_name = f"{archive_name}.sha256"
    urls = {
        item.get("name"): item.get("browser_download_url")
        for item in assets
        if isinstance(item, dict)
    }
    if not isinstance(urls.get(archive_name), str) or not isinstance(urls.get(checksum_name), str):
        raise UpdateError("release is missing the workflow archive or checksum")
    asset_prefix = f"https://github.com/{REPOSITORY}/releases/download/v{version}/"
    if not urls[archive_name].startswith(asset_prefix) or not urls[checksum_name].startswith(asset_prefix):
        raise UpdateError("release assets are outside the official repository")
    return {
        "tag": f"v{version}",
        "version": version,
        "archive_name": archive_name,
        "archive_url": urls[archive_name],
        "checksum_url": urls[checksum_name],
    }


def parse_checksum(data: bytes, archive_name: str) -> str:
    try:
        line = data.decode("utf-8").strip()
    except UnicodeDecodeError as exc:
        raise UpdateError("release checksum is not UTF-8") from exc
    match = re.fullmatch(r"([0-9a-fA-F]{64})\s+\*?([^\s]+)", line)
    if not match or match.group(2) != archive_name:
        raise UpdateError("release checksum file is malformed")
    return match.group(1).lower()


def safe_member_path(name: str) -> PurePosixPath:
    path = PurePosixPath(name)
    if path.is_absolute() or not path.parts or any(part in {"", ".", ".."} for part in path.parts):
        raise UpdateError(f"unsafe archive path: {name}")
    return path


def unpack_and_validate(archive: bytes, destination: Path, release: dict[str, Any]) -> Path:
    try:
        opened = tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz")
    except tarfile.TarError as exc:
        raise UpdateError("release archive is invalid") from exc
    with opened:
        members = opened.getmembers()
        if not members:
            raise UpdateError("release archive is empty")
        if len(members) > MAX_ARCHIVE_MEMBERS:
            raise UpdateError("release archive contains too many entries")
        if sum(member.size for member in members if member.isfile()) > MAX_UNPACKED_BYTES:
            raise UpdateError("release archive exceeds the unpacked size limit")
        roots: set[str] = set()
        names: set[str] = set()
        for member in members:
            path = safe_member_path(member.name)
            if member.name in names:
                raise UpdateError(f"duplicate archive entry: {member.name}")
            names.add(member.name)
            roots.add(path.parts[0])
            if not (member.isdir() or member.isfile()) or member.issym() or member.islnk():
                raise UpdateError(f"unsupported archive entry: {member.name}")
        expected_root = f"{PRODUCT}-v{release['version']}"
        if roots != {expected_root}:
            raise UpdateError("release archive has an unexpected root directory")
        for member in members:
            relative = safe_member_path(member.name)
            target = destination.joinpath(*relative.parts)
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            source = opened.extractfile(member)
            if source is None:
                raise UpdateError(f"cannot read archive entry: {member.name}")
            target.write_bytes(source.read())

    bundle = destination / f"{PRODUCT}-v{release['version']}"
    manifest_path = bundle / "release-manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise UpdateError("release manifest is missing or invalid") from exc
    if manifest.get("schema_version") != 1 or manifest.get("product") != PRODUCT:
        raise UpdateError("release manifest identity is invalid")
    if not re.fullmatch(r"[0-9a-f]{40}", str(manifest.get("commit", ""))):
        raise UpdateError("release manifest commit is invalid")
    if version_text(str(manifest.get("version"))) != release["version"]:
        raise UpdateError("release tag and manifest version do not match")
    if manifest.get("tag") != release["tag"]:
        raise UpdateError("release manifest tag does not match")
    if tuple(manifest.get("managed_skills", [])) != MANAGED_SKILLS:
        raise UpdateError("release manifest skill allowlist does not match")
    expected_top = {*MANAGED_SKILLS, "release-manifest.json"}
    if {path.name for path in bundle.iterdir()} != expected_top:
        raise UpdateError("release archive contains unexpected top-level files")
    hashes = manifest.get("files")
    if not isinstance(hashes, dict):
        raise UpdateError("release manifest file hashes are invalid")
    actual_files = {
        path.relative_to(bundle).as_posix(): path
        for skill in MANAGED_SKILLS
        for path in (bundle / skill).rglob("*")
        if path.is_file()
    }
    if set(hashes) != set(actual_files):
        raise UpdateError("release manifest file list does not match the archive")
    for relative, path in actual_files.items():
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        if hashes[relative] != actual:
            raise UpdateError(f"release file hash mismatch: {relative}")
    for skill in MANAGED_SKILLS:
        if not (bundle / skill / "SKILL.md").is_file():
            raise UpdateError(f"release is missing {skill}/SKILL.md")
    installed_version = (bundle / "github-issue-workflow-update" / "VERSION").read_text().strip()
    if version_text(installed_version) != release["version"]:
        raise UpdateError("updater VERSION does not match the release")
    return bundle


def installed_version(skills_root: Path) -> str | None:
    path = skills_root / "github-issue-workflow-update" / "VERSION"
    if not path.is_file():
        return None
    try:
        return version_text(path.read_text(encoding="utf-8"))
    except (OSError, UpdateError):
        return None


def run_runtime_refresh(admin: Path, dry_run: bool) -> dict[str, Any]:
    argv = [sys.executable, str(admin), "refresh-runtimes", "--all"]
    if dry_run:
        argv.append("--dry-run")
    result = subprocess.run(argv, check=False, text=True, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE)
    raw = result.stdout.strip() or result.stderr.strip()
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise UpdateError(raw or "Autopilot runtime refresh returned invalid JSON") from exc
    if result.returncode or value.get("status") == "error":
        message = value.get("error") or "Autopilot runtime refresh failed"
        raise UpdateError(message, partial_update="rollback errors:" in message)
    return value


def swap_skills(skills_root: Path, bundle: Path, transaction: Path) -> dict[str, Path | None]:
    backups: dict[str, Path | None] = {}
    backup_root = transaction / "previous"
    backup_root.mkdir()
    try:
        for skill in MANAGED_SKILLS:
            target = skills_root / skill
            backup = backup_root / skill
            if target.exists():
                os.replace(target, backup)
                backups[skill] = backup
            else:
                backups[skill] = None
            os.replace(bundle / skill, target)
    except OSError as exc:
        rollback_skills(skills_root, backups)
        raise UpdateError(f"skill installation failed: {exc}") from exc
    return backups


def rollback_skills(skills_root: Path, backups: dict[str, Path | None]) -> None:
    errors: list[str] = []
    for skill in reversed(tuple(backups)):
        target = skills_root / skill
        backup = backups.get(skill)
        try:
            if target.exists():
                shutil.rmtree(target)
            if backup is not None and backup.exists():
                os.replace(backup, target)
        except OSError as exc:
            errors.append(f"{skill}: {exc}")
    if errors:
        raise UpdateError("skill rollback failed: " + "; ".join(errors), partial_update=True)


def check(skills_root: Path) -> dict[str, Any]:
    release = latest_release()
    current = installed_version(skills_root)
    available = current is None or version_tuple(release["version"]) > version_tuple(current)
    return {"status": "update-available" if available else "up-to-date",
            "installed_version": current, "latest_version": release["version"],
            "source": REPOSITORY}


def apply(skills_root: Path) -> dict[str, Any]:
    release = latest_release()
    current = installed_version(skills_root)
    if current is not None:
        current_tuple = version_tuple(current)
        latest_tuple = version_tuple(release["version"])
        if current_tuple > latest_tuple:
            raise UpdateError("refusing to downgrade a newer installed version")
    skills_root = skills_root.resolve()
    if not skills_root.is_dir():
        raise UpdateError("current Agent skill root does not exist")
    if (skills_root / ".git").exists():
        raise UpdateError("refusing to update a source checkout; install the updater in an Agent skill root")
    with tempfile.TemporaryDirectory(prefix=".issue-workflow-update-", dir=skills_root.parent) as raw:
        transaction = Path(raw)
        archive = request_bytes(release["archive_url"])
        expected = parse_checksum(request_bytes(release["checksum_url"]), release["archive_name"])
        actual = hashlib.sha256(archive).hexdigest()
        if actual != expected:
            raise UpdateError("release archive checksum mismatch")
        bundle = unpack_and_validate(archive, transaction / "staged", release)
        staged_admin = bundle / "github-issue-autopilot" / "scripts" / "autopilot_admin.py"
        preflight = run_runtime_refresh(staged_admin, dry_run=True)
        backups: dict[str, Path | None] = {}
        try:
            backups = swap_skills(skills_root, bundle, transaction)
            installed_admin = skills_root / "github-issue-autopilot" / "scripts" / "autopilot_admin.py"
            refreshed = run_runtime_refresh(installed_admin, dry_run=False)
        except (OSError, UpdateError) as exc:
            if backups:
                try:
                    rollback_skills(skills_root, backups)
                except UpdateError as rollback_exc:
                    raise UpdateError(f"update failed: {exc}; {rollback_exc}",
                                      partial_update=True) from rollback_exc
            raise UpdateError(f"update rolled back: {exc}",
                              partial_update=getattr(exc, "partial_update", False)) from exc
    return {"status": "updated", "previous_version": current,
            "installed_version": release["version"],
            "autopilot_preflight": preflight.get("installations", 0),
            "autopilot_refreshed": refreshed.get("installations", 0),
            "restart_conversation_recommended": True}


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description=__doc__)
    value.add_argument("operation", choices=("check", "apply"))
    value.add_argument("--skills-root", type=Path,
                       default=Path(__file__).resolve().parents[2],
                       help=argparse.SUPPRESS)
    return value


def main() -> int:
    args = parser().parse_args()
    try:
        result = check(args.skills_root) if args.operation == "check" else apply(args.skills_root)
    except UpdateError as exc:
        status = ("partial-update" if exc.partial_update else
                  "rolled-back" if str(exc).startswith("update rolled back:") else "blocked")
        result = {"status": status, "error": str(exc),
                  "partial_update": exc.partial_update}
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True), file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
