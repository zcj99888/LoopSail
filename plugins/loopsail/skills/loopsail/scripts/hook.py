#!/usr/bin/env python3
"""Claude Code hooks for LoopSail's in-session worker subagent."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Iterator


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import guard  # noqa: E402
from protocol import (  # noqa: E402
    ProtocolError,
    parse_json_document,
    utc_now,
    validate_worker_request,
    validate_worker_result,
)


WORKER_TYPE = "loopsail:worker"
TOOL_ROOT = SCRIPT_DIR.parent
DEFAULT_EVENT_LIMIT = 5 * 1024 * 1024
STATE_FIELDS = {
    "schema_version", "kind", "list_id", "project", "task_file", "branch",
    "base_commit", "project_status", "active_task", "active_request",
    "last_finalized_request", "final_verification", "final_verification_attempts",
    "tasks", "created_at", "updated_at",
}
LEASE_FIELDS = {
    "request_id", "list_id", "task_id", "attempt", "request_path", "output_path",
    "event_log_path", "task_input_hash", "task_definition_hash", "experience_hash",
    "head_commit", "index_hash",
    "agent_id", "session_id", "started_at", "captured_at", "correction_used",
    "last_protocol_error", "protocol_failure", "result_status", "event_sequence",
    "event_truncated", "event_log_max_bytes", "created_at",
    "fatal_error",
}


class HookError(RuntimeError):
    pass


def load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise HookError(f"cannot read valid LoopSail state: {path}") from exc
    if not isinstance(value, dict):
        raise HookError(f"LoopSail state must be an object: {path}")
    return value


def atomic_write_json(path: Path, value: dict[str, Any]) -> None:
    if path.is_symlink() or any(parent.is_symlink() for parent in path.parents):
        raise HookError(f"refusing to write through a symbolic link: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def project_root(cwd: str | None) -> Path:
    start = Path(cwd or os.getcwd()).resolve()
    result = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=start,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0 or not result.stdout.strip():
        raise HookError("loopsail:worker must run inside its prepared Git project")
    return Path(result.stdout.strip()).resolve()


@contextlib.contextmanager
def project_lock(root: Path) -> Iterator[None]:
    path = root / ".loopsail" / "lock"
    if path.parent.is_symlink() or path.is_symlink():
        raise HookError("loopsail lock path must not be a symbolic link")
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+", encoding="utf-8")
    try:
        try:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        except ImportError as exc:
            raise HookError("platform does not support LoopSail locking") from exc
        yield
    finally:
        with contextlib.suppress(Exception):
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def active_state(root: Path) -> tuple[Path, dict[str, Any], dict[str, Any]]:
    candidates: list[tuple[Path, dict[str, Any], dict[str, Any]]] = []
    for path in sorted((root / ".loopsail" / "runs").glob("*/state.json")):
        state = load_json(path)
        lease = state.get("active_request")
        if (
            state.get("schema_version") == 2
            and state.get("kind") == "run-state"
            and set(state) == STATE_FIELDS
            and isinstance(lease, dict)
            and set(lease) == LEASE_FIELDS
            and state.get("active_task")
        ):
            candidates.append((path, state, lease))
    if len(candidates) != 1:
        raise HookError(
            "loopsail:worker has no unique valid active request; all tool calls are denied"
        )
    return candidates[0]


def request_for(root: Path, lease: dict[str, Any]) -> dict[str, Any]:
    request_path = root / str(lease.get("request_path", ""))
    request = validate_worker_request(load_json(request_path))
    for field in ("request_id", "list_id", "task_id", "attempt"):
        if request[field] != lease.get(field):
            raise HookError(f"active request binding mismatch: {field}")
    return request


def require_agent(payload: dict[str, Any], lease: dict[str, Any]) -> str:
    agent_id = payload.get("agent_id")
    if not isinstance(agent_id, str) or not agent_id:
        raise HookError("worker hook input has no agent_id")
    if payload.get("agent_type") != WORKER_TYPE:
        raise HookError("worker hook received the wrong agent_type")
    if lease.get("agent_id") != agent_id:
        raise HookError("worker agent_id does not match the active request lease")
    return agent_id


def event_target_paths(
    root: Path, tool_name: str | None, tool_input: Any
) -> list[str]:
    if tool_name == "Bash" or not isinstance(tool_input, dict):
        return []
    values = [
        value
        for key, value in tool_input.items()
        if key in {"file_path", "path", "notebook_path"}
        and isinstance(value, str)
    ]
    paths: set[str] = set()
    for value in values:
        relative = guard.normalize_project_path(root, value)
        if relative is not None:
            paths.add(relative)
    return sorted(paths)[:64]


def command_metadata(tool_name: str | None, tool_input: Any) -> tuple[str | None, str | None]:
    if tool_name != "Bash" or not isinstance(tool_input, dict):
        return None, None
    command = str(tool_input.get("command", ""))
    digest = hashlib.sha256(command.encode("utf-8", errors="replace")).hexdigest()
    first = command.strip().split(maxsplit=1)[0] if command.strip() else ""
    category = Path(first).name.lower()[:64] or "empty"
    if category == "git":
        category = "git-read" if not any(
            word in command.lower()
            for word in (" commit", " add", " push", " reset", " clean", " checkout", " switch")
        ) else "git-mutation"
    return category, digest


def append_event(
    root: Path,
    state_path: Path,
    state: dict[str, Any],
    lease: dict[str, Any],
    *,
    hook_event: str,
    outcome: str,
    agent_id: str,
    tool_name: str | None = None,
    tool_input: Any = None,
) -> None:
    if lease.get("event_truncated"):
        return
    event_path = root / str(lease["event_log_path"])
    if event_path.is_symlink() or any(parent.is_symlink() for parent in event_path.parents):
        raise HookError("refusing to append events through a symbolic link")
    event_path.parent.mkdir(parents=True, exist_ok=True)
    limit = int(lease.get("event_log_max_bytes", DEFAULT_EVENT_LIMIT))
    current_size = event_path.stat().st_size if event_path.exists() else 0
    if current_size >= limit:
        hook_event = "log_truncated"
        outcome = "truncated"
        tool_name = None
        tool_input = None
        lease["event_truncated"] = True
    sequence = int(lease.get("event_sequence", 0)) + 1
    category, command_hash = command_metadata(tool_name, tool_input)
    event = {
        "schema_version": 2,
        "kind": "worker-event",
        "at": utc_now(),
        "sequence": sequence,
        "request_id": lease["request_id"],
        "list_id": lease["list_id"],
        "task_id": lease["task_id"],
        "attempt": lease["attempt"],
        "agent_id": agent_id,
        "agent_type": WORKER_TYPE,
        "hook_event": hook_event,
        "tool_name": tool_name,
        "target_paths": event_target_paths(root, tool_name, tool_input),
        "command_category": category,
        "command_sha256": command_hash,
        "outcome": outcome,
    }
    encoded = (json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n").encode(
        "utf-8"
    )
    with event_path.open("ab") as handle:
        handle.write(encoded)
        handle.flush()
        os.fsync(handle.fileno())
    lease["event_sequence"] = sequence
    state["updated_at"] = utc_now()
    atomic_write_json(state_path, state)


def denial(reason: str) -> None:
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": reason,
                }
            },
            ensure_ascii=False,
        )
    )


def allowance() -> None:
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "allow",
                    "permissionDecisionReason": (
                        "bound loopsail:worker call passed the active request guard"
                    ),
                }
            },
            ensure_ascii=False,
        )
    )


def subagent_start(payload: dict[str, Any]) -> int:
    if payload.get("agent_type") != WORKER_TYPE:
        return 0
    root = project_root(payload.get("cwd"))
    with project_lock(root):
        state_path, state, lease = active_state(root)
        agent_id = payload.get("agent_id")
        if not isinstance(agent_id, str) or not agent_id:
            raise HookError("SubagentStart has no agent_id")
        bound = lease.get("agent_id")
        if bound is not None and bound != agent_id:
            lease["fatal_error"] = "active request received a different worker agent_id"
            state["updated_at"] = utc_now()
            atomic_write_json(state_path, state)
            raise HookError("active request is already bound to another worker agent")
        lease["agent_id"] = agent_id
        lease["session_id"] = payload.get("session_id")
        lease["started_at"] = utc_now()
        request_for(root, lease)
        atomic_write_json(state_path, state)
        append_event(
            root,
            state_path,
            state,
            lease,
            hook_event="SubagentStart",
            outcome="started",
            agent_id=agent_id,
        )
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "SubagentStart",
                    "additionalContext": (
                        "LoopSail has bound this agent to the immutable request at "
                        f"{lease['request_path']}. Read exactly that request first, execute only "
                        "the bound task, and finish with one worker-result v2 JSON document."
                    ),
                }
            },
            ensure_ascii=False,
        )
    )
    return 0


def pre_tool_use(payload: dict[str, Any]) -> int:
    if payload.get("agent_type") != WORKER_TYPE:
        return 0
    root = project_root(payload.get("cwd"))
    with project_lock(root):
        state_path, state, lease = active_state(root)
        try:
            agent_id = require_agent(payload, lease)
        except HookError as exc:
            lease["fatal_error"] = f"agent binding failure: {exc}"
            state["updated_at"] = utc_now()
            atomic_write_json(state_path, state)
            raise
        request = request_for(root, lease)
        context = {
            "root": root,
            "tool_root": TOOL_ROOT,
            "request_path": lease["request_path"],
            "task_file": state["task_file"],
            "allowed_paths": request["policy"]["allowed_paths"],
            "protected_paths": request["policy"]["protected_paths"],
        }
        tool_name = str(payload.get("tool_name", ""))
        tool_input = payload.get("tool_input", {})
        reason = guard.decide(tool_name, tool_input, context)
        append_event(
            root,
            state_path,
            state,
            lease,
            hook_event="PreToolUse",
            outcome="denied" if reason else "allowed",
            agent_id=agent_id,
            tool_name=tool_name,
            tool_input=tool_input,
        )
    if reason:
        denial(reason)
    else:
        allowance()
    return 0


def post_tool(payload: dict[str, Any], hook_event: str) -> int:
    if payload.get("agent_type") != WORKER_TYPE:
        return 0
    root = project_root(payload.get("cwd"))
    with project_lock(root):
        state_path, state, lease = active_state(root)
        try:
            agent_id = require_agent(payload, lease)
        except HookError as exc:
            lease["fatal_error"] = f"agent binding failure: {exc}"
            state["updated_at"] = utc_now()
            atomic_write_json(state_path, state)
            raise
        append_event(
            root,
            state_path,
            state,
            lease,
            hook_event=hook_event,
            outcome="succeeded" if hook_event == "PostToolUse" else "failed",
            agent_id=agent_id,
            tool_name=str(payload.get("tool_name", "")) or None,
            tool_input=payload.get("tool_input", {}),
        )
    return 0


def synthetic_protocol_failure(request: dict[str, Any], message: str) -> dict[str, Any]:
    return {
        "schema_version": 2,
        "kind": "worker-result",
        "request_id": request["request_id"],
        "list_id": request["list_id"],
        "task_id": request["task_id"],
        "attempt": request["attempt"],
        "status": "blocked",
        "summary": "Worker failed the result protocol after one correction.",
        "changed_files": [],
        "verification_results": [],
        "lessons": [],
        "blocker": f"worker_protocol_invalid_after_correction: {message}"[:2000],
    }


def capture_result(path: Path, result: dict[str, Any]) -> None:
    if path.exists():
        existing = load_json(path)
        if existing != result:
            raise HookError("worker output path already contains a different result")
        return
    atomic_write_json(path, result)


def subagent_stop(payload: dict[str, Any]) -> int:
    if payload.get("agent_type") != WORKER_TYPE:
        return 0
    root = project_root(payload.get("cwd"))
    block_reason: str | None = None
    with project_lock(root):
        state_path, state, lease = active_state(root)
        try:
            agent_id = require_agent(payload, lease)
        except HookError as exc:
            lease["fatal_error"] = f"agent binding failure: {exc}"
            state["updated_at"] = utc_now()
            atomic_write_json(state_path, state)
            raise
        request = request_for(root, lease)
        protocol_failure = False
        try:
            message = payload.get("last_assistant_message")
            if not isinstance(message, str):
                raise ProtocolError("invalid_json", "final message is missing")
            result = validate_worker_result(parse_json_document(message), binding=request)
        except ProtocolError as exc:
            if not lease.get("correction_used") and not payload.get("stop_hook_active"):
                lease["correction_used"] = True
                lease["last_protocol_error"] = str(exc)
                atomic_write_json(state_path, state)
                append_event(
                    root,
                    state_path,
                    state,
                    lease,
                    hook_event="SubagentStop",
                    outcome="corrected",
                    agent_id=agent_id,
                )
                block_reason = (
                    "Your final response did not satisfy worker-result v2: "
                    f"{exc}. Return exactly one JSON object with no prose or code fence, "
                    "preserving request_id/list_id/task_id/attempt from the request. "
                    "Use only project-relative POSIX paths in changed_files (for example "
                    "src/module.py), never absolute paths or paths containing '..'."
                )
                result = {}
            else:
                result = synthetic_protocol_failure(request, str(exc))
                protocol_failure = True
        if block_reason is None:
            capture_result(root / lease["output_path"], result)
            lease["captured_at"] = utc_now()
            lease["protocol_failure"] = protocol_failure
            lease["result_status"] = result["status"]
            atomic_write_json(state_path, state)
            append_event(
                root,
                state_path,
                state,
                lease,
                hook_event="SubagentStop",
                outcome="protocol_failed" if protocol_failure else "captured",
                agent_id=agent_id,
            )
    if block_reason is not None:
        print(json.dumps({"decision": "block", "reason": block_reason}, ensure_ascii=False))
    return 0


HANDLERS = {
    "subagent-start": subagent_start,
    "pre-tool-use": pre_tool_use,
    "post-tool-use": lambda payload: post_tool(payload, "PostToolUse"),
    "post-tool-use-failure": lambda payload: post_tool(payload, "PostToolUseFailure"),
    "subagent-stop": subagent_stop,
}


def main(argv: list[str] | None = None) -> int:
    arguments = argv if argv is not None else sys.argv[1:]
    if len(arguments) != 1 or arguments[0] not in HANDLERS:
        print("invalid loopsail hook action", file=sys.stderr)
        return 2
    try:
        payload = json.load(sys.stdin)
        if not isinstance(payload, dict):
            raise HookError("hook input must be an object")
        return HANDLERS[arguments[0]](payload)
    except (HookError, ProtocolError, OSError, ValueError) as exc:
        # Exit 2 is a fail-closed hook error in Claude Code.
        print(f"LoopSail hook denied operation: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
