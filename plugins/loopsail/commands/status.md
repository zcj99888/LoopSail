---
description: "Show status for the project-root loopsail task list"
allowed-tools: ["Read", "Bash(${CLAUDE_PLUGIN_ROOT}/skills/loopsail/scripts/loopsail.py slash status)"]
---

# Show loopsail status

Use Bash internally to execute `"${CLAUDE_PLUGIN_ROOT}/skills/loopsail/scripts/loopsail.py" slash status` for the project-root `TASKS.json`. Summarize the list ID, project status, loopsail branch, active task, attempts, AI retry count, failures, remaining tasks, and final verification. If no run state exists, clearly say that the list is validated but not started. Do not mutate files and do not ask the user to run a shell command.
