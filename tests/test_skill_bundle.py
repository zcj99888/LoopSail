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
    "init",
    "doctor",
    "validate",
    "run-once",
    "run-all",
    "status",
    "retry",
}


class PluginBundleTests(unittest.TestCase):
    def test_plugin_runtime_uses_standard_discovery_layout(self) -> None:
        manifest = json.loads(
            (PLUGIN_ROOT / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8")
        )
        self.assertEqual(manifest["name"], "loopsail")
        self.assertRegex(manifest["version"], r"^\d+\.\d+\.\d+$")
        self.assertTrue((SKILL_ROOT / "SKILL.md").is_file())
        self.assertTrue((SKILL_ROOT / "scripts" / "loopsail.py").is_file())
        self.assertTrue((SKILL_ROOT / "references" / "worker.md").is_file())
        self.assertTrue((SKILL_ROOT / "templates" / "LOOP.md").is_file())
        self.assertEqual(
            {path.stem for path in (PLUGIN_ROOT / "commands").glob("*.md")},
            COMMAND_NAMES,
        )

    def test_commands_use_fixed_slash_actions_and_no_task_list_argument(self) -> None:
        for path in (PLUGIN_ROOT / "commands").glob("*.md"):
            content = path.read_text(encoding="utf-8")
            self.assertTrue(content.startswith("---\n"), path)
            self.assertIn("description:", content, path)
            self.assertIn("allowed-tools:", content, path)
            self.assertIn("${CLAUDE_PLUGIN_ROOT}", content, path)
            self.assertIn("scripts/loopsail.py slash", content, path)
            self.assertNotIn("loopsail.py validate TASKS.json", content, path)
            self.assertNotIn("loopsail.py run TASKS.json", content, path)
            self.assertNotIn("[task-list", content.lower(), path)

        retry = (PLUGIN_ROOT / "commands" / "retry.md").read_text(
            encoding="utf-8"
        )
        self.assertIn('argument-hint: "<TASK_ID>"', retry)
        self.assertIn("exactly one task ID", retry)

    def test_copied_plugin_initializes_without_copying_runtime_into_project(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            copied_plugin = base / "plugin" / "loopsail"
            target = base / "target-project"
            target.mkdir(parents=True)
            shutil.copytree(PLUGIN_ROOT, copied_plugin)

            subprocess.run(
                ["git", "init", "-b", "main"],
                cwd=target,
                check=True,
                text=True,
                capture_output=True,
            )
            (target / "README.md").write_text("# Target project\n", encoding="utf-8")
            subprocess.run(
                ["git", "add", "."], cwd=target, check=True, text=True, capture_output=True
            )
            environment = dict(os.environ)
            environment.update(
                {
                    "GIT_AUTHOR_NAME": "Test",
                    "GIT_AUTHOR_EMAIL": "test@example.invalid",
                    "GIT_COMMITTER_NAME": "Test",
                    "GIT_COMMITTER_EMAIL": "test@example.invalid",
                }
            )
            subprocess.run(
                ["git", "commit", "-m", "seed"],
                cwd=target,
                env=environment,
                check=True,
                text=True,
                capture_output=True,
            )

            runner = copied_plugin / "skills" / "loopsail" / "scripts" / "loopsail.py"
            result = subprocess.run(
                [sys.executable, str(runner), "init"],
                cwd=target,
                env=environment,
                check=False,
                text=True,
                capture_output=True,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue((target / "LOOP.md").is_file())
            self.assertTrue((target / "TASKS.json").is_file())
            self.assertFalse((target / ".claude" / "skills" / "loopsail").exists())


if __name__ == "__main__":
    unittest.main()
