---
description: "Validate the project-root TASKS.json and LoopSail protocol-v2 configuration"
allowed-tools: ["Read", "Edit", "Bash(${CLAUDE_PLUGIN_ROOT}/skills/loopsail/scripts/loopsail.py slash validate)"]
---

# Validate TASKS.json

Run the fixed validate action and parse its command-envelope v2 response. It validates
only the project-root TASKS.json. If validation fails, read TASKS.json and the bundled
task-list.schema.json. Fix only objective schema, path, dependency, or verification
command shape errors; do not invent requirements. Re-run validate and report the list
ID and task count. A v1 task list is intentionally unsupported.
