---
description: "Initialize the current project for the loopsail structured task loop"
allowed-tools: ["AskUserQuestion", "Bash(${CLAUDE_PLUGIN_ROOT}/skills/loopsail/scripts/loopsail.py slash doctor)", "Bash(${CLAUDE_PLUGIN_ROOT}/skills/loopsail/scripts/loopsail.py slash init-check)", "Bash(${CLAUDE_PLUGIN_ROOT}/skills/loopsail/scripts/loopsail.py slash init)", "Bash(${CLAUDE_PLUGIN_ROOT}/skills/loopsail/scripts/loopsail.py slash init-confirmed)"]
disable-model-invocation: true
---

# Initialize loopsail

Use Bash internally with `"${CLAUDE_PLUGIN_ROOT}/skills/loopsail/scripts/loopsail.py" slash <action>` for every action below.

1. Run the fixed `doctor` action. Stop and report the environment problem if it fails.
2. Run the fixed `init-check` action.
3. If the current directory is not a Git repository, ask whether to initialize Git and create the loopsail scaffold plus initial commit. Run `init-confirmed` only after explicit confirmation; otherwise make no changes.
4. If it is a Git repository without `HEAD`, ask whether to create the scaffold and initial commit. Run `init-confirmed` after confirmation. If the user declines the commit but still wants the scaffold, run `init` and explain that later execution requires a clean committed baseline.
5. If the repository already has `HEAD`, run `init` without another confirmation.
6. Summarize created, updated, and preserved files. Tell the user to complete `CLAUDE.md` and `TASKS.json`, then use `/loopsail:validate`. Do not expose or ask the user to run the internal shell invocation.
