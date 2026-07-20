---
description: "Prepare, visibly execute, and finalize one LoopSail subagent task"
allowed-tools: ["Read", "Agent(loopsail:worker)", "Bash(${CLAUDE_PLUGIN_ROOT}/skills/loopsail/scripts/loopsail.py slash doctor)", "Bash(${CLAUDE_PLUGIN_ROOT}/skills/loopsail/scripts/loopsail.py slash validate)", "Bash(${CLAUDE_PLUGIN_ROOT}/skills/loopsail/scripts/loopsail.py slash prepare-step)", "Bash(${CLAUDE_PLUGIN_ROOT}/skills/loopsail/scripts/loopsail.py slash finalize-step)", "Bash(git status:*)", "Bash(git diff:*)"]
---

# Run one LoopSail step

Act as the Supervisor. Use only project-root TASKS.json and parse every Coordinator
response as one command-envelope v2 JSON document.

Tool routing is literal: do not call the Skill tool or another slash command. Each
Coordinator action below means invoking the Bash tool with exactly the matching
`${CLAUDE_PLUGIN_ROOT}/skills/loopsail/scripts/loopsail.py slash <action>` command.
Do not substitute exploratory shell commands.

1. Run `doctor` and `validate`.
2. Run `prepare-step`.
3. If data.action is spawn_worker, invoke the Agent tool exactly once with
   subagent_type loopsail:worker, prompt "Execute the active LoopSail worker request.",
   and run_in_background false. The hook supplies the authoritative request path.
4. Whether the Agent succeeds, blocks, or ends unexpectedly, always run finalize-step.
5. If prepare-step reports data.action finalize_pending, skip Agent and run finalize-step.
6. Stop after this one prepare/Agent/finalize unit. Report the actual Coordinator
   result, task commit or blocker, remaining work, and the next slash command.

Never spawn a background worker or a second Agent for the same request. Never merge,
push, publish, deploy, or discard the retained diff.
