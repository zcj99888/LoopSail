---
description: "Initialize the current project for the LoopSail 2.0 structured task loop"
allowed-tools: ["AskUserQuestion", "Bash(${CLAUDE_PLUGIN_ROOT}/skills/loopsail/scripts/loopsail.py slash doctor)", "Bash(${CLAUDE_PLUGIN_ROOT}/skills/loopsail/scripts/loopsail.py slash init-check)", "Bash(${CLAUDE_PLUGIN_ROOT}/skills/loopsail/scripts/loopsail.py slash init)", "Bash(${CLAUDE_PLUGIN_ROOT}/skills/loopsail/scripts/loopsail.py slash init-confirmed)"]
disable-model-invocation: true
---

# Initialize LoopSail

Use only the fixed actions listed in allowed-tools and parse each single
command-envelope v2 response.

1. Run doctor and stop on an unhealthy runtime.
2. Run init-check.
3. If Git initialization or an initial commit is required, obtain explicit user
   confirmation before running init-confirmed. Otherwise run init.
4. Report created, updated, and preserved files from the init-report.
5. Tell the user to complete CLAUDE.md and TASKS.json, then use /loopsail:validate.

Do not expose internal shell invocations.
