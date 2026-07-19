---
description: "Check the loopsail runtime and configured Claude launcher"
allowed-tools: ["Bash(${CLAUDE_PLUGIN_ROOT}/skills/loopsail/scripts/loopsail.py slash doctor)"]
---

# Check loopsail

Use Bash internally to execute `"${CLAUDE_PLUGIN_ROOT}/skills/loopsail/scripts/loopsail.py" slash doctor`. Report whether the launcher is healthy, the detected Claude Code version, and any configuration source that was loaded. Do not ask the user to run a shell command.
