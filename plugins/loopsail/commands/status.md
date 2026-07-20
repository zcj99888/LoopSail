---
description: "Show protocol-v2 status for the project-root LoopSail task list"
allowed-tools: ["Read", "Bash(${CLAUDE_PLUGIN_ROOT}/skills/loopsail/scripts/loopsail.py slash status)"]
---

# Show LoopSail status

Run the fixed status action and parse its single command-envelope v2 response.
Summarize whether the list has started, its branch and project status, active request
and bound agent when present, per-task attempts/commits/failures, and final
verification. Do not mutate the project.
