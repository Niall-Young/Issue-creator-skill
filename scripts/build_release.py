#!/usr/bin/env python3
"""Build a deterministic GitHub Issue Workflow release bundle."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import os
import re
import tarfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PRODUCT = "github-issue-workflow"
MANAGED_SKILLS = (
    "github-issue-handoff",
    "github-issue-repair",
    "github-issue-autopilot",
    "github-issue-workflow-update",
)
TAG_PATTERN = re.compile(r"^v(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")


def release_files() -> dict[str, Path]:
    result: dict[str, Path] = {}
    for skill in MANAGED_SKILLS:
        root = ROOT / skill
        if not (root / "SKILL.md").is_file():
            raise SystemExit(f"missing {skill}/SKILL.md")
        for path in sorted(root.rglob("*")):
            if path.is_file() and "__pycache__" not in path.parts and path.suffix not in {".pyc", ".pyo"}:
                result[path.relative_to(ROOT).as_posix()] = path
    return result


def build(tag: str, commit: str, output: Path) -> tuple[Path, Path]:
    match = TAG_PATTERN.fullmatch(tag)
    if not match:
        raise SystemExit("tag must use vMAJOR.MINOR.PATCH")
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise SystemExit("commit must be a full lowercase Git SHA")
    version = ".".join(match.groups())
    source_version = (ROOT / "github-issue-workflow-update" / "VERSION").read_text().strip()
    if source_version != version:
        raise SystemExit(f"tag {tag} does not match updater VERSION {source_version}")
    files = release_files()
    manifest = {
        "schema_version": 1,
        "product": PRODUCT,
        "version": version,
        "tag": tag,
        "commit": commit,
        "managed_skills": list(MANAGED_SKILLS),
        "files": {relative: hashlib.sha256(path.read_bytes()).hexdigest()
                  for relative, path in files.items()},
    }
    root_name = f"{PRODUCT}-{tag}"
    output.mkdir(parents=True, exist_ok=True)
    archive = output / f"{root_name}.tar.gz"
    timestamp = int(os.environ.get("SOURCE_DATE_EPOCH", "0"))
    with archive.open("wb") as raw:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=timestamp) as compressed:
            with tarfile.open(fileobj=compressed, mode="w") as tar:
                manifest_data = (json.dumps(manifest, ensure_ascii=False, indent=2,
                                            sort_keys=True) + "\n").encode()
                entries: list[tuple[str, bytes, int]] = [
                    (f"{root_name}/release-manifest.json", manifest_data, 0o644)
                ]
                entries.extend((f"{root_name}/{relative}", path.read_bytes(),
                                0o755 if os.access(path, os.X_OK) else 0o644)
                               for relative, path in files.items())
                for name, data, mode in sorted(entries):
                    info = tarfile.TarInfo(name)
                    info.size = len(data)
                    info.mode = mode
                    info.mtime = timestamp
                    info.uid = info.gid = 0
                    info.uname = info.gname = ""
                    tar.addfile(info, io.BytesIO(data))
    checksum = output / f"{archive.name}.sha256"
    checksum.write_text(f"{hashlib.sha256(archive.read_bytes()).hexdigest()}  {archive.name}\n")
    return archive, checksum


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--output", type=Path, default=Path("dist"))
    args = parser.parse_args()
    archive, checksum = build(args.tag, args.commit, args.output)
    print(json.dumps({"archive": str(archive), "checksum": str(checksum)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
