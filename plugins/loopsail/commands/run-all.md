---
description: "Run the complete LoopSail list through visible sequential subagents"
allowed-tools: ["Read", "Agent(loopsail:worker)", "Bash(${CLAUDE_PLUGIN_ROOT}/skills/loopsail/scripts/loopsail.py slash doctor)", "Bash(${CLAUDE_PLUGIN_ROOT}/skills/loopsail/scripts/loopsail.py slash validate)", "Bash(${CLAUDE_PLUGIN_ROOT}/skills/loopsail/scripts/loopsail.py slash prepare-step)", "Bash(${CLAUDE_PLUGIN_ROOT}/skills/loopsail/scripts/loopsail.py slash finalize-step)", "Bash(${CLAUDE_PLUGIN_ROOT}/skills/loopsail/scripts/loopsail.py slash status)", "Bash(git status:*)", "Bash(git diff:*)"]
disable-model-invocation: true
---

# Run all LoopSail tasks

This command is explicit authorization to supervise the whole task list sequentially.

Tool routing is literal: do not call the Skill tool or another slash command. Each
Coordinator action below means invoking the Bash tool with exactly the matching
`${CLAUDE_PLUGIN_ROOT}/skills/loopsail/scripts/loopsail.py slash <action>` command.
Do not substitute exploratory shell commands.

1. Run doctor and validate once.
2. Repeatedly run prepare-step and inspect its command-envelope v2 data.
3. For spawn_worker, invoke exactly one foreground Agent with subagent_type
   loopsail:worker, prompt "Execute the active LoopSail worker request.", and
   run_in_background false. Always run finalize-step after it returns or fails.
4. For data.action finalize_pending, finalize without spawning.
5. Continue only while finalize reports progress and the next prepare can proceed.
   Stop immediately on blocked or idle. Stop successfully on complete/already_complete.
6. Report visible subagent progress, task commits, final verification, or the exact
   retry entry point.

Never use background Bash or concurrent workers. Never merge, push, publish, deploy,
or discard retained changes.
