---
description: "Run the complete loopsail task list under unattended supervision"
allowed-tools: ["Read", "Bash(${CLAUDE_PLUGIN_ROOT}/skills/loopsail/scripts/loopsail.py slash doctor)", "Bash(${CLAUDE_PLUGIN_ROOT}/skills/loopsail/scripts/loopsail.py slash validate)", "Bash(${CLAUDE_PLUGIN_ROOT}/skills/loopsail/scripts/loopsail.py slash run-all)", "Bash(${CLAUDE_PLUGIN_ROOT}/skills/loopsail/scripts/loopsail.py slash status)", "Bash(git status:*)", "Bash(git diff:*)"]
disable-model-invocation: true
---

# Run the complete loopsail loop

This command is the user's explicit request for unattended full-list execution. Act as the Supervisor defined by the `loopsail` skill and use only the project-root `TASKS.json`.

Use Bash internally with `"${CLAUDE_PLUGIN_ROOT}/skills/loopsail/scripts/loopsail.py" slash <action>` for the fixed actions below.

1. Perform the skill's preflight with the fixed doctor and validate actions. For a new list, require a clean worktree.
2. Launch the fixed `run-all` action once as a background Bash task. Poll that same process until it exits. Use the fixed status action for periodic progress summaries; never start `run-once` or a second `run-all` concurrently.
3. On completion, report the loopsail branch, task commits, and final verification. On a block, diagnose it using the skill and stop with `/loopsail:retry <TASK_ID>` as the user-facing recovery entry.
4. Never merge, push, rebase, publish, deploy, discard changes, or ask the user to run a shell command.
