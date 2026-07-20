---
description: "Show protocol-v2 status for the project-root LoopSail task list"
allowed-tools: ["Read", "Bash(${CLAUDE_PLUGIN_ROOT}/skills/loopsail/scripts/loopsail.py slash status)"]
---

# Show LoopSail status

Tool routing is literal: do not call the Skill tool or another slash command. Invoke
the Bash tool once with exactly
`${CLAUDE_PLUGIN_ROOT}/skills/loopsail/scripts/loopsail.py slash status` and parse its
single command-envelope v2 response.
Summarize whether the list has started, its branch and project status, active request
and bound agent when present, per-task attempts/commits/failures, and final
verification. Do not mutate the project.
