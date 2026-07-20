#!/usr/bin/env python3
"""LoopSail protocol v2 constants, schema builders, and strict validators."""

from __future__ import annotations

import datetime as dt
import json
import re
from pathlib import PurePosixPath
from typing import Any, Callable


SCHEMA_VERSION = 2
DRAFT_07 = "http://json-schema.org/draft-07/schema#"
ID_PATTERN = r"^[A-Za-z][A-Za-z0-9._-]{0,63}$"
ID_RE = re.compile(ID_PATTERN)
MAX_LESSONS = 10
MAX_LESSON_LENGTH = 2000


class ProtocolError(ValueError):
    """A stable, user-safe protocol validation error."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def command_envelope(
    *,
    ok: bool,
    exit_code: int,
    data: dict[str, Any] | None = None,
    error: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": "command-envelope",
        "ok": ok,
        "exit_code": exit_code,
        "at": utc_now(),
        "data": data,
        "error": error,
    }


def exact_fields(
    value: Any,
    *,
    kind: str,
    required: set[str],
    optional: set[str] | None = None,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ProtocolError("invalid_protocol", f"{kind} must be an object")
    missing = required - set(value)
    extra = set(value) - required - (optional or set())
    if missing or extra:
        parts: list[str] = []
        if missing:
            parts.append("missing " + ", ".join(sorted(missing)))
        if extra:
            parts.append("unexpected " + ", ".join(sorted(extra)))
        raise ProtocolError("invalid_protocol", f"invalid {kind} fields: {'; '.join(parts)}")
    return value


def require_v2_kind(value: dict[str, Any], kind: str) -> None:
    version = value.get("schema_version")
    if version != SCHEMA_VERSION:
        raise ProtocolError(
            "unsupported_schema_version",
            f"{kind} schema_version {version!r} is unsupported; expected 2",
        )
    if value.get("kind") != kind:
        raise ProtocolError("invalid_protocol", f"kind must be {kind!r}")


def relative_path(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise ProtocolError("invalid_protocol", f"{field} must be a non-empty relative path")
    normalized = value.replace("\\", "/").removeprefix("./")
    path = PurePosixPath(normalized)
    if path.is_absolute() or ".." in path.parts:
        raise ProtocolError("invalid_protocol", f"{field} must stay within the project")
    return normalized


def string_list(
    value: Any,
    field: str,
    *,
    unique: bool = False,
    max_items: int | None = None,
) -> list[str]:
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item.strip() for item in value
    ):
        raise ProtocolError("invalid_protocol", f"{field} must be an array of non-empty strings")
    if unique and len(set(value)) != len(value):
        raise ProtocolError("invalid_protocol", f"{field} must contain unique values")
    if max_items is not None and len(value) > max_items:
        raise ProtocolError("invalid_protocol", f"{field} has too many items")
    return list(value)


LESSON_FIELDS = {"challenge", "detour", "root_cause", "resolution", "takeaway"}


def validate_lessons(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list) or len(value) > MAX_LESSONS:
        raise ProtocolError("invalid_protocol", "lessons must be an array with at most 10 items")
    for index, lesson in enumerate(value):
        exact_fields(lesson, kind=f"lessons[{index}]", required=LESSON_FIELDS)
        for field in ("challenge", "takeaway"):
            text = lesson[field]
            if not isinstance(text, str) or not text.strip() or len(text) > MAX_LESSON_LENGTH:
                raise ProtocolError("invalid_protocol", f"lessons[{index}].{field} is invalid")
        for field in ("detour", "root_cause", "resolution"):
            text = lesson[field]
            if text is not None and (
                not isinstance(text, str) or not text.strip() or len(text) > MAX_LESSON_LENGTH
            ):
                raise ProtocolError("invalid_protocol", f"lessons[{index}].{field} is invalid")
    return value


WORKER_REQUEST_FIELDS = {
    "schema_version",
    "kind",
    "request_id",
    "list_id",
    "project",
    "branch",
    "task_id",
    "attempt",
    "task",
    "previous_failure",
    "policy",
    "created_at",
}
WORKER_RESULT_FIELDS = {
    "schema_version",
    "kind",
    "request_id",
    "list_id",
    "task_id",
    "attempt",
    "status",
    "summary",
    "changed_files",
    "verification_results",
    "lessons",
    "blocker",
}


def validate_worker_request(value: Any) -> dict[str, Any]:
    request = exact_fields(value, kind="worker-request", required=WORKER_REQUEST_FIELDS)
    require_v2_kind(request, "worker-request")
    for field in ("request_id", "list_id", "project", "branch", "task_id", "created_at"):
        if not isinstance(request[field], str) or not request[field].strip():
            raise ProtocolError("invalid_protocol", f"worker-request.{field} is invalid")
    if not isinstance(request["attempt"], int) or isinstance(request["attempt"], bool) or request["attempt"] < 1:
        raise ProtocolError("invalid_protocol", "worker-request.attempt must be a positive integer")
    if not isinstance(request["task"], dict) or request["task"].get("id") != request["task_id"]:
        raise ProtocolError("binding_mismatch", "worker-request task binding is invalid")
    if request["previous_failure"] is not None and not isinstance(request["previous_failure"], dict):
        raise ProtocolError("invalid_protocol", "worker-request.previous_failure is invalid")
    policy = request["policy"]
    exact_fields(
        policy,
        kind="worker-request.policy",
        required={"allowed_paths", "protected_paths", "request_path"},
    )
    for field in ("allowed_paths", "protected_paths"):
        string_list(policy[field], f"policy.{field}", unique=True)
    relative_path(policy["request_path"], "policy.request_path")
    return request


def validate_worker_result(
    value: Any,
    *,
    binding: dict[str, Any] | None = None,
) -> dict[str, Any]:
    result = exact_fields(value, kind="worker-result", required=WORKER_RESULT_FIELDS)
    require_v2_kind(result, "worker-result")
    for field in ("request_id", "list_id", "task_id", "summary"):
        if not isinstance(result[field], str) or (field != "summary" and not result[field].strip()):
            raise ProtocolError("invalid_protocol", f"worker-result.{field} is invalid")
    if not isinstance(result["attempt"], int) or isinstance(result["attempt"], bool) or result["attempt"] < 1:
        raise ProtocolError("invalid_protocol", "worker-result.attempt must be a positive integer")
    if result["status"] not in {"completed", "blocked"}:
        raise ProtocolError("invalid_protocol", "worker-result.status is invalid")
    paths = string_list(result["changed_files"], "changed_files", unique=True)
    for index, path in enumerate(paths):
        relative_path(path, f"changed_files[{index}]")
    if not isinstance(result["verification_results"], list):
        raise ProtocolError("invalid_protocol", "verification_results must be an array")
    for index, record in enumerate(result["verification_results"]):
        exact_fields(
            record,
            kind=f"verification_results[{index}]",
            required={"argv", "exit_code", "summary"},
        )
        string_list(record["argv"], f"verification_results[{index}].argv")
        if record["exit_code"] is not None and (
            not isinstance(record["exit_code"], int) or isinstance(record["exit_code"], bool)
        ):
            raise ProtocolError("invalid_protocol", f"verification_results[{index}].exit_code is invalid")
        if not isinstance(record["summary"], str):
            raise ProtocolError("invalid_protocol", f"verification_results[{index}].summary is invalid")
    validate_lessons(result["lessons"])
    blocker = result["blocker"]
    if blocker is not None and (not isinstance(blocker, str) or not blocker.strip()):
        raise ProtocolError("invalid_protocol", "worker-result.blocker is invalid")
    if result["status"] == "blocked" and blocker is None:
        raise ProtocolError("invalid_protocol", "blocked worker-result requires blocker")
    if result["status"] == "completed" and blocker is not None:
        raise ProtocolError("invalid_protocol", "completed worker-result requires blocker=null")
    if binding:
        for field in ("request_id", "list_id", "task_id", "attempt"):
            if result[field] != binding[field]:
                raise ProtocolError("binding_mismatch", f"worker-result.{field} does not match active request")
    return result


def parse_json_document(text: str) -> dict[str, Any]:
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ProtocolError("invalid_json", f"final message must be one JSON document: {exc.msg}") from exc
    if not isinstance(value, dict):
        raise ProtocolError("invalid_json", "final message JSON root must be an object")
    return value


def _object(
    title: str,
    properties: dict[str, Any],
    required: list[str],
    *,
    definitions: dict[str, Any] | None = None,
) -> dict[str, Any]:
    value: dict[str, Any] = {
        "$schema": DRAFT_07,
        "title": title,
        "type": "object",
        "additionalProperties": False,
        "required": required,
        "properties": properties,
    }
    if definitions:
        value["definitions"] = definitions
    return value


def _metadata(kind: str) -> dict[str, Any]:
    return {
        "schema_version": {"const": 2},
        "kind": {"const": kind},
    }


def _command_definition() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["argv"],
        "properties": {
            "argv": {"type": "array", "minItems": 1, "items": {"type": "string", "minLength": 1}},
            "cwd": {"type": "string", "default": "."},
            "timeout_seconds": {"type": "integer", "minimum": 1, "default": 900},
        },
    }


def _lesson_definition() -> dict[str, Any]:
    nullable_text = {"type": ["string", "null"], "minLength": 1, "maxLength": 2000}
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["challenge", "detour", "root_cause", "resolution", "takeaway"],
        "properties": {
            "challenge": {"type": "string", "minLength": 1, "maxLength": 2000},
            "detour": nullable_text,
            "root_cause": nullable_text,
            "resolution": nullable_text,
            "takeaway": {"type": "string", "minLength": 1, "maxLength": 2000},
        },
    }


def task_list_schema() -> dict[str, Any]:
    task = {
        "type": "object",
        "additionalProperties": False,
        "required": ["id", "title", "description", "depends_on", "context_files", "acceptance", "verify_commands"],
        "properties": {
            "id": {"type": "string", "pattern": ID_PATTERN},
            "title": {"type": "string", "minLength": 1},
            "description": {"type": "string", "minLength": 1},
            "depends_on": {"type": "array", "uniqueItems": True, "items": {"type": "string", "minLength": 1}},
            "context_files": {"type": "array", "minItems": 1, "uniqueItems": True, "items": {"type": "string", "minLength": 1}},
            "acceptance": {"type": "array", "minItems": 1, "items": {"type": "string", "minLength": 1}},
            "verify_commands": {"type": "array", "minItems": 1, "items": {"$ref": "#/definitions/command"}},
            "allowed_paths": {"type": "array", "minItems": 1, "uniqueItems": True, "items": {"type": "string", "minLength": 1}},
            "source_refs": {"type": "array", "uniqueItems": True, "items": {"type": "string", "minLength": 1}},
            "non_goals": {"type": "array", "items": {"type": "string", "minLength": 1}},
            "stop_conditions": {"type": "array", "items": {"type": "string", "minLength": 1}},
        },
    }
    properties = {
        **_metadata("task-list"),
        "$schema": {"type": "string", "minLength": 1},
        "list_id": {"type": "string", "pattern": ID_PATTERN},
        "project": {"type": "string", "minLength": 1},
        "final_verify_commands": {"type": "array", "minItems": 1, "items": {"$ref": "#/definitions/command"}},
        "tasks": {"type": "array", "minItems": 1, "items": {"$ref": "#/definitions/task"}},
    }
    return _object(
        "LoopSail Task List v2",
        properties,
        ["schema_version", "kind", "list_id", "project", "final_verify_commands", "tasks"],
        definitions={"command": _command_definition(), "task": task},
    )


def config_schema() -> dict[str, Any]:
    properties = {
        **_metadata("loopsail-config"),
        "protected_paths": {"type": "array", "uniqueItems": True, "items": {"type": "string", "minLength": 1}},
        "verification_output_limit_bytes": {"type": "integer", "minimum": 1, "default": 65536},
        "event_log_max_bytes": {"type": "integer", "minimum": 1, "default": 5242880},
    }
    return _object("LoopSail Configuration v2", properties, ["schema_version", "kind"])


def worker_request_schema() -> dict[str, Any]:
    properties = {
        **_metadata("worker-request"),
        "request_id": {"type": "string", "minLength": 1},
        "list_id": {"type": "string", "pattern": ID_PATTERN},
        "project": {"type": "string", "minLength": 1},
        "branch": {"type": "string", "minLength": 1},
        "task_id": {"type": "string", "pattern": ID_PATTERN},
        "attempt": {"type": "integer", "minimum": 1},
        "task": {"type": "object"},
        "previous_failure": {"type": ["object", "null"]},
        "policy": {
            "type": "object",
            "additionalProperties": False,
            "required": ["allowed_paths", "protected_paths", "request_path"],
            "properties": {
                "allowed_paths": {"type": "array", "uniqueItems": True, "items": {"type": "string", "minLength": 1}},
                "protected_paths": {"type": "array", "uniqueItems": True, "items": {"type": "string", "minLength": 1}},
                "request_path": {"type": "string", "minLength": 1},
            },
        },
        "created_at": {"type": "string", "format": "date-time"},
    }
    return _object("LoopSail Worker Request v2", properties, sorted(WORKER_REQUEST_FIELDS))


def worker_result_schema() -> dict[str, Any]:
    verification = {
        "type": "object",
        "additionalProperties": False,
        "required": ["argv", "exit_code", "summary"],
        "properties": {
            "argv": {"type": "array", "minItems": 1, "items": {"type": "string"}},
            "exit_code": {"type": ["integer", "null"]},
            "summary": {"type": "string"},
        },
    }
    properties = {
        **_metadata("worker-result"),
        "request_id": {"type": "string", "minLength": 1},
        "list_id": {"type": "string", "minLength": 1},
        "task_id": {"type": "string", "minLength": 1},
        "attempt": {"type": "integer", "minimum": 1},
        "status": {"enum": ["completed", "blocked"]},
        "summary": {"type": "string"},
        "changed_files": {"type": "array", "uniqueItems": True, "items": {"type": "string", "minLength": 1}},
        "verification_results": {"type": "array", "items": {"$ref": "#/definitions/verification-result"}},
        "lessons": {"type": "array", "maxItems": 10, "items": {"$ref": "#/definitions/lesson"}},
        "blocker": {"type": ["string", "null"]},
    }
    return _object(
        "LoopSail Worker Result v2",
        properties,
        sorted(WORKER_RESULT_FIELDS),
        definitions={"verification-result": verification, "lesson": _lesson_definition()},
    )


def generic_schema(kind: str, title: str, fields: dict[str, Any]) -> dict[str, Any]:
    properties = {**_metadata(kind), **fields}
    return _object(title, properties, list(properties))


def schema_documents() -> dict[str, dict[str, Any]]:
    nullable_string = {"type": ["string", "null"]}
    documents = {
        "task-list.schema.json": task_list_schema(),
        "config.schema.json": config_schema(),
        "worker-request.schema.json": worker_request_schema(),
        "worker-result.schema.json": worker_result_schema(),
        "worker-event.schema.json": generic_schema(
            "worker-event",
            "LoopSail Worker Event v2",
            {
                "at": {"type": "string", "format": "date-time"},
                "sequence": {"type": "integer", "minimum": 1},
                "request_id": {"type": "string", "minLength": 1},
                "list_id": {"type": "string", "minLength": 1},
                "task_id": {"type": "string", "minLength": 1},
                "attempt": {"type": "integer", "minimum": 1},
                "agent_id": {"type": "string", "minLength": 1},
                "agent_type": {"const": "loopsail:worker"},
                "hook_event": {"enum": ["SubagentStart", "PreToolUse", "PostToolUse", "PostToolUseFailure", "SubagentStop", "log_truncated"]},
                "tool_name": nullable_string,
                "target_paths": {"type": "array", "uniqueItems": True, "items": {"type": "string", "minLength": 1}},
                "command_category": nullable_string,
                "command_sha256": nullable_string,
                "outcome": {"enum": ["started", "allowed", "denied", "succeeded", "failed", "captured", "corrected", "protocol_failed", "truncated"]},
            },
        ),
        "attempt-log.schema.json": generic_schema(
            "attempt-log",
            "LoopSail Attempt Log v2",
            {
                "request_id": {"type": "string", "minLength": 1},
                "list_id": {"type": "string", "minLength": 1},
                "task_id": {"type": "string", "minLength": 1},
                "attempt": {"type": "integer", "minimum": 1},
                "agent_id": nullable_string,
                "status": {"enum": ["done", "retry", "blocked"]},
                "failure_code": nullable_string,
                "failure": nullable_string,
                "actual_diff": {"type": "array", "uniqueItems": True, "items": {"type": "string"}},
                "diff_fingerprint": {"type": "string"},
                "worker_result_path": nullable_string,
                "event_log_path": nullable_string,
                "verification": {"type": "array", "items": {"type": "object"}},
                "commit": nullable_string,
                "experience_recorded": {"type": "boolean"},
                "at": {"type": "string", "format": "date-time"},
            },
        ),
        "run-state.schema.json": generic_schema(
            "run-state",
            "LoopSail Run State v2",
            {
                "list_id": {"type": "string", "minLength": 1},
                "project": {"type": "string", "minLength": 1},
                "task_file": {"type": "string", "minLength": 1},
                "branch": {"type": "string", "minLength": 1},
                "base_commit": {"type": "string", "minLength": 1},
                "project_status": {"enum": ["executing", "blocked", "complete"]},
                "active_task": nullable_string,
                "active_request": {"type": ["object", "null"]},
                "last_finalized_request": {"type": ["object", "null"]},
                "final_verification": {"type": ["object", "null"]},
                "final_verification_attempts": {"type": "integer", "minimum": 0},
                "tasks": {"type": "object"},
                "created_at": {"type": "string", "format": "date-time"},
                "updated_at": {"type": "string", "format": "date-time"},
            },
        ),
        "step-report.schema.json": generic_schema(
            "step-report",
            "LoopSail Step Report v2",
            {
                "action": {"enum": ["spawn_worker", "finalize_pending", "finalized", "blocked", "idle", "complete", "already_complete"]},
                "performed": {"type": "boolean"},
                "list_id": {"type": "string", "minLength": 1},
                "branch": {"type": "string", "minLength": 1},
                "project_status": {"enum": ["executing", "blocked", "complete"]},
                "request_id": nullable_string,
                "request_path": nullable_string,
                "worker_agent": nullable_string,
                "task": {"type": ["object", "null"]},
                "blocked_reason": nullable_string,
                "tasks_remaining": {"type": "integer", "minimum": 0},
                "next_action": {"type": ["string", "null"]},
                "at": {"type": "string", "format": "date-time"},
            },
        ),
        "status-report.schema.json": generic_schema(
            "status-report",
            "LoopSail Status Report v2",
            {
                "started": {"type": "boolean"},
                "list_id": {"type": "string", "minLength": 1},
                "project_status": {"enum": ["not_started", "executing", "blocked", "complete"]},
                "branch": {"type": "string", "minLength": 1},
                "active_task": nullable_string,
                "active_request": {"type": ["object", "null"]},
                "final_verification": {"type": ["object", "null"]},
                "tasks": {"type": "array", "items": {"type": "object"}},
                "at": {"type": "string", "format": "date-time"},
            },
        ),
        "command-envelope.schema.json": generic_schema(
            "command-envelope",
            "LoopSail Command Envelope v2",
            {
                "ok": {"type": "boolean"},
                "exit_code": {"enum": [0, 2, 3, 4]},
                "at": {"type": "string", "format": "date-time"},
                "data": {"type": ["object", "null"]},
                "error": {"type": ["object", "null"]},
            },
        ),
    }
    return documents


SCHEMA_BUILDERS: dict[str, Callable[[], dict[str, Any]]] = {
    name: (lambda name=name: schema_documents()[name]) for name in schema_documents()
}
