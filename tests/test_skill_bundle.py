from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = PROJECT_ROOT / "plugins" / "loopsail"
SKILL_ROOT = PLUGIN_ROOT / "skills" / "loopsail"
COMMAND_NAMES = {
    "init", "doctor", "validate", "run-once", "run-all", "status", "retry"
}


class PluginBundleTests(unittest.TestCase):
    def test_bundle_discovers_one_worker_and_complete_hooks(self) -> None:
        manifest = json.loads(
            (PLUGIN_ROOT / ".claude-plugin/plugin.json").read_text(encoding="utf-8")
        )
        self.assertEqual(manifest["name"], "loopsail")
        self.assertEqual(manifest["version"], "2.0.3")
        agents = list((PLUGIN_ROOT / "agents").glob("*.md"))
        self.assertEqual([path.stem for path in agents], ["worker"])
        agent = agents[0].read_text(encoding="utf-8")
        self.assertIn("name: worker", agent)
        self.assertIn("tools: Read, Edit, Write, Glob, Grep, Bash", agent)
        self.assertIn("project-relative POSIX path", agent)
        self.assertIn("Do not read TASKS.json", agent)
        for forbidden in ("Agent,", "Skill,", "Web,", "AskUserQuestion"):
            self.assertNotIn(forbidden, agent)

        hooks = json.loads(
            (PLUGIN_ROOT / "hooks/hooks.json").read_text(encoding="utf-8")
        )["hooks"]
        self.assertEqual(
            set(hooks),
            {
                "SubagentStart",
                "PreToolUse",
                "PostToolUse",
                "PostToolUseFailure",
                "SubagentStop",
            },
        )
        self.assertEqual(hooks["SubagentStart"][0]["matcher"], "^loopsail:worker$")
        self.assertEqual(hooks["SubagentStop"][0]["matcher"], "^loopsail:worker$")

    def test_commands_use_prepare_agent_finalize_without_legacy_launcher(self) -> None:
        self.assertEqual(
            {path.stem for path in (PLUGIN_ROOT / "commands").glob("*.md")},
            COMMAND_NAMES,
        )
        for path in (PLUGIN_ROOT / "commands").glob("*.md"):
            content = path.read_text(encoding="utf-8")
            self.assertTrue(content.startswith("---\n"), path)
            self.assertIn("description:", content, path)
            self.assertIn("allowed-tools:", content, path)
            self.assertIn("${CLAUDE_PLUGIN_ROOT}", content, path)
        for name in ("run-once", "run-all"):
            content = (PLUGIN_ROOT / "commands" / f"{name}.md").read_text(
                encoding="utf-8"
            )
            self.assertIn("Agent(loopsail:worker)", content)
            self.assertIn("prepare-step", content)
            self.assertIn("finalize-step", content)
            self.assertIn("run_in_background false", content)
            self.assertNotIn("as a background Bash task", content)
        corpus = "\n".join(
            path.read_text(encoding="utf-8")
            for path in PLUGIN_ROOT.rglob("*")
            if path.is_file() and "__pycache__" not in path.parts
        )
        self.assertNotIn("--json-schema", corpus)
        self.assertNotIn("--no-session-persistence", corpus)
        self.assertNotIn("worker_timeout_seconds", corpus)
        self.assertFalse((SKILL_ROOT / "references/worker.md").exists())
        self.assertFalse((SKILL_ROOT / "references/claude-settings.json").exists())

    def test_copied_plugin_initializes_with_one_envelope(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            copied = base / "plugin"
            target = base / "target"
            target.mkdir()
            shutil.copytree(PLUGIN_ROOT, copied)
            subprocess.run(["git", "init", "-b", "main"], cwd=target, check=True, capture_output=True)
            subprocess.run(["git", "config", "user.name", "Test"], cwd=target, check=True)
            subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=target, check=True)
            (target / "README.md").write_text("# Target\n", encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=target, check=True)
            subprocess.run(["git", "commit", "-m", "seed"], cwd=target, check=True, capture_output=True)
            env = dict(os.environ)
            env["HOME"] = str(base / "home")
            (base / "home").mkdir()
            runner = copied / "skills/loopsail/scripts/loopsail.py"
            result = subprocess.run(
                [sys.executable, str(runner), "init"],
                cwd=target,
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            lines = result.stdout.splitlines()
            self.assertEqual(len(lines), 1)
            envelope = json.loads(lines[0])
            self.assertTrue(envelope["ok"])
            self.assertEqual(envelope["data"]["kind"], "init-report")
            self.assertTrue((target / "TASKS.json").is_file())
            self.assertFalse((target / ".claude/skills/loopsail").exists())


if __name__ == "__main__":
    unittest.main()
