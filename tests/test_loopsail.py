from __future__ import annotations

import argparse
import contextlib
import hashlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = PROJECT_ROOT / "plugins" / "loopsail"
SKILL_ROOT = PLUGIN_ROOT / "skills" / "loopsail"
SPEC = importlib.util.spec_from_file_location(
    "loopsail_under_test", SKILL_ROOT / "scripts" / "loopsail.py"
)
assert SPEC and SPEC.loader
loopsail = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = loopsail
SPEC.loader.exec_module(loopsail)


def write_json(path: Path, value: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def command(*argv: str) -> dict[str, object]:
    return {"argv": list(argv), "cwd": ".", "timeout_seconds": 30}


def task(task_id: str, *, dependencies: list[str] | None = None) -> dict[str, object]:
    return {
        "id": task_id,
        "title": task_id,
        "description": f"Implement {task_id}",
        "depends_on": dependencies or [],
        "context_files": ["CLAUDE.md"],
        "allowed_paths": ["result*.txt"],
        "acceptance": [f"{task_id} is implemented"],
        "verify_commands": [command("python3", "-c", "raise SystemExit(0)")],
    }


def task_list(*tasks: dict[str, object], list_id: str = "test-run") -> dict[str, object]:
    return {
        "schema_version": 1,
        "list_id": list_id,
        "project": "Test",
        "final_verify_commands": [command("python3", "-c", "raise SystemExit(0)")],
        "tasks": list(tasks),
    }


def lesson(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "challenge": "定位一个非显而易见的问题",
        "detour": "先检查了无关模块",
        "root_cause": "边界条件没有被现有测试覆盖",
        "resolution": "补充实现和回归测试",
        "takeaway": "先从失败断言反推最小复现",
    }
    value.update(overrides)
    return value


def git_at(root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        check=check,
        text=True,
        capture_output=True,
    )


@contextlib.contextmanager
def working_directory(path: Path):
    previous = Path.cwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(previous)


class TemporaryGitProject:
    def __init__(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.git("init", "-b", "main")
        (self.root / ".gitignore").write_text(
            "/TASKS.json\n.loopsail/runs/\n.loopsail/logs/\n.loopsail/lock\n",
            encoding="utf-8",
        )
        (self.root / "CLAUDE.md").write_text("# Test project\n", encoding="utf-8")
        (self.root / loopsail.LESSONS_FILE).write_text("# Test experience log\n", encoding="utf-8")
        self.git("add", ".")
        self.git("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-m", "initial")
        self.git("config", "user.name", "Test")
        self.git("config", "user.email", "test@example.invalid")

    def git(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", "-C", str(self.root), *args],
            check=True,
            text=True,
            capture_output=True,
        )

    def close(self) -> None:
        self.temporary.cleanup()


class InitTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)

    def make_repository(self, name: str, *, commit: bool) -> Path:
        root = self.base / name
        root.mkdir()
        git_at(root, "init", "-b", "main")
        git_at(root, "config", "user.name", "Test")
        git_at(root, "config", "user.email", "test@example.invalid")
        if commit:
            (root / "README.md").write_text("# Seed\n", encoding="utf-8")
            git_at(root, "add", "README.md")
            git_at(root, "commit", "-m", "seed")
        return root

    def call_init(self, root: Path, *, yes: bool = False) -> str:
        output = io.StringIO()
        with working_directory(root), contextlib.redirect_stdout(output):
            exit_code = loopsail.command_init(argparse.Namespace(yes=yes))
        self.assertEqual(exit_code, 0)
        return output.getvalue()

    def test_complete_scaffold_uses_repository_root_and_project_name(self) -> None:
        root = self.make_repository("示例项目", commit=True)
        nested = root / "nested"
        nested.mkdir()

        output = self.call_init(nested, yes=True)

        self.assertIn(str(root), output)
        self.assertIn("created:", output)
        self.assertIn("项目开发规范", (root / "CLAUDE.md").read_text(encoding="utf-8"))
        self.assertIn("完整阅读 `CLAUDE.md`", (root / "AGENTS.md").read_text(encoding="utf-8"))
        self.assertIn("由人审阅", (root / "LOOP.md").read_text(encoding="utf-8"))
        self.assertIn(
            "示例项目 经验记录", (root / loopsail.LESSONS_FILE).read_text(encoding="utf-8")
        )
        self.assertFalse((root / ".claude" / "skills" / "loopsail").exists())
        template = json.loads((root / "TASKS.template.json").read_text(encoding="utf-8"))
        self.assertEqual(template["project"], "示例项目")
        with self.assertRaises(loopsail.LoopSailError):
            loopsail.validate_task_list(template, root)
        task_file = json.loads((root / "TASKS.json").read_text(encoding="utf-8"))
        self.assertEqual(task_file, template)
        self.assertFalse((root / ".loopsail" / "input").exists())
        self.assertEqual(
            git_at(root, "check-ignore", "-q", "TASKS.json", check=False).returncode,
            0,
        )
        self.assertFalse((root / ".loopsail" / "config.json").exists())
        self.assertFalse((root / ".loopsail" / "runs").exists())
        self.assertFalse((nested / "CLAUDE.md").exists())
        self.assertEqual(git_at(root, "rev-list", "--count", "HEAD").stdout.strip(), "1")

    def test_existing_files_are_preserved_and_gitignore_merge_is_idempotent(self) -> None:
        root = self.make_repository("project", commit=False)
        original_claude = "# Existing rules\n"
        original_lessons = "# Existing experience\n\nKeep this note.\n"
        (root / "CLAUDE.md").write_text(original_claude, encoding="utf-8")
        (root / loopsail.LESSONS_FILE).write_text(original_lessons, encoding="utf-8")
        (root / ".gitignore").write_text("custom/\n.loopsail/input/", encoding="utf-8")
        (root / "README.md").write_text("# Seed\n", encoding="utf-8")
        git_at(root, "add", ".")
        git_at(root, "commit", "-m", "seed")

        first_output = self.call_init(root)
        generated_agents = (root / "AGENTS.md").read_bytes()
        first_gitignore = (root / ".gitignore").read_text(encoding="utf-8")
        working_task = root / "TASKS.json"
        working_task.write_text('{"preserved": true}\n', encoding="utf-8")
        second_output = self.call_init(root)

        self.assertIn("updated:\n  - .gitignore", first_output)
        self.assertEqual((root / "CLAUDE.md").read_text(encoding="utf-8"), original_claude)
        self.assertEqual(
            (root / loopsail.LESSONS_FILE).read_text(encoding="utf-8"), original_lessons
        )
        self.assertEqual((root / "AGENTS.md").read_bytes(), generated_agents)
        self.assertFalse((root / ".claude" / "skills" / "loopsail").exists())
        self.assertEqual((root / ".gitignore").read_text(encoding="utf-8"), first_gitignore)
        for entry in loopsail.INIT_GITIGNORE_ENTRIES:
            self.assertEqual(first_gitignore.splitlines().count(entry), 1)
        self.assertEqual(working_task.read_text(encoding="utf-8"), '{"preserved": true}\n')
        self.assertIn("created:\n  - (none)", second_output)
        self.assertIn("  - TASKS.json", second_output)

    def test_existing_project_template_seeds_root_working_task_file(self) -> None:
        root = self.make_repository("custom-template", commit=True)
        custom_template = '{"schema_version": 1, "project": "custom"}\n'
        (root / "TASKS.template.json").write_text(custom_template, encoding="utf-8")

        self.call_init(root)

        self.assertEqual((root / "TASKS.json").read_text(encoding="utf-8"), custom_template)

    def test_non_repository_requires_confirmation_before_writing(self) -> None:
        noninteractive = self.base / "noninteractive"
        noninteractive.mkdir()
        with working_directory(noninteractive), mock.patch.object(
            loopsail, "stdin_is_interactive", return_value=False
        ):
            with self.assertRaisesRegex(loopsail.LoopSailError, "不是 Git 仓库"):
                loopsail.command_init(argparse.Namespace(yes=False))
        self.assertFalse((noninteractive / ".git").exists())
        self.assertEqual(list(noninteractive.iterdir()), [])

        interactive = self.base / "interactive"
        interactive.mkdir()
        with working_directory(interactive), mock.patch.object(
            loopsail, "stdin_is_interactive", return_value=True
        ), mock.patch("builtins.input", return_value="n"):
            with self.assertRaisesRegex(loopsail.LoopSailError, "已取消"):
                loopsail.command_init(argparse.Namespace(yes=False))
        self.assertFalse((interactive / ".git").exists())

    def test_interactive_git_init_can_decline_initial_commit(self) -> None:
        root = self.base / "prompted"
        root.mkdir()
        output = io.StringIO()
        with working_directory(root), contextlib.redirect_stdout(output), mock.patch.object(
            loopsail, "stdin_is_interactive", return_value=True
        ), mock.patch("builtins.input", side_effect=["yes", "no"]):
            self.assertEqual(loopsail.command_init(argparse.Namespace(yes=False)), 0)

        self.assertTrue((root / ".git").is_dir())
        self.assertNotEqual(
            git_at(root, "rev-parse", "--verify", "HEAD", check=False).returncode,
            0,
        )
        self.assertIn("git: initialized", output.getvalue())
        self.assertIn("commit: skipped", output.getvalue())

    def test_yes_initializes_git_and_creates_only_scaffold_commit(self) -> None:
        root = self.base / "automated"
        root.mkdir()
        identity = {
            "GIT_AUTHOR_NAME": "Test",
            "GIT_AUTHOR_EMAIL": "test@example.invalid",
            "GIT_COMMITTER_NAME": "Test",
            "GIT_COMMITTER_EMAIL": "test@example.invalid",
        }
        with mock.patch.dict(os.environ, identity), working_directory(root):
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(loopsail.command_init(argparse.Namespace(yes=True)), 0)

        self.assertEqual(
            git_at(root, "show", "-s", "--format=%s", "HEAD").stdout.strip(),
            loopsail.INIT_COMMIT_MESSAGE,
        )
        committed = set(
            git_at(
                root, "-c", "core.quotePath=false", "ls-tree", "-r", "--name-only", "HEAD"
            ).stdout.splitlines()
        )
        expected_scaffold = {
            ".gitignore",
            "AGENTS.md",
            "CLAUDE.md",
            "LOOP.md",
            "TASKS.template.json",
            loopsail.LESSONS_FILE,
        }
        expected_scaffold.update(target for _, target in loopsail.INIT_TEMPLATE_FILES)
        self.assertEqual(committed, expected_scaffold)
        self.assertIn("commit:", output.getvalue())

    def test_initial_commit_preserves_unrelated_staged_content(self) -> None:
        root = self.make_repository("staged", commit=False)
        (root / "unrelated.txt").write_text("user content\n", encoding="utf-8")
        git_at(root, "add", "unrelated.txt")

        self.call_init(root, yes=True)

        committed = git_at(root, "ls-tree", "-r", "--name-only", "HEAD").stdout.splitlines()
        self.assertNotIn("unrelated.txt", committed)
        self.assertIn("A  unrelated.txt", git_at(root, "status", "--short").stdout)

    def test_missing_identity_does_not_stage_scaffold(self) -> None:
        root = self.make_repository("no-identity", commit=False)
        result = loopsail.initialize_scaffold(root)
        completed = subprocess.CompletedProcess(["git", "var"], 1, "", "missing identity")
        with mock.patch.object(loopsail, "run_git", return_value=completed):
            with self.assertRaisesRegex(loopsail.LoopSailError, "身份未配置"):
                loopsail.create_initial_commit(root, result["touched"])
        status = git_at(root, "status", "--short").stdout
        self.assertNotIn("A  CLAUDE.md", status)
        self.assertIn("?? CLAUDE.md", status)

    def test_commit_hook_failure_keeps_scaffold_staged(self) -> None:
        root = self.make_repository("hook-failure", commit=False)
        result = loopsail.initialize_scaffold(root)
        hook = root / ".git" / "hooks" / "pre-commit"
        hook.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
        hook.chmod(0o755)

        with self.assertRaisesRegex(loopsail.LoopSailError, "可能仍处于暂存状态"):
            loopsail.create_initial_commit(root, result["touched"])
        self.assertIn("A  CLAUDE.md", git_at(root, "status", "--short").stdout)

    def test_unsafe_target_fails_preflight_and_write_failure_rolls_back(self) -> None:
        unsafe = self.make_repository("unsafe", commit=True)
        (unsafe / "CLAUDE.md").mkdir()
        with working_directory(unsafe):
            with self.assertRaisesRegex(loopsail.LoopSailError, "not a regular file"):
                loopsail.command_init(argparse.Namespace(yes=False))
        self.assertFalse((unsafe / ".loopsail").exists())
        self.assertFalse((unsafe / ".gitignore").exists())

        rollback = self.make_repository("rollback", commit=True)
        original_write = loopsail.atomic_write_bytes

        skill_path = rollback / "LOOP.md"

        def failing_write(path: Path, value: bytes, *, replace_existing: bool) -> None:
            if path == skill_path:
                raise OSError("simulated write failure")
            original_write(path, value, replace_existing=replace_existing)

        with mock.patch.object(loopsail, "atomic_write_bytes", side_effect=failing_write):
            with self.assertRaisesRegex(loopsail.LoopSailError, "was rolled back"):
                loopsail.initialize_scaffold(rollback)
        self.assertFalse((rollback / "CLAUDE.md").exists())
        self.assertFalse((rollback / ".loopsail").exists())
        self.assertFalse((rollback / ".claude").exists())
        self.assertFalse((rollback / ".gitignore").exists())


class SlashAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "project"
        self.root.mkdir()
        git_at(self.root, "init", "-b", "main")

    def test_slash_task_file_is_always_project_root_file(self) -> None:
        nested = self.root / "nested"
        nested.mkdir()
        with working_directory(nested):
            args = loopsail.slash_task_args()
        self.assertEqual(args.task_list, self.root / "TASKS.json")

    def test_slash_retry_requires_the_active_task_id_shape(self) -> None:
        state_path = self.root / "state.json"
        state_path.write_text("{}\n", encoding="utf-8")
        task_value = {"list_id": "test-run"}
        state = {
            "project_status": "blocked",
            "active_task": "TASK-001",
            "tasks": {
                "TASK-001": {"last_failure": {"summary": "temporary launcher failure"}}
            },
        }

        patches = (
            mock.patch.object(
                loopsail,
                "load_task_input",
                return_value=(self.root / "TASKS.json", task_value),
            ),
            mock.patch.object(
                loopsail,
                "state_paths",
                return_value=(state_path, self.root / "snapshot.json", self.root / "logs"),
            ),
            mock.patch.object(loopsail, "load_json", return_value=state),
            mock.patch.object(loopsail, "validate_state"),
        )
        with working_directory(self.root), patches[0], patches[1], patches[2], patches[3]:
            with self.assertRaisesRegex(loopsail.LoopSailError, "valid task ID"):
                loopsail.slash_task_args(actor="ai", requested_task_id="; git status")
            with self.assertRaisesRegex(loopsail.LoopSailError, "active blocked task"):
                loopsail.slash_task_args(actor="ai", requested_task_id="TASK-002")
            args = loopsail.slash_task_args(actor="ai", requested_task_id="TASK-001")

        self.assertEqual(args.task_list, self.root / "TASKS.json")
        self.assertEqual(args.task_id, "TASK-001")
        self.assertEqual(args.actor, "ai")
        self.assertIn("temporary launcher failure", args.reason)

    def test_non_retry_slash_actions_reject_extra_arguments(self) -> None:
        with self.assertRaisesRegex(loopsail.LoopSailError, "does not accept arguments"):
            loopsail.command_slash(
                argparse.Namespace(action="doctor", task_id="unexpected")
            )


class ConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        base = Path(self.temporary.name)
        self.home = base / "home"
        self.root = base / "project"
        self.root.mkdir()

    def test_user_project_and_explicit_config_overlay_by_field(self) -> None:
        write_json(
            self.home / ".loopsail" / "config.json",
            {"claude": {"command_prefix": ["user-claude"]}, "worker_timeout_seconds": 100},
        )
        write_json(
            self.root / ".loopsail" / "config.json",
            {"claude": {"extra_args": ["--effort", "high"]}, "worker_timeout_seconds": 200},
        )
        explicit = self.root / "explicit.json"
        write_json(explicit, {"log_output_limit_bytes": 1234})
        with mock.patch.object(loopsail.Path, "home", return_value=self.home):
            config, sources = loopsail.load_config(self.root, explicit)
        self.assertEqual(config["claude"]["command_prefix"], ["user-claude"])
        self.assertEqual(config["claude"]["extra_args"], ["--effort", "high"])
        self.assertEqual(config["worker_timeout_seconds"], 200)
        self.assertEqual(config["log_output_limit_bytes"], 1234)
        self.assertEqual(len(sources), 4)

    def test_arrays_replace_and_unknown_keys_fail(self) -> None:
        with self.assertRaisesRegex(loopsail.LoopSailError, "unknown keys"):
            loopsail.validate_config({"unknown": True})
        with self.assertRaisesRegex(loopsail.LoopSailError, "inline secret"):
            loopsail.validate_config({"claude": {"extra_args": ["TOKEN=secret-value"]}})


class TaskListValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / "CLAUDE.md").write_text("# Test\n", encoding="utf-8")

    def test_valid_list_is_normalized(self) -> None:
        value = task_list(task("TASK-001"), task("TASK-002", dependencies=["TASK-001"]))
        normalized = loopsail.validate_task_list(value, self.root)
        self.assertEqual([item["id"] for item in normalized["tasks"]], ["TASK-001", "TASK-002"])

    def test_unknown_dependency_and_cycle_fail(self) -> None:
        with self.assertRaisesRegex(loopsail.LoopSailError, "unknown dependency"):
            loopsail.validate_task_list(task_list(task("TASK-001", dependencies=["NOPE"])), self.root)
        first = task("TASK-001", dependencies=["TASK-002"])
        second = task("TASK-002", dependencies=["TASK-001"])
        with self.assertRaisesRegex(loopsail.LoopSailError, "dependency cycle"):
            loopsail.validate_task_list(task_list(first, second), self.root)

    def test_duplicate_id_escaping_context_and_shell_command_fail(self) -> None:
        with self.assertRaisesRegex(loopsail.LoopSailError, "duplicate task id"):
            loopsail.validate_task_list(task_list(task("TASK-001"), task("TASK-001")), self.root)
        bad = task("TASK-001")
        bad["context_files"] = ["../secret"]
        with self.assertRaisesRegex(loopsail.LoopSailError, "stay within"):
            loopsail.validate_task_list(task_list(bad), self.root)
        bad_command = task("TASK-001")
        bad_command["verify_commands"] = [command("git", "push")]
        with self.assertRaisesRegex(loopsail.LoopSailError, "may not mutate Git"):
            loopsail.validate_task_list(task_list(bad_command), self.root)


class SelectionAndStateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.project = TemporaryGitProject()
        self.addCleanup(self.project.close)
        self.normalized = loopsail.validate_task_list(
            task_list(task("TASK-001"), task("TASK-002", dependencies=["TASK-001"])),
            self.project.root,
        )
        self.task_file = self.project.root / "TASKS.json"
        write_json(self.task_file, self.normalized)
        self.state = loopsail.create_state(self.normalized, self.task_file, self.project.root)

    def test_selection_follows_dependencies(self) -> None:
        self.assertEqual(loopsail.ready_task(self.normalized, self.state)["id"], "TASK-001")
        self.state["tasks"]["TASK-001"]["status"] = "done"
        self.assertEqual(loopsail.ready_task(self.normalized, self.state)["id"], "TASK-002")

    def test_completed_definition_is_immutable(self) -> None:
        previous = json.loads(json.dumps(self.normalized))
        updated = json.loads(json.dumps(self.normalized))
        updated["tasks"][0]["description"] = "changed"
        self.state["tasks"]["TASK-001"]["status"] = "done"
        with self.assertRaisesRegex(loopsail.LoopSailError, "completed task definition cannot change"):
            loopsail.reconcile_task_list(self.project.root, updated, self.state, previous)

    def test_removed_pending_task_becomes_superseded(self) -> None:
        previous = json.loads(json.dumps(self.normalized))
        updated = json.loads(json.dumps(self.normalized))
        updated["tasks"] = updated["tasks"][:1]
        loopsail.reconcile_task_list(self.project.root, updated, self.state, previous)
        self.assertEqual(self.state["tasks"]["TASK-002"]["status"], "superseded")

    def test_adding_task_does_not_bypass_an_existing_task_blocker(self) -> None:
        previous = json.loads(json.dumps(self.normalized))
        updated = json.loads(json.dumps(self.normalized))
        updated["tasks"].append(task("TASK-003"))
        self.state["tasks"]["TASK-001"]["status"] = "blocked"
        self.state["active_task"] = "TASK-001"
        self.state["project_status"] = "blocked"
        loopsail.reconcile_task_list(self.project.root, updated, self.state, previous)
        self.assertEqual(self.state["project_status"], "blocked")
        self.assertEqual(self.state["active_task"], "TASK-001")

    def test_completed_list_rejects_final_command_changes(self) -> None:
        previous = json.loads(json.dumps(self.normalized))
        updated = json.loads(json.dumps(self.normalized))
        updated["final_verify_commands"] = [command("python3", "--version")]
        self.state["project_status"] = "complete"
        with self.assertRaisesRegex(loopsail.LoopSailError, "completed task list is frozen"):
            loopsail.reconcile_task_list(self.project.root, updated, self.state, previous)

    def test_experience_only_diff_does_not_block_unfinished_task_revision(self) -> None:
        previous = json.loads(json.dumps(self.normalized))
        updated = json.loads(json.dumps(self.normalized))
        updated["tasks"][0]["description"] = "clarified after a blocker"
        self.state["tasks"]["TASK-001"]["attempts"] = 2
        self.state["tasks"]["TASK-001"]["attempt_sequence"] = 2
        self.state["tasks"]["TASK-001"]["ai_retry_count"] = 1
        (self.project.root / loopsail.LESSONS_FILE).write_text(
            "# Test experience log\n\nBlocked attempt.\n", encoding="utf-8"
        )

        loopsail.reconcile_task_list(self.project.root, updated, self.state, previous)

        self.assertEqual(self.state["tasks"]["TASK-001"]["status"], "pending")
        self.assertEqual(self.state["tasks"]["TASK-001"]["attempts"], 0)
        self.assertEqual(self.state["tasks"]["TASK-001"]["attempt_sequence"], 2)
        self.assertEqual(self.state["tasks"]["TASK-001"]["ai_retry_count"], 1)

    def test_repeated_unchanged_failure_blocks_early(self) -> None:
        self.state["tasks"]["TASK-001"]["attempts"] = 1
        first = loopsail.record_failure(self.state, "TASK-001", "Same failure", "tree")
        self.assertFalse(first)
        self.state["tasks"]["TASK-001"]["attempts"] = 2
        second = loopsail.record_failure(self.state, "TASK-001", " same   failure ", "tree")
        self.assertTrue(second)

    def test_failed_lock_contender_cannot_unlink_or_bypass_active_lock(self) -> None:
        lock_path = self.project.root / ".loopsail" / "lock"

        with loopsail.project_lock(self.project.root):
            self.assertTrue(lock_path.is_file())
            with self.assertRaisesRegex(loopsail.LoopSailError, "already running"):
                with loopsail.project_lock(self.project.root):
                    self.fail("a second lock holder must not enter")
            self.assertTrue(lock_path.is_file())
            with self.assertRaisesRegex(loopsail.LoopSailError, "already running"):
                with loopsail.project_lock(self.project.root):
                    self.fail("a third lock holder must not enter")
            self.assertTrue(lock_path.is_file())

        self.assertFalse(lock_path.exists())


