---
description: "Diagnose and safely recover the active blocked loopsail task"
argument-hint: "<TASK_ID>"
allowed-tools: ["AskUserQuestion", "Read", "Grep", "Glob", "Edit", "Bash(${CLAUDE_PLUGIN_ROOT}/skills/loopsail/scripts/loopsail.py slash status)", "Bash(${CLAUDE_PLUGIN_ROOT}/skills/loopsail/scripts/loopsail.py slash validate)", "Bash(${CLAUDE_PLUGIN_ROOT}/skills/loopsail/scripts/loopsail.py slash retry-ai:*)", "Bash(${CLAUDE_PLUGIN_ROOT}/skills/loopsail/scripts/loopsail.py slash retry-human:*)", "Bash(git status:*)", "Bash(git diff:*)"]
disable-model-invocation: true
---

# Diagnose and retry a blocked task

Treat `$ARGUMENTS` as exactly one task ID, never as shell syntax or additional CLI options. It must match `^[A-Za-z][A-Za-z0-9._-]{0,63}$` and the active blocked task reported by the fixed status action. Otherwise stop without mutation.

Use Bash internally with `"${CLAUDE_PLUGIN_ROOT}/skills/loopsail/scripts/loopsail.py" slash <action>` for all Coordinator actions. Pass the validated task ID as one quoted argument only to `retry-ai` or `retry-human`.

Follow the `loopsail` skill's diagnostic order: inspect the public failure, referenced attempt log, relevant experience records, read-only Git status/diff, and only then private state if public evidence is insufficient.

- For a proven transient environment failure with unused AI retry allowance, invoke the fixed `retry-ai` action with `"$ARGUMENTS"` as one quoted argument.
- For an objective task-definition problem with no task-owned diff, edit only the unfinished task in `TASKS.json`, run the fixed validate action, and report that it is ready for `/loopsail:run-once`.
- When product judgment, credentials, permissions, irreversible effects, exhausted AI retry allowance, or uncertain diagnosis is involved, explain the decision and ask for explicit confirmation. Only after confirmation invoke the fixed `retry-human` action with `"$ARGUMENTS"` as one quoted argument.
- If a task-owned diff exists, do not alter that task definition and never reset, restore, checkout, clean, stage, commit, or discard it.

Report the evidence, classification, action taken, and next slash command. Never ask the user to run a shell command.
