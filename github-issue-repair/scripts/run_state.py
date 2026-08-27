#!/usr/bin/env python3
"""Maintain an auditable, idempotent GitHub Issue repair run ledger."""

from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import json
import os
import re
import subprocess
import sys
import tempfile
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator


SCHEMA_VERSION = 1
PACKAGE_ID = re.compile(r"^[a-z0-9][a-z0-9-]*$")
STATES = {
    "INGEST",
    "TRIAGE",
    "AWAIT_SCOPE_APPROVAL",
    "PREPARE",
    "IMPLEMENT",
    "VERIFY",
    "REVIEW",
    "AWAIT_PUBLICATION_APPROVAL",
    "PUSH_AND_DRAFT_PR",
    "COMPLETE",
    "BLOCKED",
    "NEEDS_REPLAN",
    "NEEDS_HUMAN",
    "UNSAFE",
    "FAILED",
    "CANCELLED",
}
TRANSITIONS = {
    "INGEST": {"TRIAGE", "BLOCKED", "UNSAFE", "FAILED", "CANCELLED"},
    "TRIAGE": {"AWAIT_SCOPE_APPROVAL", "BLOCKED", "NEEDS_HUMAN", "UNSAFE", "FAILED", "CANCELLED"},
    "AWAIT_SCOPE_APPROVAL": {"PREPARE", "NEEDS_HUMAN", "CANCELLED"},
    "PREPARE": {"IMPLEMENT", "BLOCKED", "NEEDS_REPLAN", "UNSAFE", "FAILED", "CANCELLED"},
    "IMPLEMENT": {"VERIFY", "BLOCKED", "NEEDS_REPLAN", "UNSAFE", "FAILED", "CANCELLED"},
    "VERIFY": {"REVIEW", "IMPLEMENT", "BLOCKED", "NEEDS_REPLAN", "FAILED", "CANCELLED"},
    "REVIEW": {"AWAIT_PUBLICATION_APPROVAL", "IMPLEMENT", "BLOCKED", "NEEDS_REPLAN", "UNSAFE", "FAILED", "CANCELLED"},
    "AWAIT_PUBLICATION_APPROVAL": {"PUSH_AND_DRAFT_PR", "NEEDS_HUMAN", "CANCELLED"},
    "PUSH_AND_DRAFT_PR": {"COMPLETE", "BLOCKED", "FAILED"},
    "BLOCKED": {"TRIAGE", "PREPARE", "IMPLEMENT", "VERIFY", "REVIEW", "CANCELLED"},
    "NEEDS_REPLAN": {"TRIAGE", "CANCELLED"},
    "NEEDS_HUMAN": {"TRIAGE", "AWAIT_SCOPE_APPROVAL", "AWAIT_PUBLICATION_APPROVAL", "CANCELLED"},
    "FAILED": {"PREPARE", "IMPLEMENT", "VERIFY", "CANCELLED"},
    "UNSAFE": set(),
    "CANCELLED": set(),
    "COMPLETE": set(),
}
PACKAGE_TRANSITIONS = {
    "PROPOSED": {"APPROVED", "BLOCKED", "CANCELLED"},
    "APPROVED": {"PREPARING", "BLOCKED", "CANCELLED"},
    "PREPARING": {"IMPLEMENTING", "BLOCKED", "FAILED", "CANCELLED"},
    "IMPLEMENTING": {"VERIFYING", "BLOCKED", "FAILED", "CANCELLED"},
    "VERIFYING": {"REVIEW_READY", "IMPLEMENTING", "BLOCKED", "FAILED", "CANCELLED"},
    "REVIEW_READY": {"PUBLISH_APPROVED", "IMPLEMENTING", "BLOCKED", "FAILED", "CANCELLED"},
    "PUBLISH_APPROVED": {"PUBLISHED", "BLOCKED", "FAILED", "CANCELLED"},
    "PUBLISHED": set(),
    "BLOCKED": {"PREPARING", "IMPLEMENTING", "VERIFYING", "REVIEW_READY", "CANCELLED"},
    "FAILED": {"PREPARING", "IMPLEMENTING", "VERIFYING", "CANCELLED"},
    "CANCELLED": set(),
}


