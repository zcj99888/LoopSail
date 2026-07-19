---
description: "Execute or recover one supervised loopsail progress unit"
allowed-tools: ["Read", "Bash(${CLAUDE_PLUGIN_ROOT}/skills/loopsail/scripts/loopsail.py slash doctor)", "Bash(${CLAUDE_PLUGIN_ROOT}/skills/loopsail/scripts/loopsail.py slash validate)", "Bash(${CLAUDE_PLUGIN_ROOT}/skills/loopsail/scripts/loopsail.py slash run-once)", "Bash(git status:*)", "Bash(git diff:*)"]
---

# Run one loopsail step

Act as the Supervisor defined by the `loopsail` skill. Use only the project-root `TASKS.json`.

Use Bash internally with `"${CLAUDE_PLUGIN_ROOT}/skills/loopsail/scripts/loopsail.py" slash <action>` for the fixed actions below.

1. Perform the skill's preflight, including the fixed doctor and validate actions and a read-only clean-worktree check when this list has not started.
2. Launch the fixed `run-once` action as a background Bash task because a Worker may run for up to 2700 seconds. Poll the same process until it exits; never start a duplicate because output is temporarily quiet.
3. Interpret the fresh step report and actual exit code according to the skill. Read referenced logs only when diagnosing a block.
4. Report the performed unit, task result, branch, remaining work, and the next slash command. Do not continue to another step and do not ask the user to run a shell command.
