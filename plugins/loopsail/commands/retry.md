---
description: "Diagnose and safely recover the active blocked LoopSail task"
argument-hint: "<TASK_ID>"
allowed-tools: ["AskUserQuestion", "Read", "Grep", "Glob", "Edit", "Bash(${CLAUDE_PLUGIN_ROOT}/skills/loopsail/scripts/loopsail.py slash status)", "Bash(${CLAUDE_PLUGIN_ROOT}/skills/loopsail/scripts/loopsail.py slash validate)", "Bash(${CLAUDE_PLUGIN_ROOT}/skills/loopsail/scripts/loopsail.py slash retry-ai:*)", "Bash(${CLAUDE_PLUGIN_ROOT}/skills/loopsail/scripts/loopsail.py slash retry-human:*)", "Bash(git status:*)", "Bash(git diff:*)"]
disable-model-invocation: true
---

# Diagnose and retry a blocked task

Treat the argument as exactly one task ID matching
^[A-Za-z][A-Za-z0-9._-]{0,63}$ and equal to status.active_task.

Inspect the public failure, attempt log, sanitized event log, relevant experience
record, and read-only Git diff. Never reset or discard a retained task diff.

- Use retry-ai only for a proven transient failure with unused AI retry allowance.
- With no task-owned diff, objective unfinished-task definition errors may be fixed in
  TASKS.json and revalidated.
- Product judgment, credentials, permissions, irreversible effects, uncertain
  diagnosis, or exhausted AI allowance require explicit confirmation before
  retry-human.

Report evidence, classification, action, and the next slash command.
