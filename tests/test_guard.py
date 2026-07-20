from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_ROOT = PROJECT_ROOT / "plugins/loopsail/skills/loopsail/scripts"
SPEC = importlib.util.spec_from_file_location(
    "loopsail_guard_v2_test", SCRIPT_ROOT / "guard.py"
)
assert SPEC and SPEC.loader
guard = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = guard
SPEC.loader.exec_module(guard)


class GuardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.tool_root = self.root.parent / "plugin-install"
        self.context = {
            "root": self.root,
            "tool_root": self.tool_root,
            "request_path": ".loopsail/input/list/request.json",
            "task_file": "TASKS.json",
            "allowed_paths": ["src/**", "tests/**"],
            "protected_paths": ["src/generated/**"],
        }

    def decide(self, tool: str, value: dict[str, object]) -> str | None:
        return guard.decide(tool, value, self.context)

    def test_allows_exact_request_read_and_in_scope_work(self) -> None:
        self.assertIsNone(
            self.decide(
                "Read",
                {"file_path": str(self.root / ".loopsail/input/list/request.json")},
            )
        )
        self.assertIsNone(
            self.decide("Edit", {"file_path": str(self.root / "src/app.py")})
        )
        self.assertIsNone(self.decide("Bash", {"command": "git diff --check"}))

    def test_denies_other_control_files_and_mutations(self) -> None:
        reason = self.decide(
            "Read", {"file_path": str(self.root / ".loopsail/runs/list/state.json")}
        )
        self.assertIn("bound immutable", reason)
        self.assertIn(
            "control",
            self.decide("Edit", {"file_path": str(self.root / "TASKS.json")}),
        )
        self.assertIn(
            "control",
            self.decide("Read", {"file_path": str(self.root / "TASKS.json")}),
        )
        self.assertIn(
            "control",
            self.decide("Read", {"file_path": str(self.root / "经验记录.md")}),
        )
        self.assertIn(
            "outside",
            self.decide("Write", {"file_path": str(self.root / "docs/extra.md")}),
        )
        self.assertIn(
            "protected",
            self.decide(
                "Write", {"file_path": str(self.root / "src/generated/client.py")}
            ),
        )

    def test_denies_git_external_secret_destructive_and_unknown_tools(self) -> None:
        cases = [
            ("Bash", {"command": "git commit -m done"}, "Git mutations"),
            ("Bash", {"command": "git -C . -c user.name=x commit -m done"}, "Git mutations"),
            ("Bash", {"command": "curl -X POST https://example.invalid"}, "external"),
            ("Bash", {"command": "rm -rf build"}, "deletion"),
            ("Read", {"file_path": str(self.root / ".env")}, "secret"),
            ("Agent", {"prompt": "delegate"}, "not allowed"),
        ]
        for tool, value, expected in cases:
            with self.subTest(tool=tool, value=value):
                self.assertIn(expected.lower(), self.decide(tool, value).lower())

    def test_missing_policy_fails_closed(self) -> None:
        self.assertIn(
            "invalid",
            guard.decide("Read", {"file_path": "src/a.py"}, {"root": self.root}),
        )

    def test_edit_content_is_not_misclassified_as_a_path(self) -> None:
        self.assertIsNone(
            self.decide(
                "Edit",
                {
                    "file_path": str(self.root / "src/app.py"),
                    "old_string": "route = '/api/v1'",
                    "new_string": "route = '/api/v2'",
                },
            )
        )


if __name__ == "__main__":
    unittest.main()
