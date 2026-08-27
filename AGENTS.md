# Project Agent Instructions

## Mandatory workflow for code changes

Any task that creates, modifies, renames, or deletes code or another implementation-related project file must use both `$gitwork` and `$good-readme`.

Implementation-related files include source code, configuration, dependencies, scripts, tests, assets, build files, CI/CD workflows, migrations, and other files that can affect project behavior or delivery.

For every qualifying task:

1. Load and read the complete `$gitwork` and `$good-readme` skills before changing project files.
2. Follow `$gitwork` to establish the Git baseline and preserve unrelated user work before editing.
3. Implement and verify the requested change.
4. Run the complete `$good-readme` end-of-turn gate. Create or update the bilingual `README.md` when repository evidence requires it; otherwise report `README: unchanged` with the reason.
5. Return to `$gitwork` to review task-owned artifacts, stage only isolated task changes, run its required checks, and create the task commit when safe.
6. Do not report the task as complete until both skill workflows have been completed, or until an exact blocker has been reported.

These requirements are mandatory even when the user does not mention Git or README documentation. Apply each skill's own trigger and safety gates faithfully; never treat a qualifying change as exempt merely because it is small, internal, test-only, or documentation-adjacent.
