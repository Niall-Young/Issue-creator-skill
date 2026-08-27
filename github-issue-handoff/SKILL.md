---
name: github-issue-handoff
description: Turn a GitHub repository link and optional task description into a Chinese, agent-ready GitHub Issue handoff. Use when the user wants to create or submit an Issue that another agent can execute; do not trigger for ordinary GitHub browsing, link discussion, or implementing an existing Issue.
---

# GitHub Issue Handoff

Create an Issue that a new agent can execute using only the Issue and the repository. The Issue is a task handoff, not a loose reminder: it must explain the outcome, verified repository context, boundaries, acceptance criteria, and validation.

## Authorization

- An explicit invocation such as `$github-issue-handoff <GitHub URL>` authorizes creating one ready Issue. An explicit request to create, submit, or file an Issue does the same.
- If the skill was selected implicitly, require clear Issue-creation intent. A GitHub link by itself outside an explicit skill invocation is not authorization.
- If the user asks for a draft or preview, do not create the Issue.
- One request containing multiple independently deliverable tasks requires split confirmation before creating multiple Issues.
- Do not add or create labels, assignees, milestones, or projects unless the user explicitly requests them.

## Prepare the task

1. Extract the repository from the supplied GitHub URL. For repository subpaths such as `blob`, `tree`, `commit`, `pull`, or `issues`, the first two path components after the host are the owner and repository. Remove a terminal `.git` suffix. Validate the result with `gh repo view` rather than trusting string parsing.
2. Check `gh auth status` and read repository metadata, including the canonical name, default branch, archive state, Issues availability, viewer permission, Issue templates, and Security Policy. Never reveal credentials or token details in the response.
3. Establish the requested outcome from the user's current message and relevant conversation. Do not infer a task merely because a repository was supplied. If no actionable outcome is present, ask only the single most important question: what should change or be investigated?
4. Inspect enough of the repository to make the handoff executable:
   - Prefer the current checkout when its remote matches the target repository.
   - Otherwise read metadata and files through `gh`; use a shallow clone in a task-specific temporary directory only when code inspection materially improves the Issue.
   - Check the README, contribution guidance, relevant code/configuration, existing tests, and user-supplied links. Record only facts actually verified.
5. Classify the primary deliverable as `Feature`, `Bug`, `Refactor`, or `Research`, then read [references/issue-templates.md](references/issue-templates.md) and use that template. Treat repository-provided Issue templates as supplemental requirements; they must not replace the skill's standard handoff headings.

All Issue titles and prose must be Chinese, while code, identifiers, commands, paths, logs, and error messages remain in their original form. Prefix the title with `[Feature]`, `[Bug]`, `[Refactor]`, or `[Research]`.

## Keep the task atomic

An Issue should describe one independently verifiable outcome. If the request contains work that could be assigned to different agents without requiring a shared implementation context, propose an ordered list of atomic Issue titles and wait for confirmation before creating any of them. Keep tightly coupled implementation and tests together.

## Readiness gate

Create an Issue only when all applicable statements are true:

- The desired outcome and reason for the work are explicit.
- Relevant repository entry points are identified, or the Issue explains why discovery is itself the task.
- In-scope work and meaningful non-goals are clear.
- Acceptance criteria are observable rather than subjective.
- Validation commands or concrete manual checks are supplied when the repository makes them discoverable.
- Claims about current behavior are supported by the repository, user evidence, or clearly marked as `待确认`.

If a critical field cannot be derived, ask one focused question at a time. Do not fabricate reproduction steps, environments, logs, file paths, root causes, or implementation constraints. Do not turn optional implementation ideas into requirements.

## Prevent unsafe or duplicate Issues

- Search both open and closed Issues using the component, behavior, and distinctive terms from the proposed title. Read plausible matches. A high-confidence duplicate has the same underlying outcome or failure, not merely shared keywords.
- For a high-confidence duplicate, stop and return the existing Issue URL with a short explanation. Create a new one only if the user explicitly insists after seeing the match.
- If the report may expose a vulnerability, secret, authentication bypass, or exploitable weakness, do not create a public Issue. Read the Security Policy and return its private reporting route.
- Stop with a clear explanation if the repository is archived, Issues are disabled, access is unavailable, or repository guidance directs the user to another channel.

## Create and verify

For a ready single task, create the Issue without an additional preview step. Pass the exact Markdown body through stdin or a safely created temporary file to `gh issue create`; never interpolate Issue content into executable shell syntax.

Treat creation as successful only after obtaining the Issue URL and reading the same remote Issue back with `gh issue view`. Verify the repository, title, and body are complete, then return a concise result with the final title and clickable URL.

If creation fails without a reliable receipt, search recent Issues by the proposed title and current author before retrying. If an exact recent match exists, read it back and use that URL. Otherwise report the failure and do not blindly retry.