class LauncherTests(unittest.TestCase):
    def test_json_tail_tolerates_shell_noise(self) -> None:
        payload = loopsail.parse_json_tail("startup noise\n{\"structured_output\": {\"status\": \"ok\"}}\n")
        self.assertEqual(payload["structured_output"]["status"], "ok")

    def test_worker_launcher_uses_prefix_and_does_not_select_model(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        (root / "CLAUDE.md").write_text("# Test\n", encoding="utf-8")
        task_file = root / "TASKS.json"
        task_value = task("TASK-001")
        task_state = loopsail.new_task_state(task_value)
        task_state["attempts"] = 1
        config = json.loads(json.dumps(loopsail.DEFAULT_CONFIG))
        config["claude"]["command_prefix"] = ["bash", "-lic", "ccds \"$@\"", "loopsail-claude"]
        result = {
            "structured_output": {
                "task_id": "TASK-001",
                "status": "completed",
                "summary": "done",
                "changed_files": [],
                "verification_results": [],
                "lessons": [],
                "blocker": None,
            }
        }
        captured: list[str] = []

        def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            captured.extend(argv)
            return subprocess.CompletedProcess(argv, 0, json.dumps(result), "shell startup warning")

        with mock.patch.object(loopsail, "run_process", side_effect=fake_run):
            worker_result, _ = loopsail.invoke_worker(root, task_file, task_value, task_state, config)
        self.assertEqual(captured[:4], ["bash", "-lic", "ccds \"$@\"", "loopsail-claude"])
        self.assertIn("--no-session-persistence", captured)
        self.assertIn("--tools", captured)
        self.assertNotIn("--model", captured)
        self.assertEqual(worker_result["status"], "completed")

    def test_worker_lessons_are_required_and_strictly_validated(self) -> None:
        result: dict[str, object] = {
            "task_id": "TASK-001",
            "status": "completed",
            "summary": "done",
            "changed_files": [],
            "verification_results": [],
            "lessons": [lesson()],
            "blocker": None,
        }
        self.assertEqual(
            loopsail.validate_worker_result(result, "TASK-001")["lessons"], [lesson()]
        )

        missing = dict(result)
        missing.pop("lessons")
        with self.assertRaisesRegex(loopsail.LoopSailError, "invalid fields"):
            loopsail.validate_worker_result(missing, "TASK-001")

        too_many = dict(result)
        too_many["lessons"] = [lesson() for _ in range(loopsail.MAX_LESSONS_PER_ATTEMPT + 1)]
        with self.assertRaisesRegex(loopsail.LoopSailError, "at most"):
            loopsail.validate_worker_result(too_many, "TASK-001")

        unknown = dict(result)
        bad_lesson = lesson(extra="not allowed")
        unknown["lessons"] = [bad_lesson]
        with self.assertRaisesRegex(loopsail.LoopSailError, "invalid fields"):
            loopsail.validate_worker_result(unknown, "TASK-001")


class WorkerEnvTests(unittest.TestCase):
    def test_outer_claude_session_markers_are_removed_from_worker_environment(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        (root / "CLAUDE.md").write_text("# Test\n", encoding="utf-8")
        task_file = root / "TASKS.json"
        task_value = task("TASK-001")
        task_state = loopsail.new_task_state(task_value)
        task_state["attempts"] = 1
        config = json.loads(json.dumps(loopsail.DEFAULT_CONFIG))
        result = {
            "structured_output": {
                "task_id": "TASK-001",
                "status": "completed",
                "summary": "done",
                "changed_files": [],
                "verification_results": [],
                "lessons": [],
                "blocker": None,
            }
        }
        captured_env: dict[str, str] = {}

        def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            captured_env.update(kwargs["env"])  # type: ignore[arg-type]
            return subprocess.CompletedProcess(argv, 0, json.dumps(result), "")

        inherited = {
            "CLAUDECODE": "1",
            "CLAUDE_CODE_ENTRYPOINT": "outer-session",
            "CLAUDE_CODE_TEST_MARKER": "remove-me",
            "CLAUDE_CONFIG_DIR": "/tmp/test-claude-config",
            "ANTHROPIC_API_KEY": "test-anthropic-key",
        }
        with mock.patch.dict(os.environ, inherited), mock.patch.object(
            loopsail, "run_process", side_effect=fake_run
        ):
            loopsail.invoke_worker(root, task_file, task_value, task_state, config)

        self.assertNotIn("CLAUDECODE", captured_env)
        self.assertFalse(any(key.startswith("CLAUDE_CODE_") for key in captured_env))
        self.assertEqual(captured_env["CLAUDE_CONFIG_DIR"], inherited["CLAUDE_CONFIG_DIR"])
        self.assertEqual(captured_env["ANTHROPIC_API_KEY"], inherited["ANTHROPIC_API_KEY"])
        self.assertEqual(captured_env["LOOPSAIL_PROJECT_ROOT"], str(root))
        self.assertEqual(captured_env["LOOPSAIL_TASK_FILE"], str(task_file))
        self.assertEqual(captured_env["LOOPSAIL_TOOL_DIR"], str(loopsail.TOOL_ROOT))
        self.assertIn("LOOPSAIL_ALLOWED_PATHS", captured_env)
        self.assertIn("LOOPSAIL_PROTECTED_PATHS", captured_env)


class ExperienceLogTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def test_missing_symlink_and_non_utf8_logs_fail_closed(self) -> None:
        with self.assertRaisesRegex(loopsail.LoopSailError, "missing or unsafe"):
            loopsail.require_safe_experience_file(self.root)

        outside = self.root / "outside.md"
        outside.write_text("# Outside\n", encoding="utf-8")
        (self.root / loopsail.LESSONS_FILE).symlink_to(outside)
        with self.assertRaisesRegex(loopsail.LoopSailError, "missing or unsafe"):
            loopsail.require_safe_experience_file(self.root)

        (self.root / loopsail.LESSONS_FILE).unlink()
        (self.root / loopsail.LESSONS_FILE).write_bytes(b"\xff\xfe")
        with self.assertRaisesRegex(loopsail.LoopSailError, "UTF-8"):
            loopsail.require_safe_experience_file(self.root)

    def test_atomic_write_failure_is_reported_as_an_experience_error(self) -> None:
        (self.root / loopsail.LESSONS_FILE).write_text("# Experience\n", encoding="utf-8")
        with mock.patch.object(loopsail, "atomic_write_bytes", side_effect=OSError("read only")):
            with self.assertRaisesRegex(loopsail.LoopSailError, "cannot safely update"):
                loopsail.append_experience_record(
                    self.root,
                    task_list(task("TASK-001")),
                    task_id="TASK-001",
                    title="Test",
                    attempt=1,
                    outcome="阻塞",
                    stage="任务验证",
                    lessons=[],
                    failure="failed",
                )


class StepModeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.project = TemporaryGitProject()
        self.addCleanup(self.project.close)
        self.task_file = self.project.root / "TASKS.json"
        write_json(self.task_file, task_list(task("TASK-001")))
        previous = Path.cwd()
        os.chdir(self.project.root)
        self.addCleanup(os.chdir, previous)

    def call_once(self) -> tuple[int, dict[str, object], str]:
        output = io.StringIO()
        args = argparse.Namespace(
            task_list=self.task_file,
            runner_config=None,
            once=True,
        )
        with contextlib.redirect_stdout(output):
            exit_code = loopsail.command_run(args)
        stdout = output.getvalue()
        last_line = stdout.rstrip().splitlines()[-1]
        return exit_code, json.loads(last_line), stdout

    def initialize_state(
        self,
    ) -> tuple[dict[str, object], Path, Path, Path, bool]:
        task_file, normalized = loopsail.load_task_input(self.task_file, self.project.root)
        return loopsail.initialize_or_load_state(self.project.root, task_file, normalized)

    def assert_last_step(self, expected: dict[str, object]) -> None:
        report_path = (
            self.project.root
            / ".loopsail"
            / "runs"
            / "test-run"
            / loopsail.STEP_REPORT_FILE
        )
        self.assertEqual(loopsail.load_json(report_path), expected)

    @staticmethod
    def successful_worker(
        root: Path,
        task_file: Path,
        task_value: dict[str, object],
        task_state: dict[str, object],
        config: dict[str, object],
    ) -> tuple[dict[str, object], dict[str, object]]:
        changed = f"result-{task_value['id']}.txt"
        (root / changed).write_text("implemented\n", encoding="utf-8")
        return (
            {
                "task_id": task_value["id"],
                "status": "completed",
                "summary": "implemented",
                "changed_files": [changed],
                "verification_results": [],
                "lessons": [],
                "blocker": None,
            },
            {"exit_code": 0, "duration_seconds": 0.01},
        )

    @staticmethod
    def no_change_worker(
        root: Path,
        task_file: Path,
        task_value: dict[str, object],
        task_state: dict[str, object],
        config: dict[str, object],
    ) -> tuple[dict[str, object], dict[str, object]]:
        return (
            {
                "task_id": task_value["id"],
                "status": "completed",
                "summary": "verification will fail",
                "changed_files": [],
                "verification_results": [],
                "lessons": [],
                "blocker": None,
            },
            {"exit_code": 0, "duration_seconds": 0.01},
        )

    def test_once_advances_two_tasks_then_runs_final_verification(self) -> None:
        write_json(
            self.task_file,
            task_list(
                task("TASK-001"),
                task("TASK-002", dependencies=["TASK-001"]),
            ),
        )

        with mock.patch.object(
            loopsail, "invoke_worker", side_effect=self.successful_worker
        ) as worker:
            first_code, first, first_stdout = self.call_once()
            self.assertEqual(first_code, 3)
            self.assertEqual(first["exit_code"], 3)
            self.assertEqual(first["kind"], "attempt")
            self.assertTrue(first["performed"])
            self.assertEqual(first["project_status"], "executing")
            self.assertEqual(first["task"]["id"], "TASK-001")
            self.assertEqual(first["task"]["status"], "done")
            self.assertEqual(first["task"]["attempt"], 1)
            self.assertEqual(first["task"]["attempts"], 1)
            self.assertIsNotNone(first["task"]["commit"])
            self.assertIsNone(first["task"]["failure"])
            self.assertEqual(
                first["next_ready_task"], {"id": "TASK-002", "title": "TASK-002"}
            )
            self.assertEqual(first["tasks_remaining"], 1)
            self.assertEqual(json.loads(first_stdout.rstrip().splitlines()[-1]), first)
            self.assert_last_step(first)

            second_code, second, second_stdout = self.call_once()
            self.assertEqual(second_code, 3)
            self.assertEqual(second["exit_code"], 3)
            self.assertEqual(second["kind"], "attempt")
            self.assertEqual(second["task"]["id"], "TASK-002")
            self.assertEqual(second["task"]["status"], "done")
            self.assertEqual(second["tasks_remaining"], 0)
            self.assertIsNone(second["next_ready_task"])
            self.assertEqual(json.loads(second_stdout.rstrip().splitlines()[-1]), second)
            self.assert_last_step(second)

            final_code, final, final_stdout = self.call_once()
            self.assertEqual(final_code, 0)
            self.assertEqual(final["exit_code"], 0)
            self.assertEqual(final["kind"], "final-verification")
            self.assertTrue(final["performed"])
            self.assertEqual(final["project_status"], "complete")
            self.assertEqual(final["final_verification"]["status"], "passed")
            self.assertIsNone(final["final_verification"]["failure"])
            self.assertEqual(json.loads(final_stdout.rstrip().splitlines()[-1]), final)
            self.assert_last_step(final)

        self.assertEqual(worker.call_count, 2)

    def test_retryable_failure_then_repeated_fingerprint_blocks(self) -> None:
        value = task_list(task("TASK-001"))
        value["tasks"][0]["verify_commands"] = [
            command("python3", "-c", "raise SystemExit(9)")
        ]
        write_json(self.task_file, value)

        with mock.patch.object(
            loopsail, "invoke_worker", side_effect=self.no_change_worker
        ) as worker:
            first_code, first, _ = self.call_once()
            second_code, second, _ = self.call_once()

        self.assertEqual(first_code, 3)
        self.assertEqual(first["kind"], "attempt")
        self.assertEqual(first["task"]["status"], "pending")
        self.assertEqual(first["task"]["attempt"], 1)
        self.assertEqual(
            first["task"]["attempt_log"],
            loopsail.experience_log_reference("test-run", "TASK-001", 1),
        )
        self.assertEqual(second_code, 2)
        self.assertEqual(second["exit_code"], 2)
        self.assertEqual(second["kind"], "attempt")
        self.assertEqual(second["project_status"], "blocked")
        self.assertEqual(second["task"]["status"], "blocked")
        self.assertEqual(second["task"]["attempt"], 2)
        self.assertEqual(second["task"]["ai_retry_count"], 0)
        self.assertEqual(second["task"]["ai_retries_remaining"], 1)
        self.assertTrue(second["blocked_reason"])
        self.assert_last_step(second)
        self.assertEqual(worker.call_count, 2)

    def test_entry_blocked_is_structured_once_but_full_run_keeps_error(self) -> None:
        state, state_path, _, _, _ = self.initialize_state()
        item = state["tasks"]["TASK-001"]
        item.update(
            {
                "status": "blocked",
                "attempts": 1,
                "attempt_sequence": 1,
                "last_failure": {
                    "summary": "environment unavailable",
                    "fingerprint": "tree",
                    "recorded_at": loopsail.utc_now(),
                },
            }
        )
        state["active_task"] = "TASK-001"
        state["project_status"] = "blocked"
        loopsail.atomic_write_json(state_path, state)

        with mock.patch.object(loopsail, "invoke_worker") as worker:
            exit_code, report, _ = self.call_once()
        worker.assert_not_called()
        self.assertEqual(exit_code, 2)
        self.assertEqual(report["kind"], "blocked")
        self.assertFalse(report["performed"])
        self.assertEqual(report["task"]["id"], "TASK-001")
        self.assertEqual(report["blocked_reason"], "environment unavailable")
        self.assert_last_step(report)

        args = argparse.Namespace(
            task_list=self.task_file,
            runner_config=None,
            once=False,
        )
        with self.assertRaisesRegex(
            loopsail.LoopSailError,
            "run is blocked at TASK-001; update the list or use "
            "/loopsail:retry TASK-001",
        ):
            loopsail.command_run(args)

    def test_already_complete_report_contains_schema_required_keys(self) -> None:
        state, state_path, _, _, _ = self.initialize_state()
        state["tasks"]["TASK-001"].update(
            {"status": "done", "commit": state["base_commit"]}
        )
        state["project_status"] = "complete"
        state["active_task"] = None
        state["final_verification"] = {
            "status": "passed",
            "commands": [],
            "failure": None,
            "at": loopsail.utc_now(),
        }
        loopsail.atomic_write_json(state_path, state)

        with mock.patch.object(loopsail, "invoke_worker") as worker:
            exit_code, report, _ = self.call_once()
        worker.assert_not_called()
        self.assertEqual(exit_code, 0)
        self.assertEqual(report["kind"], "already-complete")
        self.assertFalse(report["performed"])
        self.assertEqual(report["project_status"], "complete")
        schema = loopsail.load_json(loopsail.SCHEMA_DIR / "step-report.schema.json")
        self.assertLessEqual(set(schema["required"]), set(report))
        self.assert_last_step(report)

    def test_idle_report_is_defensive_and_does_not_invoke_worker(self) -> None:
        write_json(
            self.task_file,
            task_list(
                task("TASK-001"),
                task("TASK-002", dependencies=["TASK-001"]),
            ),
        )
        state, state_path, _, _, _ = self.initialize_state()
        state["tasks"]["TASK-001"]["status"] = "superseded"
        state["tasks"]["TASK-002"]["status"] = "pending"
        state["project_status"] = "executing"
        state["active_task"] = None
        loopsail.atomic_write_json(state_path, state)

        with mock.patch.object(loopsail, "invoke_worker") as worker:
            exit_code, report, _ = self.call_once()
        worker.assert_not_called()
        self.assertEqual(exit_code, 4)
        self.assertEqual(report["exit_code"], 4)
        self.assertEqual(report["kind"], "idle")
        self.assertFalse(report["performed"])
        self.assertEqual(report["project_status"], "executing")
        self.assertEqual(report["tasks_remaining"], 1)
        self.assert_last_step(report)

    def test_resume_is_one_step_and_never_launches_a_new_worker(self) -> None:
        state, state_path, _, _, _ = self.initialize_state()
        state["tasks"]["TASK-001"].update(
            {"status": "running", "attempts": 1, "attempt_sequence": 1}
        )
        state["active_task"] = "TASK-001"
        loopsail.atomic_write_json(state_path, state)
        (self.project.root / "result.txt").write_text("recovered\n", encoding="utf-8")

        with mock.patch.object(loopsail, "invoke_worker") as worker:
            exit_code, report, _ = self.call_once()
        worker.assert_not_called()
        self.assertEqual(exit_code, 3)
        self.assertEqual(report["kind"], "resume")
        self.assertTrue(report["performed"])
        self.assertEqual(report["task"]["id"], "TASK-001")
        self.assertEqual(report["task"]["status"], "done")
        self.assertIsNotNone(report["task"]["commit"])
        self.assertEqual(report["tasks_remaining"], 0)
        self.assert_last_step(report)


class GitIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.project = TemporaryGitProject()
        self.addCleanup(self.project.close)
        self.task_file = self.project.root / "TASKS.json"
        write_json(self.task_file, task_list(task("TASK-001")))

    def test_run_creates_branch_verifies_and_commits(self) -> None:
        original_lessons = (self.project.root / loopsail.LESSONS_FILE).read_text(encoding="utf-8")

        def fake_worker(
            root: Path,
            task_file: Path,
            task_value: dict[str, object],
            task_state: dict[str, object],
            config: dict[str, object],
        ) -> tuple[dict[str, object], dict[str, object]]:
            (root / "result.txt").write_text("implemented\n", encoding="utf-8")
            return (
                {
                    "task_id": "TASK-001",
                    "status": "completed",
                    "summary": "implemented",
                    "changed_files": ["result.txt"],
                    "verification_results": [],
                    "lessons": [],
                    "blocker": None,
                },
                {"exit_code": 0, "duration_seconds": 0.01},
            )

        old_cwd = Path.cwd()
        os.chdir(self.project.root)
        self.addCleanup(os.chdir, old_cwd)
        args = argparse.Namespace(task_list=self.task_file, runner_config=None)
        with mock.patch.object(loopsail, "invoke_worker", side_effect=fake_worker):
            with contextlib.redirect_stdout(io.StringIO()):
                exit_code = loopsail.command_run(args)
        self.assertEqual(exit_code, 0)
        self.assertEqual(self.project.git("branch", "--show-current").stdout.strip(), "loopsail/test-run")
        message = self.project.git("show", "-s", "--format=%B", "HEAD").stdout
        self.assertIn("LoopSail-List: test-run", message)
        self.assertIn("LoopSail-Task: TASK-001", message)
        state = loopsail.load_json(self.project.root / ".loopsail" / "runs" / "test-run" / "state.json")
        self.assertEqual(state["project_status"], "complete")
        self.assertEqual(state["tasks"]["TASK-001"]["status"], "done")
        last_step = loopsail.load_json(
            self.project.root
            / ".loopsail"
            / "runs"
            / "test-run"
            / loopsail.STEP_REPORT_FILE
        )
        self.assertEqual(last_step["kind"], "final-verification")
        self.assertEqual(last_step["exit_code"], 0)
        self.assertEqual(last_step["project_status"], "complete")
        self.assertEqual(
            (self.project.root / loopsail.LESSONS_FILE).read_text(encoding="utf-8"),
            original_lessons,
        )

    def test_successful_task_records_sanitized_lessons_in_the_same_commit(self) -> None:
        private_root = str(self.project.root)

        def fake_worker(
            root: Path,
            task_file: Path,
            task_value: dict[str, object],
            task_state: dict[str, object],
            config: dict[str, object],
        ) -> tuple[dict[str, object], dict[str, object]]:
            (root / "result.txt").write_text("implemented\n", encoding="utf-8")
            return (
                {
                    "task_id": "TASK-001",
                    "status": "completed",
                    "summary": "implemented",
                    "changed_files": ["result.txt"],
                    "verification_results": [],
                    "lessons": [
                        lesson(
                            challenge=(
                                f"在 {private_root}/private.py 定位失败\n"
                                "<!-- injected --> token=super-secret-value，"
                                "并误读了 /opt/private/corpus.txt"
                            )
                        )
                    ],
                    "blocker": None,
                },
                {"exit_code": 0, "duration_seconds": 0.01},
            )

        old_cwd = Path.cwd()
        os.chdir(self.project.root)
        self.addCleanup(os.chdir, old_cwd)
        args = argparse.Namespace(task_list=self.task_file, runner_config=None)
        with mock.patch.object(loopsail, "invoke_worker", side_effect=fake_worker):
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(loopsail.command_run(args), 0)

        content = (self.project.root / loopsail.LESSONS_FILE).read_text(encoding="utf-8")
        self.assertIn("### 经验 1", content)
        self.assertIn("[project-root]/private.py", content)
        self.assertIn("token=[REDACTED]", content)
        self.assertNotIn(private_root, content)
        self.assertNotIn("injected", content)
        self.assertNotIn("super-secret-value", content)
        self.assertNotIn("/opt/private/corpus.txt", content)
        self.assertIn("[absolute-path]", content)
        committed = self.project.git(
            "-c", "core.quotePath=false", "show", "--pretty=", "--name-only", "HEAD"
        ).stdout.splitlines()
        self.assertIn("result.txt", committed)
        self.assertIn(loopsail.LESSONS_FILE, committed)

    def test_failed_attempts_are_recorded_without_changing_the_diff_fingerprint(self) -> None:
        value = task_list(task("TASK-001"))
        value["tasks"][0]["verify_commands"] = [
            command("python3", "-c", "raise SystemExit(9)")
        ]
        write_json(self.task_file, value)

        def fake_worker(
            root: Path,
            task_file: Path,
            task_value: dict[str, object],
            task_state: dict[str, object],
            config: dict[str, object],
        ) -> tuple[dict[str, object], dict[str, object]]:
            return (
                {
                    "task_id": "TASK-001",
                    "status": "completed",
                    "summary": "verification will fail",
                    "changed_files": [],
                    "verification_results": [],
                    "lessons": [],
                    "blocker": None,
                },
                {"exit_code": 0, "duration_seconds": 0.01},
            )

        old_cwd = Path.cwd()
        os.chdir(self.project.root)
        self.addCleanup(os.chdir, old_cwd)
        args = argparse.Namespace(task_list=self.task_file, runner_config=None)
        with mock.patch.object(loopsail, "invoke_worker", side_effect=fake_worker):
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(loopsail.command_run(args), 2)

        state = loopsail.load_json(
            self.project.root / ".loopsail" / "runs" / "test-run" / "state.json"
        )
        self.assertEqual(state["tasks"]["TASK-001"]["attempts"], 2)
        self.assertEqual(state["tasks"]["TASK-001"]["status"], "blocked")
        content = (self.project.root / loopsail.LESSONS_FILE).read_text(encoding="utf-8")
        self.assertEqual(content.count("### 自动失败记录"), 2)
        self.assertEqual(
            loopsail.diff_fingerprint(self.project.root, [loopsail.LESSONS_FILE]),
            hashlib.sha256().hexdigest(),
        )

    def test_worker_experience_log_mutation_blocks_immediately(self) -> None:
        def fake_worker(
            root: Path,
            task_file: Path,
            task_value: dict[str, object],
            task_state: dict[str, object],
            config: dict[str, object],
        ) -> tuple[dict[str, object], dict[str, object]]:
            (root / loopsail.LESSONS_FILE).write_text("tampered\n", encoding="utf-8")
            return (
                {
                    "task_id": "TASK-001",
                    "status": "completed",
                    "summary": "tampered",
                    "changed_files": [loopsail.LESSONS_FILE],
                    "verification_results": [],
                    "lessons": [],
                    "blocker": None,
                },
                {"exit_code": 0, "duration_seconds": 0.01},
            )

        old_cwd = Path.cwd()
        os.chdir(self.project.root)
        self.addCleanup(os.chdir, old_cwd)
        args = argparse.Namespace(task_list=self.task_file, runner_config=None)
        with mock.patch.object(loopsail, "invoke_worker", side_effect=fake_worker):
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(loopsail.command_run(args), 2)

        state = loopsail.load_json(
            self.project.root / ".loopsail" / "runs" / "test-run" / "state.json"
        )
        self.assertEqual(state["tasks"]["TASK-001"]["status"], "blocked")
        self.assertIn("experience log", state["tasks"]["TASK-001"]["last_failure"]["summary"])

    def test_worker_blocker_records_structured_lessons(self) -> None:
        def fake_worker(
            root: Path,
            task_file: Path,
            task_value: dict[str, object],
            task_state: dict[str, object],
            config: dict[str, object],
        ) -> tuple[dict[str, object], dict[str, object]]:
            return (
                {
                    "task_id": "TASK-001",
                    "status": "blocked",
                    "summary": "requirement is ambiguous",
                    "changed_files": [],
                    "verification_results": [],
                    "lessons": [
                        lesson(
                            challenge="接口错误语义不明确",
                            root_cause="任务没有规定兼容行为",
                            resolution=None,
                            takeaway="涉及公开接口兼容性时应在任务验收条件中明确错误语义",
                        )
                    ],
                    "blocker": "缺少公开接口错误语义",
                },
                {"exit_code": 0, "duration_seconds": 0.01},
            )

        old_cwd = Path.cwd()
        os.chdir(self.project.root)
        self.addCleanup(os.chdir, old_cwd)
        args = argparse.Namespace(task_list=self.task_file, runner_config=None)
        with mock.patch.object(loopsail, "invoke_worker", side_effect=fake_worker):
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(loopsail.command_run(args), 2)

        content = (self.project.root / loopsail.LESSONS_FILE).read_text(encoding="utf-8")
        self.assertIn("Worker 主动阻塞", content)
        self.assertIn("接口错误语义不明确", content)
        self.assertIn("尚未解决", content)

    def test_worker_process_failure_gets_an_automatic_record(self) -> None:
        old_cwd = Path.cwd()
        os.chdir(self.project.root)
        self.addCleanup(os.chdir, old_cwd)
        args = argparse.Namespace(task_list=self.task_file, runner_config=None)
        with mock.patch.object(
            loopsail, "invoke_worker", side_effect=loopsail.LoopSailError("launcher unavailable")
        ):
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(loopsail.command_run(args), 2)

        state = loopsail.load_json(
            self.project.root / ".loopsail" / "runs" / "test-run" / "state.json"
        )
        self.assertEqual(state["tasks"]["TASK-001"]["attempts"], 2)
        content = (self.project.root / loopsail.LESSONS_FILE).read_text(encoding="utf-8")
        self.assertEqual(content.count("### 自动失败记录"), 2)
        self.assertIn("Worker 进程未成功返回有效结构化结果", content)
        self.assertNotIn("launcher unavailable", content)

    def test_final_failure_requires_a_new_repair_task(self) -> None:
        value = task_list(task("TASK-001"))
        value["final_verify_commands"] = [command("python3", "-c", "raise SystemExit(4)")]
        write_json(self.task_file, value)

        def fake_worker(
            root: Path,
            task_file: Path,
            task_value: dict[str, object],
            task_state: dict[str, object],
            config: dict[str, object],
        ) -> tuple[dict[str, object], dict[str, object]]:
            (root / "result.txt").write_text("implemented\n", encoding="utf-8")
            return (
                {
                    "task_id": task_value["id"],
                    "status": "completed",
                    "summary": "implemented",
                    "changed_files": ["result.txt"],
                    "verification_results": [],
                    "lessons": [],
                    "blocker": None,
                },
                {"exit_code": 0, "duration_seconds": 0.01},
            )

        old_cwd = Path.cwd()
        os.chdir(self.project.root)
        self.addCleanup(os.chdir, old_cwd)
        args = argparse.Namespace(task_list=self.task_file, runner_config=None)
        with mock.patch.object(loopsail, "invoke_worker", side_effect=fake_worker):
            with contextlib.redirect_stdout(io.StringIO()):
                exit_code = loopsail.command_run(args)
        self.assertEqual(exit_code, 2)
        state = loopsail.load_json(self.project.root / ".loopsail" / "runs" / "test-run" / "state.json")
        self.assertEqual(state["project_status"], "blocked")
        self.assertIsNone(state["active_task"])
        lessons = (self.project.root / loopsail.LESSONS_FILE).read_text(encoding="utf-8")
        self.assertIn("最终验证阻塞", lessons)
        self.assertIn("FINAL-attempt-1.json", lessons)

        value["final_verify_commands"] = [command("python3", "-c", "raise SystemExit(0)")]
        write_json(self.task_file, value)
        with self.assertRaisesRegex(loopsail.LoopSailError, "requires a new repair task"):
            loopsail.initialize_or_load_state(self.project.root, self.task_file, loopsail.validate_task_list(value, self.project.root))

    def test_interrupted_running_diff_is_verified_and_committed_without_new_worker(self) -> None:
        old_cwd = Path.cwd()
        os.chdir(self.project.root)
        self.addCleanup(os.chdir, old_cwd)
        task_file, normalized = loopsail.load_task_input(self.task_file, self.project.root)
        state, state_path, _, _, _ = loopsail.initialize_or_load_state(
            self.project.root, task_file, normalized
        )
        item = state["tasks"]["TASK-001"]
        item["status"] = "running"
        item["attempts"] = 1
        state["active_task"] = "TASK-001"
        loopsail.atomic_write_json(state_path, state)
        (self.project.root / "result.txt").write_text("recovered\n", encoding="utf-8")
        args = argparse.Namespace(task_list=self.task_file, runner_config=None)
        with mock.patch.object(loopsail, "invoke_worker") as worker:
            with contextlib.redirect_stdout(io.StringIO()):
                exit_code = loopsail.command_run(args)
        self.assertEqual(exit_code, 0)
        worker.assert_not_called()
        message = self.project.git("show", "-s", "--format=%B", "HEAD").stdout
        self.assertIn("LoopSail-Task: TASK-001", message)
        committed_lessons = self.project.git("show", f"HEAD:{loopsail.LESSONS_FILE}").stdout
        self.assertIn("中断后恢复成功", committed_lessons)

    def test_worker_task_input_mutation_blocks_even_though_input_is_gitignored(self) -> None:
        def mutating_worker(
            root: Path,
            task_file: Path,
            task_value: dict[str, object],
            task_state: dict[str, object],
            config: dict[str, object],
        ) -> tuple[dict[str, object], dict[str, object]]:
            task_file.write_text("{}\n", encoding="utf-8")
            return (
                {
                    "task_id": "TASK-001",
                    "status": "completed",
                    "summary": "changed control input",
                    "changed_files": [],
                    "verification_results": [],
                    "lessons": [],
                    "blocker": None,
                },
                {"exit_code": 0, "duration_seconds": 0.01},
            )

        old_cwd = Path.cwd()
        os.chdir(self.project.root)
        self.addCleanup(os.chdir, old_cwd)
        args = argparse.Namespace(task_list=self.task_file, runner_config=None)
        with mock.patch.object(loopsail, "invoke_worker", side_effect=mutating_worker):
            with contextlib.redirect_stdout(io.StringIO()):
                exit_code = loopsail.command_run(args)
        self.assertEqual(exit_code, 2)
        state = loopsail.load_json(self.project.root / ".loopsail" / "runs" / "test-run" / "state.json")
        self.assertEqual(state["project_status"], "blocked")
        self.assertIn("protected task-list", state["tasks"]["TASK-001"]["last_failure"]["summary"])

    def test_human_retry_resets_only_a_blocked_task(self) -> None:
        old_cwd = Path.cwd()
        os.chdir(self.project.root)
        self.addCleanup(os.chdir, old_cwd)
        task_file, normalized = loopsail.load_task_input(self.task_file, self.project.root)
        state = loopsail.create_state(normalized, task_file, self.project.root)
        item = state["tasks"]["TASK-001"]
        item.update(
            {
                "status": "blocked",
                "attempts": 3,
                "last_failure": {"summary": "failure", "fingerprint": "tree"},
            }
        )
        state["active_task"] = "TASK-001"
        state["project_status"] = "blocked"
        state_path, _, _ = loopsail.state_paths(self.project.root, "test-run")
        loopsail.atomic_write_json(state_path, state)
        args = argparse.Namespace(
            task_list=self.task_file,
            task_id="TASK-001",
            reason="environment fixed",
        )
        with contextlib.redirect_stdout(io.StringIO()):
            exit_code = loopsail.command_retry(args)
        self.assertEqual(exit_code, 0)
        updated = loopsail.load_json(state_path)
        self.assertEqual(updated["tasks"]["TASK-001"]["status"], "pending")
        self.assertEqual(updated["tasks"]["TASK-001"]["attempts"], 0)
        self.assertEqual(updated["tasks"]["TASK-001"]["attempt_sequence"], 3)
        self.assertEqual(
            updated["tasks"]["TASK-001"]["last_failure"],
            {"summary": "failure", "fingerprint": "tree"},
        )
        self.assertEqual(updated["project_status"], "executing")


class RetryActorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.project = TemporaryGitProject()
        self.addCleanup(self.project.close)
        self.task_file = self.project.root / "TASKS.json"
        write_json(self.task_file, task_list(task("TASK-001")))
        previous = Path.cwd()
        os.chdir(self.project.root)
        self.addCleanup(os.chdir, previous)

    def write_blocked_state(self, *, ai_retry_count: int | None = 0) -> Path:
        task_file, normalized = loopsail.load_task_input(self.task_file, self.project.root)
        state = loopsail.create_state(normalized, task_file, self.project.root)
        item = state["tasks"]["TASK-001"]
        item.update(
            {
                "status": "blocked",
                "attempts": 3,
                "attempt_sequence": 3,
                "last_failure": {
                    "summary": "transient failure",
                    "fingerprint": "tree",
                    "recorded_at": loopsail.utc_now(),
                },
            }
        )
        if ai_retry_count is None:
            item.pop("ai_retry_count", None)
        else:
            item["ai_retry_count"] = ai_retry_count
        state["active_task"] = "TASK-001"
        state["project_status"] = "blocked"
        state_path, _, _ = loopsail.state_paths(self.project.root, "test-run")
        loopsail.atomic_write_json(state_path, state)
        return state_path

    def call_retry(self, actor: str) -> int:
        args = argparse.Namespace(
            task_list=self.task_file,
            task_id="TASK-001",
            reason="environment recovered",
            actor=actor,
        )
        with contextlib.redirect_stdout(io.StringIO()):
            return loopsail.command_retry(args)

    def test_ai_retry_succeeds_once_and_second_request_is_rejected(self) -> None:
        state_path = self.write_blocked_state()

        self.assertEqual(self.call_retry("ai"), 0)
        updated = loopsail.load_json(state_path)
        item = updated["tasks"]["TASK-001"]
        self.assertEqual(item["status"], "pending")
        self.assertEqual(item["attempts"], 0)
        self.assertEqual(item["attempt_sequence"], 3)
        self.assertEqual(item["ai_retry_count"], 1)
        self.assertEqual(item["last_failure"]["summary"], "transient failure")
        retry_log = loopsail.load_json(
            self.project.root
            / ".loopsail"
            / "logs"
            / "test-run"
            / "TASK-001-attempt-0.json"
        )
        self.assertEqual(retry_log["status"], "ai-retry")
        self.assertEqual(retry_log["actor"], "ai")
        self.assertEqual(retry_log["ai_retry_count"], 1)

        item["status"] = "blocked"
        item["last_failure"] = {
            "summary": "failed again",
            "fingerprint": "tree-2",
            "recorded_at": loopsail.utc_now(),
        }
        updated["active_task"] = "TASK-001"
        updated["project_status"] = "blocked"
        loopsail.atomic_write_json(state_path, updated)

        with self.assertRaisesRegex(loopsail.LoopSailError, "AI retry limit"):
            self.call_retry("ai")
        rejected = loopsail.load_json(state_path)
        self.assertEqual(rejected["tasks"]["TASK-001"]["status"], "blocked")
        self.assertEqual(rejected["tasks"]["TASK-001"]["ai_retry_count"], 1)

    def test_human_retry_resets_ai_quota(self) -> None:
        state_path = self.write_blocked_state(ai_retry_count=1)

        self.assertEqual(self.call_retry("human"), 0)

        updated = loopsail.load_json(state_path)
        self.assertEqual(updated["tasks"]["TASK-001"]["status"], "pending")
        self.assertEqual(updated["tasks"]["TASK-001"]["ai_retry_count"], 0)
        self.assertEqual(
            updated["tasks"]["TASK-001"]["last_failure"]["summary"],
            "transient failure",
        )
        retry_log = loopsail.load_json(
            self.project.root
            / ".loopsail"
            / "logs"
            / "test-run"
            / "TASK-001-attempt-0.json"
        )
        self.assertEqual(retry_log["status"], "human-retry")
        self.assertEqual(retry_log["actor"], "human")
        self.assertEqual(retry_log["ai_retry_count"], 0)

    def test_old_state_without_ai_retry_count_is_treated_as_zero(self) -> None:
        state_path = self.write_blocked_state(ai_retry_count=None)

        self.assertEqual(self.call_retry("ai"), 0)

        updated = loopsail.load_json(state_path)
        self.assertEqual(updated["tasks"]["TASK-001"]["ai_retry_count"], 1)


class StatusTests(unittest.TestCase):
    def setUp(self) -> None:
        self.project = TemporaryGitProject()
        self.addCleanup(self.project.close)
        self.task_file = self.project.root / "TASKS.json"
        write_json(self.task_file, task_list(task("TASK-001")))
        previous = Path.cwd()
        os.chdir(self.project.root)
        self.addCleanup(os.chdir, previous)

    def test_blocked_status_includes_supervisor_diagnostics(self) -> None:
        task_file, normalized = loopsail.load_task_input(self.task_file, self.project.root)
        state = loopsail.create_state(normalized, task_file, self.project.root)
        failure = {
            "summary": "verification failed",
            "fingerprint": "tree",
            "recorded_at": loopsail.utc_now(),
        }
        final_verification = {
            "status": "failed",
            "commands": [],
            "failure": "final verification failed",
            "at": loopsail.utc_now(),
        }
        state["tasks"]["TASK-001"].update(
            {
                "status": "blocked",
                "attempts": 2,
                "last_failure": failure,
                "ai_retry_count": 1,
            }
        )
        state["active_task"] = "TASK-001"
        state["project_status"] = "blocked"
        state["final_verification"] = final_verification
        state_path, _, _ = loopsail.state_paths(self.project.root, "test-run")
        loopsail.atomic_write_json(state_path, state)

        output = io.StringIO()
        args = argparse.Namespace(task_list=self.task_file, runner_config=None)
        with contextlib.redirect_stdout(output):
            self.assertEqual(loopsail.command_status(args), 0)
        status = json.loads(output.getvalue())

        self.assertEqual(status["active_task"], "TASK-001")
        self.assertEqual(status["final_verification"], final_verification)
        self.assertEqual(status["tasks"][0]["last_failure"], "verification failed")
        self.assertEqual(status["tasks"][0]["ai_retry_count"], 1)


if __name__ == "__main__":
    unittest.main()
