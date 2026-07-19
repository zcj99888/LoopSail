from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SKILL_ROOT = (
    Path(__file__).resolve().parents[1]
    / "plugins"
    / "loopsail"
    / "skills"
    / "loopsail"
)
GUARD_PATH = SKILL_ROOT / "scripts" / "guard.py"
SPEC = importlib.util.spec_from_file_location("loopsail_guard_under_test", GUARD_PATH)
assert SPEC and SPEC.loader
guard = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = guard
SPEC.loader.exec_module(guard)


class GuardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.environment = mock.patch.dict(
            os.environ,
            {
                "LOOPSAIL_PROJECT_ROOT": str(self.root),
                "LOOPSAIL_TOOL_DIR": str(self.root.parent / "loopsail-installation"),
                "LOOPSAIL_TASK_FILE": str(self.root / "TASKS.json"),
                "LOOPSAIL_ALLOWED_PATHS": '["src/**", "tests/**"]',
                "LOOPSAIL_PROTECTED_PATHS": '["src/generated/**"]',
            },
            clear=False,
        )
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def test_blocks_worker_git_mutations(self) -> None:
        self.assertIn("coordinator", guard.decide("Bash", {"command": "git commit -m done"}))
        self.assertIn("coordinator", guard.decide("Bash", {"command": "git push origin main"}))
        self.assertIn(
            "control files",
            guard.decide("Bash", {"command": "python3 inspect.py .loopsail/runs/test/state.json"}),
        )

    def test_allows_read_only_git_and_in_scope_edit(self) -> None:
        self.assertIsNone(guard.decide("Bash", {"command": "git diff --check"}))
        self.assertIsNone(guard.decide("Edit", {"file_path": str(self.root / "src" / "app.py")}))

    def test_blocks_protected_and_out_of_scope_paths(self) -> None:
        protected = guard.decide("Edit", {"file_path": str(self.root / "LOOP.md")})
        self.assertIn("protected", protected)
        task_input = guard.decide("Edit", {"file_path": str(self.root / "TASKS.json")})
        self.assertIn("protected", task_input)
        experience = guard.decide(
            "Edit", {"file_path": str(self.root / guard.LESSONS_FILE)}
        )
        self.assertIn("protected", experience)
        skill = guard.decide(
            "Edit",
            {
                "file_path": str(
                    self.root / ".claude" / "skills" / "loopsail" / "SKILL.md"
                )
            },
        )
        self.assertIn("protected", skill)
        shell_experience = guard.decide(
            "Bash", {"command": f"echo note >> {guard.LESSONS_FILE}"}
        )
        self.assertIn("coordinator", shell_experience)
        outside = guard.decide("Write", {"file_path": str(self.root / "docs" / "extra.md")})
        self.assertIn("outside", outside)
        configured = guard.decide(
            "Edit", {"file_path": str(self.root / "src" / "generated" / "client.py")}
        )
        self.assertIn("protected", configured)
        installation = guard.decide(
            "Write", {"file_path": str(self.root.parent / "loopsail-installation" / "loopsail.py")}
        )
        self.assertIn("installation", installation)
        shell_installation = guard.decide(
            "Bash", {"command": "sed -i s/old/new/ ../loopsail-installation/loopsail.py"}
        )
        self.assertIn("installation", shell_installation)

    def test_blocks_secret_paths_and_destructive_commands(self) -> None:
        self.assertIn("secret", guard.decide("Read", {"file_path": str(self.root / ".env")}))
        self.assertIn("deletion", guard.decide("Bash", {"command": "rm -rf build"}))


if __name__ == "__main__":
    unittest.main()
