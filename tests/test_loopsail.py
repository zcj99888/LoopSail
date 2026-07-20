from __future__ import annotations

import argparse
import contextlib
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
SKILL_ROOT = PROJECT_ROOT / "plugins" / "loopsail" / "skills" / "loopsail"
SCRIPT_ROOT = SKILL_ROOT / "scripts"
if str(SCRIPT_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPT_ROOT))


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


protocol = load_module("loopsail_protocol_test", SCRIPT_ROOT / "protocol.py")
loopsail = load_module("loopsail_runtime_test", SCRIPT_ROOT / "loopsail.py")
hook = load_module("loopsail_hook_test", SCRIPT_ROOT / "hook.py")


def write_json(path: Path, value: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def command(*argv: str) -> dict[str, object]:
    return {"argv": list(argv), "cwd": ".", "timeout_seconds": 30}


def task(task_id: str = "T-001", **overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "id": task_id,
        "title": f"Task {task_id}",
        "description": f"Implement {task_id}",
        "depends_on": [],
        "context_files": ["CLAUDE.md"],
        "allowed_paths": ["result*.txt"],
        "acceptance": ["result.txt contains ok"],
        "verify_commands": [
            command(
                "python3",
                "-c",
                "from pathlib import Path; raise SystemExit(Path('result.txt').read_text().strip() != 'ok')",
            )
        ],
    }
    value.update(overrides)
    return value


def task_list(
    *tasks: dict[str, object],
    list_id: str = "test-list",
    final_passes: bool = True,
) -> dict[str, object]:
    return {
        "schema_version": 2,
        "kind": "task-list",
        "list_id": list_id,
        "project": "Test Project",
        "final_verify_commands": [
            command("python3", "-c", f"raise SystemExit({0 if final_passes else 1})")
        ],
        "tasks": list(tasks or (task(),)),
    }


def lesson() -> dict[str, object]:
    return {
        "challenge": "A subtle boundary",
        "detour": None,
        "root_cause": "Missing protocol binding",
        "resolution": "Bound the result to the request",
        "takeaway": "Validate identity and content independently",
    }


@contextlib.contextmanager
def cwd(path: Path):
    previous = Path.cwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(previous)


class GitProject:
    def __init__(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.git("init", "-b", "main")
        self.git("config", "user.name", "Test")
        self.git("config", "user.email", "test@example.invalid")
        (self.root / ".gitignore").write_text(
            "/TASKS.json\n.loopsail/input/\n.loopsail/output/\n"
            ".loopsail/runs/\n.loopsail/logs/\n.loopsail/lock\n",
            encoding="utf-8",
        )
        (self.root / "CLAUDE.md").write_text("# Test project\n", encoding="utf-8")
        (self.root / loopsail.LESSONS_FILE).write_text(
            "# Test experience\n", encoding="utf-8"
        )
        self.git("add", ".")
        self.git("commit", "-m", "initial")
        self.write_tasks(task_list())

    def git(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", "-C", str(self.root), *args],
            check=check,
            text=True,
            capture_output=True,
        )

    def write_tasks(self, value: dict[str, object]) -> None:
        write_json(self.root / "TASKS.json", value)

    def call(self, *arguments: str) -> tuple[int, dict[str, object], str]:
        output = io.StringIO()
        errors = io.StringIO()
        with cwd(self.root), mock.patch.dict(
            os.environ, {"HOME": str(self.home)}, clear=False
        ), contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            code = loopsail.main(list(arguments))
        lines = output.getvalue().splitlines()
        if len(lines) != 1:
            raise AssertionError(f"expected one stdout line, got {lines!r}")
        envelope = json.loads(lines[0])
        return code, envelope, errors.getvalue()

    def state(self, list_id: str = "test-list") -> dict[str, object]:
        return json.loads(
            (
                self.root / ".loopsail" / "runs" / list_id / "state.json"
            ).read_text(encoding="utf-8")
        )

    def request(self) -> dict[str, object]:
        lease = self.state()["active_request"]
        assert isinstance(lease, dict)
        return json.loads((self.root / lease["request_path"]).read_text(encoding="utf-8"))

    def start_agent(self, agent_id: str = "agent-1") -> None:
        payload = {
            "hook_event_name": "SubagentStart",
            "agent_type": "loopsail:worker",
            "agent_id": agent_id,
            "session_id": "session-1",
            "cwd": str(self.root),
        }
        with contextlib.redirect_stdout(io.StringIO()):
            self.assert_hook_ok(hook.subagent_start(payload))

    @staticmethod
    def assert_hook_ok(value: int) -> None:
        if value != 0:
            raise AssertionError(f"hook returned {value}")

    def worker_result(
        self,
        *,
        status: str = "completed",
        blocker: str | None = None,
        changed_files: list[str] | None = None,
    ) -> dict[str, object]:
        request = self.request()
        return {
            "schema_version": 2,
            "kind": "worker-result",
            "request_id": request["request_id"],
            "list_id": request["list_id"],
            "task_id": request["task_id"],
            "attempt": request["attempt"],
            "status": status,
            "summary": "implemented" if status == "completed" else "blocked",
            "changed_files": changed_files or [],
            "verification_results": [],
            "lessons": [lesson()],
            "blocker": blocker,
        }

    def stop_agent(
        self,
        value: dict[str, object] | str,
        *,
        agent_id: str = "agent-1",
        stop_hook_active: bool = False,
    ) -> str:
        message = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        payload = {
            "hook_event_name": "SubagentStop",
            "agent_type": "loopsail:worker",
            "agent_id": agent_id,
            "session_id": "session-1",
            "cwd": str(self.root),
            "last_assistant_message": message,
            "stop_hook_active": stop_hook_active,
        }
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assert_hook_ok(hook.subagent_stop(payload))
        return output.getvalue()

    def close(self) -> None:
        self.temporary.cleanup()


class ProjectTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.project = GitProject()
        self.addCleanup(self.project.close)

    def prepare(self) -> dict[str, object]:
        code, envelope, stderr = self.project.call("slash", "prepare-step")
        self.assertEqual(code, 3, envelope)
        self.assertEqual(stderr, "")
        self.assertTrue(envelope["ok"])
        self.assertEqual(envelope["data"]["action"], "spawn_worker")
        return envelope["data"]

    def complete_worker(self, content: str = "ok\n") -> dict[str, object]:
        self.project.start_agent()
        (self.project.root / "result.txt").write_text(content, encoding="utf-8")
        result_value = self.project.worker_result(changed_files=["result.txt"])
        self.assertEqual(self.project.stop_agent(result_value), "")
        return result_value


class ProtocolTests(unittest.TestCase):
    def test_checked_in_schemas_are_generated_draft7_documents(self) -> None:
        documents = protocol.schema_documents()
        self.assertGreaterEqual(len(documents), 10)
        for name, generated in documents.items():
            checked = json.loads(
                (SKILL_ROOT / "references" / name).read_text(encoding="utf-8")
            )
            self.assertEqual(checked, generated, name)
            self.assertEqual(
                checked["$schema"], "http://json-schema.org/draft-07/schema#", name
            )
            self.assertNotIn("$defs", json.dumps(checked), name)

    def test_worker_result_is_strict_and_bound(self) -> None:
        binding = {
            "request_id": "r",
            "list_id": "l",
            "task_id": "T-001",
            "attempt": 1,
        }
        value = {
            "schema_version": 2,
            "kind": "worker-result",
            **binding,
            "status": "completed",
            "summary": "ok",
            "changed_files": ["a.txt"],
            "verification_results": [],
            "lessons": [],
            "blocker": None,
        }
        self.assertIs(protocol.validate_worker_result(value, binding=binding), value)
        for mutation in (
            {**value, "extra": True},
            {key: item for key, item in value.items() if key != "summary"},
            {**value, "request_id": "wrong"},
            {**value, "status": "blocked", "blocker": None},
        ):
            with self.subTest(mutation=mutation):
                with self.assertRaises(protocol.ProtocolError):
                    protocol.validate_worker_result(mutation, binding=binding)

    def test_worker_request_rejects_missing_extra_and_task_binding(self) -> None:
        value = {
            "schema_version": 2,
            "kind": "worker-request",
            "request_id": "r",
            "list_id": "list",
            "project": "project",
            "branch": "loopsail/list",
            "task_id": "T-001",
            "attempt": 1,
            "task": {"id": "T-001"},
            "previous_failure": None,
            "policy": {
                "allowed_paths": [],
                "protected_paths": [".loopsail/**"],
                "request_path": ".loopsail/input/list/r.json",
            },
            "created_at": "2026-01-01T00:00:00+00:00",
        }
        self.assertIs(protocol.validate_worker_request(value), value)
        for mutation in (
            {**value, "extra": True},
            {key: item for key, item in value.items() if key != "policy"},
            {**value, "task": {"id": "T-002"}},
        ):
            with self.subTest(mutation=mutation):
                with self.assertRaises(protocol.ProtocolError):
                    protocol.validate_worker_request(mutation)

    def test_task_list_v1_is_explicitly_unsupported(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            old = task_list()
            old.pop("kind")
            old["schema_version"] = 1
            with self.assertRaises(loopsail.LoopSailError) as caught:
                loopsail.validate_task_list(old, root)
            self.assertEqual(caught.exception.code, "unsupported_schema_version")

    def test_config_v2_accepts_only_three_business_fields(self) -> None:
        value = {
            "schema_version": 2,
            "kind": "loopsail-config",
            "protected_paths": ["generated/**"],
            "verification_output_limit_bytes": 10,
            "event_log_max_bytes": 20,
        }
        loopsail.validate_config(value)
        for legacy in ("claude", "worker_timeout_seconds", "max_budget_usd"):
            with self.subTest(legacy=legacy):
                with self.assertRaises(loopsail.LoopSailError):
                    loopsail.validate_config({**value, legacy: {}})

    def test_task_dependencies_cycles_and_context_are_semantic(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "CLAUDE.md").write_text("# rules\n", encoding="utf-8")
            valid = task_list(
                task("A-001"),
                task("A-002", depends_on=["A-001"]),
            )
            normalized = loopsail.validate_task_list(valid, root)
            self.assertEqual(normalized["tasks"][1]["depends_on"], ["A-001"])
            unknown = task_list(task("A-001", depends_on=["missing"]))
            with self.assertRaisesRegex(loopsail.LoopSailError, "unknown dependency"):
                loopsail.validate_task_list(unknown, root)
            cycle = task_list(
                task("A-001", depends_on=["A-002"]),
                task("A-002", depends_on=["A-001"]),
            )
            with self.assertRaisesRegex(loopsail.LoopSailError, "cycle"):
                loopsail.validate_task_list(cycle, root)


class EnvelopeTests(ProjectTestCase):
    def test_actions_emit_one_envelope_and_matching_exit_code(self) -> None:
        actions = [
            ("doctor",),
            ("slash", "validate"),
            ("slash", "status"),
            ("slash", "init-check"),
        ]
        for action in actions:
            with self.subTest(action=action):
                code, envelope, stderr = self.project.call(*action)
                self.assertEqual(stderr, "")
                self.assertEqual(code, envelope["exit_code"])
                self.assertEqual(envelope["kind"], "command-envelope")
                self.assertEqual(set(envelope), {
                    "schema_version", "kind", "ok", "exit_code", "at", "data", "error"
                })

    def test_expected_error_is_also_an_envelope(self) -> None:
        code, envelope, stderr = self.project.call("slash", "finalize-step")
        self.assertEqual(code, 2)
        self.assertEqual(stderr, "")
        self.assertFalse(envelope["ok"])
        self.assertEqual(envelope["error"]["code"], "loopsail_error")

    def test_unsupported_v1_error_has_stable_code(self) -> None:
        old = task_list()
        old["schema_version"] = 1
        old.pop("kind")
        self.project.write_tasks(old)
        code, envelope, stderr = self.project.call("slash", "validate")
        self.assertEqual(code, 2)
        self.assertEqual(stderr, "")
        self.assertEqual(envelope["error"]["code"], "unsupported_schema_version")


class PrepareFinalizeTests(ProjectTestCase):
    def test_success_uses_actual_diff_verifies_and_commits(self) -> None:
        report = self.prepare()
        request = self.project.request()
        self.assertEqual(request["kind"], "worker-request")
        self.assertEqual(request["task_id"], "T-001")
        self.assertEqual(report["request_path"], request["policy"]["request_path"])
        self.complete_worker()

        code, envelope, _ = self.project.call("slash", "finalize-step")
        self.assertEqual(code, 3, envelope)
        self.assertEqual(envelope["data"]["action"], "finalized")
        state = self.project.state()
        item = state["tasks"]["T-001"]
        self.assertEqual(item["status"], "done")
        self.assertIsNone(state["active_request"])
        self.assertEqual(
            self.project.git("show", "--format=%B", "--no-patch", "HEAD").stdout.splitlines()[0],
            "loopsail(T-001): Task T-001",
        )
        attempt = json.loads(
            (
                self.project.root
                / ".loopsail/logs/test-list/T-001-attempt-1.json"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(attempt["actual_diff"], ["result.txt"])
        self.assertEqual(attempt["status"], "done")
        self.assertEqual(attempt["agent_id"], "agent-1")

        before = self.project.git("rev-list", "--count", "HEAD").stdout.strip()
        code, envelope, _ = self.project.call("slash", "finalize-step")
        after = self.project.git("rev-list", "--count", "HEAD").stdout.strip()
        self.assertEqual(code, 3)
        self.assertFalse(envelope["data"]["performed"])
        self.assertEqual(before, after)

        code, envelope, _ = self.project.call("slash", "prepare-step")
        self.assertEqual(code, 0, envelope)
        self.assertEqual(envelope["data"]["action"], "complete")
        self.assertEqual(self.project.state()["project_status"], "complete")
        final_log = json.loads(
            (
                self.project.root / ".loopsail/logs/test-list/FINAL-attempt-1.json"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(final_log["kind"], "attempt-log")
        self.assertEqual(final_log["status"], "done")

    def test_worker_blocker_is_immediate(self) -> None:
        self.prepare()
        self.project.start_agent()
        value = self.project.worker_result(status="blocked", blocker="needs credentials")
        self.project.stop_agent(value)
        code, envelope, _ = self.project.call("slash", "finalize-step")
        self.assertEqual(code, 2)
        self.assertEqual(envelope["error"]["code"], "worker_blocked")
        self.assertEqual(self.project.state()["tasks"]["T-001"]["status"], "blocked")

    def test_missing_result_retries_once_then_repeated_fingerprint_blocks(self) -> None:
        self.prepare()
        code, envelope, _ = self.project.call("slash", "finalize-step")
        self.assertEqual(code, 3, envelope)
        self.assertEqual(self.project.state()["tasks"]["T-001"]["status"], "pending")
        self.prepare()
        code, envelope, _ = self.project.call("slash", "finalize-step")
        self.assertEqual(code, 2)
        self.assertEqual(envelope["error"]["code"], "worker_result_missing")
        self.assertEqual(self.project.state()["tasks"]["T-001"]["status"], "blocked")

    def test_three_distinct_verification_failures_reach_attempt_limit(self) -> None:
        for attempt in range(1, 4):
            self.prepare()
            self.complete_worker(content=f"wrong-{attempt}\n")
            code, envelope, _ = self.project.call("slash", "finalize-step")
            self.assertEqual(code, 2 if attempt == 3 else 3, envelope)
        item = self.project.state()["tasks"]["T-001"]
        self.assertEqual(item["attempts"], 3)
        self.assertEqual(item["status"], "blocked")

    def test_prepare_detects_orphaned_lease_and_preserves_diff(self) -> None:
        self.prepare()
        (self.project.root / "result.txt").write_text("partial\n", encoding="utf-8")
        code, envelope, _ = self.project.call("slash", "prepare-step")
        self.assertEqual(code, 2)
        self.assertEqual(envelope["error"]["code"], "orphaned_attempt_lease")
        self.assertTrue((self.project.root / "result.txt").is_file())
        self.assertEqual(self.project.state()["tasks"]["T-001"]["status"], "blocked")

    def test_active_lease_blocks_a_second_task_list(self) -> None:
        self.prepare()
        self.project.write_tasks(task_list(task(), list_id="other-list"))
        code, envelope, _ = self.project.call("slash", "prepare-step")
        self.assertEqual(code, 2)
        self.assertEqual(envelope["error"]["code"], "concurrent_attempt_lease")

    def test_scope_violation_blocks_even_if_worker_reports_no_files(self) -> None:
        self.prepare()
        self.project.start_agent()
        (self.project.root / "outside.md").write_text("bad\n", encoding="utf-8")
        self.project.stop_agent(self.project.worker_result())
        code, envelope, _ = self.project.call("slash", "finalize-step")
        self.assertEqual(code, 2)
        self.assertEqual(envelope["error"]["code"], "scope_violation")
        self.assertIn("outside.md", envelope["error"]["message"])

    def test_worker_git_mutation_is_detected_authoritatively(self) -> None:
        self.prepare()
        self.project.start_agent()
        (self.project.root / "result.txt").write_text("ok\n", encoding="utf-8")
        self.project.git("add", "result.txt")
        self.project.git("commit", "-m", "worker must not commit")
        self.project.stop_agent(
            self.project.worker_result(changed_files=["result.txt"])
        )
        code, envelope, _ = self.project.call("slash", "finalize-step")
        self.assertEqual(code, 2)
        self.assertEqual(envelope["error"]["code"], "git_state_changed")

    def test_task_and_experience_integrity_are_authoritative(self) -> None:
        for target, expected in (("task", "task_input_changed"), ("experience", "experience_changed")):
            with self.subTest(target=target):
                project = GitProject()
                try:
                    code, envelope, _ = project.call("slash", "prepare-step")
                    self.assertEqual(code, 3, envelope)
                    project.start_agent()
                    (project.root / "result.txt").write_text("ok\n", encoding="utf-8")
                    project.stop_agent(project.worker_result(changed_files=["result.txt"]))
                    if target == "task":
                        value = task_list(task(description="changed while running"))
                        project.write_tasks(value)
                    else:
                        (project.root / loopsail.LESSONS_FILE).write_text(
                            "# changed by worker\n", encoding="utf-8"
                        )
                    code, envelope, _ = project.call("slash", "finalize-step")
                    self.assertEqual(code, 2)
                    self.assertEqual(envelope["error"]["code"], expected)
                finally:
                    project.close()

    def test_verification_and_final_verification_fail_closed(self) -> None:
        self.prepare()
        self.complete_worker(content="wrong\n")
        code, envelope, _ = self.project.call("slash", "finalize-step")
        self.assertEqual(code, 3)
        self.assertFalse(envelope["data"]["blocked_reason"])
        self.assertEqual(self.project.state()["tasks"]["T-001"]["status"], "pending")

        other = GitProject()
        try:
            other.write_tasks(task_list(task(), final_passes=False))
            code, _, _ = other.call("slash", "prepare-step")
            self.assertEqual(code, 3)
            other.start_agent()
            (other.root / "result.txt").write_text("ok\n", encoding="utf-8")
            other.stop_agent(other.worker_result(changed_files=["result.txt"]))
            code, _, _ = other.call("slash", "finalize-step")
            self.assertEqual(code, 3)
            code, envelope, _ = other.call("slash", "prepare-step")
            self.assertEqual(code, 2)
            self.assertEqual(envelope["error"]["code"], "final_verification_failed")
            self.assertEqual(other.state()["project_status"], "blocked")
        finally:
            other.close()


class HookTests(ProjectTestCase):
    def test_worker_without_active_request_fails_closed(self) -> None:
        payload = {
            "agent_type": "loopsail:worker",
            "agent_id": "agent-1",
            "cwd": str(self.project.root),
            "tool_name": "Read",
            "tool_input": {"file_path": str(self.project.root / "CLAUDE.md")},
        }
        with self.assertRaises(hook.HookError):
            hook.pre_tool_use(payload)

    def test_agent_binding_guard_and_other_agents(self) -> None:
        self.prepare()
        request_path = self.project.state()["active_request"]["request_path"]
        self.project.start_agent()
        allowed = {
            "agent_type": "loopsail:worker",
            "agent_id": "agent-1",
            "cwd": str(self.project.root),
            "tool_name": "Read",
            "tool_input": {"file_path": str(self.project.root / request_path)},
        }
        allowed_output = io.StringIO()
        with contextlib.redirect_stdout(allowed_output):
            self.assertEqual(hook.pre_tool_use(allowed), 0)
        self.assertEqual(
            json.loads(allowed_output.getvalue())["hookSpecificOutput"][
                "permissionDecision"
            ],
            "allow",
        )

        denied = {**allowed, "tool_input": {
            "file_path": str(self.project.root / ".loopsail/runs/test-list/state.json")
        }}
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(hook.pre_tool_use(denied), 0)
        self.assertEqual(
            json.loads(output.getvalue())["hookSpecificOutput"]["permissionDecision"],
            "deny",
        )

        wrong = {**allowed, "agent_id": "wrong"}
        with self.assertRaises(hook.HookError):
            hook.pre_tool_use(wrong)
        self.assertIn(
            "binding failure",
            self.project.state()["active_request"]["fatal_error"],
        )

        other = {**allowed, "agent_type": "general-purpose", "agent_id": "other"}
        self.assertEqual(hook.pre_tool_use(other), 0)

    def test_events_are_sanitized_and_truncated_once(self) -> None:
        self.prepare()
        state_path = self.project.root / ".loopsail/runs/test-list/state.json"
        state = self.project.state()
        state["active_request"]["event_log_max_bytes"] = 1
        write_json(state_path, state)
        self.project.start_agent()
        payload = {
            "agent_type": "loopsail:worker",
            "agent_id": "agent-1",
            "cwd": str(self.project.root),
            "tool_name": "Bash",
            "tool_input": {"command": "git diff -- secret-token-value"},
        }
        with contextlib.redirect_stdout(io.StringIO()):
            hook.pre_tool_use(payload)
            hook.post_tool(payload, "PostToolUse")
        lease = self.project.state()["active_request"]
        text = (self.project.root / lease["event_log_path"]).read_text(encoding="utf-8")
        events = [json.loads(line) for line in text.splitlines()]
        self.assertEqual(sum(e["hook_event"] == "log_truncated" for e in events), 1)
        self.assertNotIn("git diff", text)
        self.assertNotIn("secret-token-value", text)
        self.assertTrue(all("tool_output" not in event for event in events))

    def test_invalid_result_gets_one_correction_then_protocol_block(self) -> None:
        self.prepare()
        self.project.start_agent()
        first = self.project.stop_agent("not json")
        decision = json.loads(first)
        self.assertEqual(decision["decision"], "block")
        self.assertIn("worker-result v2", decision["reason"])

        second = self.project.stop_agent("still not json", stop_hook_active=True)
        self.assertEqual(second, "")
        lease = self.project.state()["active_request"]
        captured = json.loads(
            (self.project.root / lease["output_path"]).read_text(encoding="utf-8")
        )
        self.assertEqual(captured["status"], "blocked")
        self.assertTrue(lease["protocol_failure"])

        code, envelope, _ = self.project.call("slash", "finalize-step")
        self.assertEqual(code, 2)
        self.assertEqual(envelope["error"]["code"], "worker_protocol_failure")


class RetryAndStateTests(ProjectTestCase):
    def block_worker(self) -> None:
        self.prepare()
        self.project.start_agent()
        self.project.stop_agent(
            self.project.worker_result(status="blocked", blocker="transient")
        )
        code, _, _ = self.project.call("slash", "finalize-step")
        self.assertEqual(code, 2)

    def test_ai_retry_once_and_human_retry_resets_quota(self) -> None:
        self.block_worker()
        code, envelope, _ = self.project.call(
            "retry", "TASKS.json", "T-001", "--reason", "transient", "--actor", "ai"
        )
        self.assertEqual(code, 0, envelope)
        self.assertEqual(envelope["data"]["ai_retry_count"], 1)

        self.block_worker()
        code, envelope, _ = self.project.call(
            "retry", "TASKS.json", "T-001", "--reason", "again", "--actor", "ai"
        )
        self.assertEqual(code, 2)
        self.assertIn("AI retry limit", envelope["error"]["message"])

        code, envelope, _ = self.project.call(
            "retry", "TASKS.json", "T-001", "--reason", "confirmed", "--actor", "human"
        )
        self.assertEqual(code, 0, envelope)
        self.assertEqual(envelope["data"]["ai_retry_count"], 0)

    def test_corrupt_state_extra_field_is_rejected(self) -> None:
        self.prepare()
        state_path = self.project.root / ".loopsail/runs/test-list/state.json"
        state = self.project.state()
        state["unexpected"] = True
        write_json(state_path, state)
        code, envelope, _ = self.project.call("slash", "status")
        self.assertEqual(code, 2)
        self.assertEqual(envelope["error"]["code"], "invalid_run_state")

    def test_config_layers_require_v2_and_replace_arrays(self) -> None:
        user = self.project.home / ".loopsail/config.json"
        project = self.project.root / ".loopsail/config.json"
        write_json(
            user,
            {
                "schema_version": 2,
                "kind": "loopsail-config",
                "protected_paths": ["user/**"],
            },
        )
        write_json(
            project,
            {
                "schema_version": 2,
                "kind": "loopsail-config",
                "protected_paths": ["project/**"],
                "event_log_max_bytes": 1234,
            },
        )
        with mock.patch.dict(os.environ, {"HOME": str(self.project.home)}, clear=False):
            config, sources = loopsail.load_config(self.project.root)
        self.assertEqual(config["protected_paths"], ["project/**"])
        self.assertEqual(config["event_log_max_bytes"], 1234)
        self.assertEqual(len(sources), 3)

    def test_project_lock_remains_as_one_persistent_inode(self) -> None:
        lock_path = self.project.root / ".loopsail/lock"
        lock_path.parent.mkdir(exist_ok=True)
        with loopsail.project_lock(self.project.root):
            inode = lock_path.stat().st_ino
            with self.assertRaises(loopsail.LoopSailError):
                with loopsail.project_lock(self.project.root):
                    pass
        self.assertTrue(lock_path.is_file())
        self.assertEqual(lock_path.stat().st_ino, inode)


class InitTests(unittest.TestCase):
    def test_init_is_idempotent_and_adds_v2_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            subprocess.run(["git", "init", "-b", "main"], cwd=root, check=True, capture_output=True)
            subprocess.run(["git", "config", "user.name", "Test"], cwd=root, check=True)
            subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=root, check=True)
            (root / "README.md").write_text("# seed\n", encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=root, check=True)
            subprocess.run(["git", "commit", "-m", "seed"], cwd=root, check=True, capture_output=True)
            home = root / "home"
            home.mkdir()
            def invoke() -> dict[str, object]:
                out = io.StringIO()
                with cwd(root), mock.patch.dict(os.environ, {"HOME": str(home)}, clear=False), contextlib.redirect_stdout(out):
                    self.assertEqual(loopsail.main(["init"]), 0)
                return json.loads(out.getvalue())
            first = invoke()
            second = invoke()
            self.assertIn("TASKS.json", first["data"]["created"])
            self.assertIn("TASKS.json", second["data"]["preserved"])
            tasks = json.loads((root / "TASKS.json").read_text(encoding="utf-8"))
            self.assertEqual(tasks["schema_version"], 2)
            self.assertEqual(tasks["kind"], "task-list")
            ignore = (root / ".gitignore").read_text(encoding="utf-8")
            self.assertIn(".loopsail/output/", ignore)

    def test_unsafe_symlink_fails_before_partial_writes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            subprocess.run(["git", "init", "-b", "main"], cwd=root, check=True, capture_output=True)
            outside = root.parent / f"{root.name}-outside"
            outside.write_text("keep\n", encoding="utf-8")
            (root / "CLAUDE.md").symlink_to(outside)
            self.addCleanup(outside.unlink)
            with self.assertRaises(loopsail.LoopSailError):
                loopsail.initialize_scaffold(root)
            self.assertEqual(outside.read_text(encoding="utf-8"), "keep\n")
            self.assertFalse((root / "LOOP.md").exists())


if __name__ == "__main__":
    unittest.main()
