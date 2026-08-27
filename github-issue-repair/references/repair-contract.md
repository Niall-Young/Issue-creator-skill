# Repair contract

Use these structures for plans, evidence, and PR handoff. Keep facts, inferences, and user approvals distinguishable.

## Work-package plan

Register a JSON object with a `packages` array. Each package has:

```json
{
  "id": "pkg-1",
  "title": "Observable outcome",
  "issue_urls": ["https://github.com/owner/repo/issues/123"],
  "status": "PROPOSED",
  "risk": "low",
  "depends_on": [],
  "relationships": [],
  "predicted_paths": ["src/component/"],
  "acceptance_criteria": ["Observable result"],
  "verification": ["command or concrete manual check"]
}
```

Requirements:

- `id` is unique within the run and uses lowercase letters, digits, and hyphens.
- `status` starts as `PROPOSED`; update it through `run_state.py package`, never by editing the ledger.
- `risk` is `low`, `medium`, or `high`; only approved low-risk packages are parallel candidates.
- Every `depends_on` value resolves to another package in the same plan, without self-dependencies or cycles.
- `relationships` entries use `blocked-by`, `duplicate-of`, `same-root-cause`, or `conflicts-with` and identify the related package or Issue.
- Predicted paths are advisory. Acceptance criteria and verification entries must be observable and non-empty.

## Evidence package

Produce one evidence package per implemented work package:

```markdown
## Work package

- ID:
- Linked Issues:
- Base SHA:
- Head SHA:
- Branch:

## Change

- Root cause or rationale:
- Changed files:
- Scope deviations:

## Acceptance evidence

| Criterion | Evidence | Result |
| --- | --- | --- |

## Verification

| Command or check | Base result | Branch result | Exit code |
| --- | --- | --- | --- |

## Review

- Independent reviewer:
- Anti-gaming checks:
- Skipped checks and reason:
- Residual risks:
```

Do not mark a package verified when a criterion lacks evidence, the relevant baseline cannot be distinguished, or the independent review found unresolved scope drift.

## Failure classes

- `transient`: tool timeout, temporary network failure, or unavailable runner; retry only within budget.
- `needs-replan`: newly discovered dependency, overlap, or invalid verification contract.
- `needs-human`: product ambiguity, missing authority, or materially uncertain trade-off.
- `unsafe`: secret exposure, destructive action, security-sensitive scope, or policy conflict.
- `verification-failed`: the candidate does not meet its contract.
- `integration-conflict`: individually valid packages fail when combined.

Every failure record includes the package, state, evidence, attempted commands, retry count, and recommended next action.

## Draft-PR handoff

The draft PR includes:

1. Linked Issues using neutral references such as `Relates to #123` unless closing is separately approved.
2. Base/head SHAs and package dependencies.
3. Root cause and implementation summary.
4. Acceptance-criteria mapping and exact verification evidence.
5. Skipped checks, residual risks, and explicit agent provenance.
