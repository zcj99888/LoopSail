---
description: "Validate the project-root TASKS.json for loopsail execution"
allowed-tools: ["Read", "Edit", "Bash(${CLAUDE_PLUGIN_ROOT}/skills/loopsail/scripts/loopsail.py slash validate)"]
---

# Validate TASKS.json

Use Bash internally to execute `"${CLAUDE_PLUGIN_ROOT}/skills/loopsail/scripts/loopsail.py" slash validate`. It always validates `TASKS.json` at the Git project root and accepts no alternate task-list path or CLI options.

If validation fails, read `TASKS.json` and the task-list schema from `${CLAUDE_PLUGIN_ROOT}/skills/loopsail/references/task-list.schema.json`. Fix only objective schema, path, dependency, or command-shape errors; do not invent product requirements or acceptance criteria. Re-run the fixed action after each correction. Report success with the list ID and task count, or identify the product decisions the user must provide. Do not ask the user to run a shell command.