class LedgerError(RuntimeError):
    pass


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if result.returncode:
        raise LedgerError(result.stderr.strip() or "Git command failed")
    return result.stdout.strip()


def repository_root(repo: Path) -> Path:
    return Path(git(repo, "rev-parse", "--show-toplevel")).resolve()


def ledger_root(repo: Path) -> Path:
    root = repository_root(repo)
    common = Path(git(root, "rev-parse", "--git-common-dir"))
    if not common.is_absolute():
        common = root / common
    return common.resolve() / "issue-repair" / "runs"


def state_path(repo: Path, run_id: str) -> Path:
    return ledger_root(repo) / run_id / "state.json"


def validate_run_id(run_id: str) -> None:
    try:
        uuid.UUID(run_id)
    except ValueError as exc:
        raise LedgerError("run-id must be a UUID") from exc


@contextmanager
def locked(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def load(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise LedgerError(f"run not found: {path.parent.name}") from exc
    except json.JSONDecodeError as exc:
        raise LedgerError(f"invalid ledger JSON: {exc}") from exc


def write_atomic(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix="state.", suffix=".json", dir=path.parent)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(state, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def event(state: dict[str, Any], kind: str, **fields: Any) -> None:
    state["updated_at"] = now()
    state["events"].append({"at": state["updated_at"], "kind": kind, **fields})


def validate_plan(plan: Any) -> list[dict[str, Any]]:
    if not isinstance(plan, dict) or not isinstance(plan.get("packages"), list) or not plan["packages"]:
        raise LedgerError("plan must contain a non-empty packages array")
    packages = plan["packages"]
    ids: set[str] = set()
    for package in packages:
        if not isinstance(package, dict):
            raise LedgerError("every package must be an object")
        package_id = package.get("id")
        if not isinstance(package_id, str) or not PACKAGE_ID.fullmatch(package_id):
            raise LedgerError("package id must use lowercase letters, digits, and hyphens")
        if package_id in ids:
            raise LedgerError(f"duplicate package id: {package_id}")
        ids.add(package_id)
        if package.get("status") != "PROPOSED":
            raise LedgerError(f"{package_id}: initial status must be PROPOSED")
        if package.get("risk") not in {"low", "medium", "high"}:
            raise LedgerError(f"{package_id}: risk must be low, medium, or high")
        for field in ("title", "acceptance_criteria", "verification"):
            value = package.get(field)
            if field == "title" and (not isinstance(value, str) or not value.strip()):
                raise LedgerError(f"{package_id}: title is required")
            if field != "title" and (not isinstance(value, list) or not value):
                raise LedgerError(f"{package_id}: {field} must be a non-empty array")
        if not isinstance(package.get("depends_on", []), list):
            raise LedgerError(f"{package_id}: depends_on must be an array")
    graph: dict[str, list[str]] = {}
    for package in packages:
        package_id = package["id"]
        dependencies = package.get("depends_on", [])
        if package_id in dependencies:
            raise LedgerError(f"{package_id}: self-dependency is not allowed")
        missing = [dependency for dependency in dependencies if dependency not in ids]
        if missing:
            raise LedgerError(f"{package_id}: unknown dependencies: {', '.join(missing)}")
        graph[package_id] = dependencies
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(node: str) -> None:
        if node in visiting:
            raise LedgerError("package dependency cycle detected")
        if node in visited:
            return
        visiting.add(node)
        for dependency in graph[node]:
            visit(dependency)
        visiting.remove(node)
        visited.add(node)

    for package_id in graph:
        visit(package_id)
    return packages


def mutate(repo: Path, run_id: str, operation: Any) -> dict[str, Any]:
    validate_run_id(run_id)
    path = state_path(repo, run_id)
    with locked(path.parent / ".lock"):
        state = load(path)
        operation(state)
        write_atomic(path, state)
    return state


def command_init(args: argparse.Namespace) -> dict[str, Any]:
    repo = repository_root(args.repo)
    base_sha = git(repo, "rev-parse", "--verify", f"{args.base_sha}^{{commit}}")
    run_id = args.run_id or str(uuid.uuid4())
    validate_run_id(run_id)
    path = state_path(repo, run_id)
    with locked(path.parent / ".lock"):
        if path.exists():
            state = load(path)
            expected = (str(repo), args.source_url, base_sha)
            actual = (state["repository"], state["source_url"], state["base_sha"])
            if actual != expected:
                raise LedgerError("run-id already exists with different input")
            return state
        timestamp = now()
        state = {
            "schema_version": SCHEMA_VERSION,
            "run_id": run_id,
            "repository": str(repo),
            "source_url": args.source_url,
            "base_sha": base_sha,
            "state": "INGEST",
            "plan": None,
            "approvals": {},
            "receipts": {},
            "created_at": timestamp,
            "updated_at": timestamp,
            "events": [{"at": timestamp, "kind": "initialized", "state": "INGEST"}],
        }
        write_atomic(path, state)
    return state


def command_plan(args: argparse.Namespace) -> dict[str, Any]:
    plan = json.loads(args.file.read_text(encoding="utf-8"))
    packages = validate_plan(plan)

    def operation(state: dict[str, Any]) -> None:
        if state["state"] not in {"INGEST", "TRIAGE", "NEEDS_REPLAN"}:
            raise LedgerError("plan can only be registered during intake or triage")
        if state["plan"] is not None and state["plan"] != {"packages": packages}:
            if state["state"] != "NEEDS_REPLAN":
                raise LedgerError("a different plan is already registered; transition to NEEDS_REPLAN first")
        state["plan"] = {"packages": packages}
        event(state, "plan_registered", package_ids=[package["id"] for package in packages])

    return mutate(args.repo, args.run_id, operation)


def command_approve(args: argparse.Namespace) -> dict[str, Any]:
    required_state = {"scope": "AWAIT_SCOPE_APPROVAL", "publication": "AWAIT_PUBLICATION_APPROVAL"}[args.kind]

    def operation(state: dict[str, Any]) -> None:
        if state["state"] != required_state:
            raise LedgerError(f"{args.kind} approval requires state {required_state}")
        approval = {"actor": args.actor, "at": now(), "note": args.note}
        existing = state["approvals"].get(args.kind)
        if existing:
            comparable = {"actor": existing["actor"], "note": existing["note"]}
            if comparable != {"actor": args.actor, "note": args.note}:
                raise LedgerError(f"conflicting {args.kind} approval already recorded")
            return
        state["approvals"][args.kind] = approval
        event(state, "approval_recorded", approval_kind=args.kind, actor=args.actor)

    return mutate(args.repo, args.run_id, operation)


def command_transition(args: argparse.Namespace) -> dict[str, Any]:
    def operation(state: dict[str, Any]) -> None:
        current = state["state"]
        if args.to == current:
            return
        if args.to not in TRANSITIONS[current]:
            raise LedgerError(f"invalid transition: {current} -> {args.to}")
        if args.to == "PREPARE" and "scope" not in state["approvals"]:
            raise LedgerError("scope approval is required before PREPARE")
        if args.to == "PUSH_AND_DRAFT_PR" and "publication" not in state["approvals"]:
            raise LedgerError("publication approval is required before remote writes")
        state["state"] = args.to
        event(state, "transition", previous=current, next_state=args.to, note=args.note)

    return mutate(args.repo, args.run_id, operation)


def command_package(args: argparse.Namespace) -> dict[str, Any]:
    def operation(state: dict[str, Any]) -> None:
        if state["plan"] is None:
            raise LedgerError("register a plan before updating a package")
        package = next((item for item in state["plan"]["packages"] if item["id"] == args.package_id), None)
        if package is None:
            raise LedgerError(f"unknown package: {args.package_id}")
        current = package["status"]
        if args.to == current:
            return
        if args.to not in PACKAGE_TRANSITIONS[current]:
            raise LedgerError(f"invalid package transition: {current} -> {args.to}")
        if args.to == "APPROVED" and "scope" not in state["approvals"]:
            raise LedgerError("scope approval is required before approving a package")
        if args.to == "PUBLISH_APPROVED" and "publication" not in state["approvals"]:
            raise LedgerError("publication approval is required before approving package publication")
        package["status"] = args.to
        event(
            state,
            "package_transition",
            package_id=args.package_id,
            previous=current,
            next_status=args.to,
            note=args.note,
        )

    return mutate(args.repo, args.run_id, operation)


def command_receipt(args: argparse.Namespace) -> dict[str, Any]:
    def operation(state: dict[str, Any]) -> None:
        if args.kind in {"push", "draft-pr"} and state["state"] != "PUSH_AND_DRAFT_PR":
            raise LedgerError("remote receipts require state PUSH_AND_DRAFT_PR")
        receipt = {"kind": args.kind, "value": args.value, "at": now()}
        existing = state["receipts"].get(args.key)
        if existing:
            if {"kind": existing["kind"], "value": existing["value"]} != {"kind": args.kind, "value": args.value}:
                raise LedgerError(f"conflicting receipt key: {args.key}")
            return
        state["receipts"][args.key] = receipt
        event(state, "receipt_recorded", key=args.key, receipt_kind=args.kind)

    return mutate(args.repo, args.run_id, operation)


def command_show(args: argparse.Namespace) -> dict[str, Any]:
    validate_run_id(args.run_id)
    return load(state_path(args.repo, args.run_id))


def parser() -> argparse.ArgumentParser:
    main = argparse.ArgumentParser(description=__doc__)
    commands = main.add_subparsers(dest="command", required=True)

    init = commands.add_parser("init", help="create or read an idempotent run")
    init.add_argument("--repo", type=Path, default=Path.cwd())
    init.add_argument("--source-url", required=True)
    init.add_argument("--base-sha", required=True)
    init.add_argument("--run-id")
    init.set_defaults(handler=command_init)

    plan = commands.add_parser("plan", help="register a validated work-package plan")
    plan.add_argument("--repo", type=Path, default=Path.cwd())
    plan.add_argument("--run-id", required=True)
    plan.add_argument("--file", type=Path, required=True)
    plan.set_defaults(handler=command_plan)

    approve = commands.add_parser("approve", help="record a scope or publication approval")
    approve.add_argument("--repo", type=Path, default=Path.cwd())
    approve.add_argument("--run-id", required=True)
    approve.add_argument("--kind", choices=("scope", "publication"), required=True)
    approve.add_argument("--actor", required=True)
    approve.add_argument("--note", default="")
    approve.set_defaults(handler=command_approve)

    transition = commands.add_parser("transition", help="apply a validated state transition")
    transition.add_argument("--repo", type=Path, default=Path.cwd())
    transition.add_argument("--run-id", required=True)
    transition.add_argument("--to", choices=sorted(STATES), required=True)
    transition.add_argument("--note", default="")
    transition.set_defaults(handler=command_transition)

    package = commands.add_parser("package", help="apply a validated work-package transition")
    package.add_argument("--repo", type=Path, default=Path.cwd())
    package.add_argument("--run-id", required=True)
    package.add_argument("--package-id", required=True)
    package.add_argument("--to", choices=sorted(PACKAGE_TRANSITIONS), required=True)
    package.add_argument("--note", default="")
    package.set_defaults(handler=command_package)

    receipt = commands.add_parser("receipt", help="record an idempotent artifact or remote receipt")
    receipt.add_argument("--repo", type=Path, default=Path.cwd())
    receipt.add_argument("--run-id", required=True)
    receipt.add_argument("--kind", choices=("branch", "commit", "push", "draft-pr", "evidence"), required=True)
    receipt.add_argument("--key", required=True)
    receipt.add_argument("--value", required=True)
    receipt.set_defaults(handler=command_receipt)

    show = commands.add_parser("show", help="print a run ledger")
    show.add_argument("--repo", type=Path, default=Path.cwd())
    show.add_argument("--run-id", required=True)
    show.set_defaults(handler=command_show)
    return main


def main() -> int:
    args = parser().parse_args()
    try:
        state = args.handler(args)
    except (LedgerError, OSError, json.JSONDecodeError) as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    print(json.dumps({"status": "ok", "run": state}, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
