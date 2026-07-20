---
description: "Check the LoopSail 2.0 runtime, worker agent, hooks, configuration, and schemas"
allowed-tools: ["Bash(${CLAUDE_PLUGIN_ROOT}/skills/loopsail/scripts/loopsail.py slash doctor)"]
---

# Check LoopSail

Tool routing is literal: do not call the Skill tool or another slash command. Invoke
the Bash tool once with exactly
`${CLAUDE_PLUGIN_ROOT}/skills/loopsail/scripts/loopsail.py slash doctor`.

Parse its single command-envelope v2 JSON document and
report whether the worker agent, all four hook phases, configuration, Python runtime,
and checked-in Draft-07 schemas are healthy. Do not expose internal absolute plugin
paths or ask the user to run a shell command.
