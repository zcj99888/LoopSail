#!/usr/bin/env python3
"""Structured task-list driven Claude Code development loop."""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import fnmatch
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path, PurePosixPath
from typing import Any, Iterator, Sequence


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from protocol import (  # noqa: E402
    ProtocolError,
    command_envelope,
    validate_worker_result as validate_worker_result_v2,
)

TOOL_ROOT = Path(__file__).resolve().parent.parent
SCHEMA_DIR = TOOL_ROOT / "references"
TEMPLATE_DIR = TOOL_ROOT / "templates"

INIT_TEMPLATE_FILES = (
    ("CLAUDE.md", "CLAUDE.md"),
    ("AGENTS.md", "AGENTS.md"),
    ("LOOP.md", "LOOP.md"),
    ("经验记录.md", "经验记录.md"),
    ("TASKS.template.json", "TASKS.template.json"),
)
INIT_TASK_FILE = "TASKS.json"
LESSONS_FILE = "经验记录.md"
INIT_GITIGNORE_HEADER = "# loopsail 本地输入与运行状态"
INIT_GITIGNORE_ENTRIES = (
    "/TASKS.json",
    ".loopsail/input/",
    ".loopsail/output/",
    ".loopsail/runs/",
    ".loopsail/logs/",
    ".loopsail/lock",
)
INIT_COMMIT_MESSAGE = "chore: initialize loopsail"

DEFAULT_CONFIG: dict[str, Any] = {
    "schema_version": 2,
    "kind": "loopsail-config",
    "protected_paths": [],
    "verification_output_limit_bytes": 65536,
    "event_log_max_bytes": 5 * 1024 * 1024,
}
TOP_CONFIG_KEYS = set(DEFAULT_CONFIG)
TASK_REQUIRED = {
    "id",
    "title",
    "description",
    "depends_on",
    "context_files",
    "acceptance",
    "verify_commands",
}
TASK_OPTIONAL = {"allowed_paths", "source_refs", "non_goals", "stop_conditions"}
LIST_REQUIRED = {
    "schema_version",
    "kind",
    "list_id",
    "project",
    "final_verify_commands",
    "tasks",
}
LIST_OPTIONAL = {"$schema"}
STATUS_VALUES = {"pending", "running", "done", "blocked", "superseded"}
STATE_FIELDS = {
    "schema_version",
    "kind",
    "list_id",
    "project",
    "task_file",
    "branch",
    "base_commit",
    "project_status",
    "active_task",
    "active_request",
    "last_finalized_request",
    "final_verification",
    "final_verification_attempts",
    "tasks",
    "created_at",
    "updated_at",
}
TASK_STATE_FIELDS = {
    "definition_hash",
    "status",
    "attempts",
    "attempt_sequence",
    "ai_retry_count",
    "commit",
    "last_failure",
    "updated_at",
}
LEASE_FIELDS = {
    "request_id",
    "list_id",
    "task_id",
    "attempt",
    "request_path",
    "output_path",
    "event_log_path",
    "task_input_hash",
    "task_definition_hash",
    "experience_hash",
    "head_commit",
    "index_hash",
    "agent_id",
    "session_id",
    "started_at",
    "captured_at",
    "correction_used",
    "last_protocol_error",
    "protocol_failure",
    "result_status",
    "event_sequence",
    "event_truncated",
    "event_log_max_bytes",
    "fatal_error",
    "created_at",
}
ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9._-]{0,63}$")
SECRET_VALUE_RE = re.compile(
    r"(?i)(?:api[_-]?key|(?:access[_-]?)?token|password|secret|credential)\s*[=:]|"
    r"\bsk-(?:ant-)?[A-Za-z0-9_-]{12,}"
)
PROTECTED_PATTERNS = (
    "LOOP.md",
    "TASKS.template.json",
    ".claude/skills/loopsail/**",
    ".loopsail/**",
    ".git/**",
)
MAX_ATTEMPTS = 3
AI_RETRY_LIMIT = 1
STEP_REPORT_FILE = "last-step.json"
EXIT_PROGRESSED = 3
EXIT_IDLE = 4
MAX_LESSON_FIELD_LENGTH = 2000
CREDENTIAL_ASSIGNMENT_RE = re.compile(
    r"(?i)\b(api[_-]?key|(?:access[_-]?)?token|password|secret|credential)\b"
    r"\s*([=:])\s*(?:\"[^\"]*\"|'[^']*'|[^\s,;]+)"
)
SECRET_TOKEN_RE = re.compile(r"\bsk-(?:ant-)?[A-Za-z0-9_-]{12,}\b", re.I)
ABSOLUTE_PATH_RE = re.compile(
    r"(?<![\w\]:/])(?:/(?!/)[^\s`'\"<>]+|[A-Za-z]:[\\/][^\s`'\"<>]+)"
)


class LoopSailError(RuntimeError):
    """Expected user-facing failure."""

    def __init__(self, message: str, *, code: str = "loopsail_error") -> None:
        super().__init__(message)
        self.code = code


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def value_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise LoopSailError(f"file does not exist: {path}") from exc
    except json.JSONDecodeError as exc:
        raise LoopSailError(f"invalid JSON in {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise LoopSailError(f"JSON root must be an object: {path}")
    return value


def atomic_write_json(path: Path, value: dict[str, Any]) -> None:
    if path.is_symlink() or any(parent.is_symlink() for parent in path.parents):
        raise LoopSailError(f"refusing to write through a symbolic link: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def atomic_write_bytes(path: Path, value: bytes, *, replace_existing: bool) -> None:
    """Atomically create or replace one file without following symlinks."""
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        if replace_existing:
            if path.is_symlink() or not path.is_file():
                raise LoopSailError(f"refusing to replace unsafe path: {path}")
            os.chmod(temporary, path.stat().st_mode & 0o7777)
            os.replace(temporary, path)
        else:
            mask = os.umask(0)
            os.umask(mask)
            os.chmod(temporary, 0o666 & ~mask)
            try:
                os.link(temporary, path)
            except FileExistsError as exc:
                raise LoopSailError(f"path appeared during initialization: {path}") from exc
            temporary.unlink()
    finally:
        if temporary.exists():
            temporary.unlink()


def run_process(
    argv: Sequence[str],
    *,
    cwd: Path,
    timeout: int | float | None = None,
    env: dict[str, str] | None = None,
    check: bool = False,
) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            list(argv),
            cwd=cwd,
            env=env,
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as exc:
        raise LoopSailError(f"command not found: {argv[0]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise LoopSailError(f"command timed out after {timeout}s: {argv[0]}") from exc
    if check and result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise LoopSailError(f"command failed ({result.returncode}): {' '.join(argv)}\n{detail}")
    return result


def run_git(root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return run_process(["git", *args], cwd=root, check=check)


def maybe_discover_project_root(start: Path | None = None) -> Path | None:
    result = run_process(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=(start or Path.cwd()).resolve(),
    )
    if result.returncode != 0 or not result.stdout.strip():
        return None
    return Path(result.stdout.strip()).resolve()


def discover_project_root(start: Path | None = None) -> Path:
    root = maybe_discover_project_root(start)
    if root is None:
        raise LoopSailError("loopsail must run inside a Git repository")
    return root


def require_safe_init_file(path: Path) -> None:
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise LoopSailError(f"initialization target is not a regular file: {path}")


def require_safe_init_directory(path: Path) -> None:
    if path.is_symlink() or (path.exists() and not path.is_dir()):
        raise LoopSailError(f"initialization target is not a directory: {path}")


def load_init_templates(root: Path) -> dict[str, bytes]:
    rendered: dict[str, bytes] = {}
    project_name = root.name or "TODO-project"
    for source_name, target_name in INIT_TEMPLATE_FILES:
        source = TEMPLATE_DIR / source_name
        if source.is_symlink() or not source.is_file():
            raise LoopSailError(f"loopsail initialization template is missing or unsafe: {source}")
        try:
            template = source.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise LoopSailError(f"cannot read loopsail initialization template: {source}: {exc}") from exc
        if source_name == "TASKS.template.json":
            try:
                value = json.loads(template)
            except json.JSONDecodeError as exc:
                raise LoopSailError(f"invalid bundled initialization template: {source}: {exc}") from exc
            value["project"] = project_name
            content = json.dumps(value, ensure_ascii=False, indent=2) + "\n"
        else:
            content = template.replace("{{PROJECT_NAME}}", project_name)
        rendered[target_name] = content.encode("utf-8")
    return rendered


def merged_gitignore(existing: bytes | None) -> tuple[bytes, bool]:
    if existing is None:
        content = ""
    else:
        try:
            content = existing.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise LoopSailError(".gitignore must be UTF-8 to merge loopsail entries") from exc
    lines = content.splitlines()
    missing = [entry for entry in INIT_GITIGNORE_ENTRIES if entry not in lines]
    if not missing:
        return existing or b"", False

    newline = "\r\n" if "\r\n" in content else "\n"
    addition = ""
    if content:
        if not content.endswith(("\n", "\r")):
            addition += newline
        if INIT_GITIGNORE_HEADER not in lines and not (content + addition).endswith(
            newline + newline
        ):
            addition += newline
    if INIT_GITIGNORE_HEADER not in lines:
        addition += INIT_GITIGNORE_HEADER + newline
    addition += newline.join(missing) + newline
    return (content + addition).encode("utf-8"), True


def rollback_init_changes(
    created_files: list[Path],
    updated_files: list[tuple[Path, bytes]],
    created_directories: list[Path],
) -> list[str]:
    errors: list[str] = []
    for path in reversed(created_files):
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            errors.append(f"cannot remove {path}: {exc}")
    for path, original in reversed(updated_files):
        try:
            atomic_write_bytes(path, original, replace_existing=True)
        except (LoopSailError, OSError) as exc:
            errors.append(f"cannot restore {path}: {exc}")
    for path in reversed(created_directories):
        try:
            path.rmdir()
        except OSError as exc:
            errors.append(f"cannot remove directory {path}: {exc}")
    return errors


def initialize_scaffold(root: Path) -> dict[str, list[str]]:
    """Create the target-project scaffold while preserving existing files."""
    templates = load_init_templates(root)
    targets = {target: root / target for _, target in INIT_TEMPLATE_FILES}
    task_file = root / INIT_TASK_FILE
    gitignore = root / ".gitignore"
    loopsail_directory = root / ".loopsail"

    required_directories: list[Path] = []
    for path in targets.values():
        parents: list[Path] = []
        parent = path.parent
        while parent != root:
            parents.append(parent)
            parent = parent.parent
        for directory in reversed(parents):
            if directory not in required_directories:
                required_directories.append(directory)

    for path in targets.values():
        require_safe_init_file(path)
    for path in required_directories:
        require_safe_init_directory(path)
    require_safe_init_file(task_file)
    require_safe_init_file(gitignore)
    require_safe_init_directory(loopsail_directory)

    original_gitignore = gitignore.read_bytes() if gitignore.exists() else None
    gitignore_content, gitignore_changes = merged_gitignore(original_gitignore)

    created: list[str] = []
    updated: list[str] = []
    skipped: list[str] = []
    touched: list[str] = []
    created_files: list[Path] = []
    updated_files: list[tuple[Path, bytes]] = []
    created_directories: list[Path] = []

    try:
        if not loopsail_directory.exists():
            loopsail_directory.mkdir()
            created_directories.append(loopsail_directory)

        for directory in required_directories:
            if not directory.exists():
                directory.mkdir()
                created_directories.append(directory)

        for _, target_name in INIT_TEMPLATE_FILES:
            path = targets[target_name]
            if path.exists():
                skipped.append(target_name)
                continue
            atomic_write_bytes(path, templates[target_name], replace_existing=False)
            created_files.append(path)
            created.append(target_name)
            touched.append(target_name)

        if task_file.exists():
            skipped.append(INIT_TASK_FILE)
        else:
            task_template = targets["TASKS.template.json"].read_bytes()
            atomic_write_bytes(
                task_file,
                task_template,
                replace_existing=False,
            )
            created_files.append(task_file)
            created.append(INIT_TASK_FILE)

        if original_gitignore is None:
            atomic_write_bytes(gitignore, gitignore_content, replace_existing=False)
            created_files.append(gitignore)
            created.append(".gitignore")
            touched.append(".gitignore")
        elif gitignore_changes:
            atomic_write_bytes(gitignore, gitignore_content, replace_existing=True)
            updated_files.append((gitignore, original_gitignore))
            updated.append(".gitignore")
            touched.append(".gitignore")
        else:
            skipped.append(".gitignore")
    except (LoopSailError, OSError) as exc:
        rollback_errors = rollback_init_changes(
            created_files, updated_files, created_directories
        )
        detail = f"; rollback errors: {'; '.join(rollback_errors)}" if rollback_errors else ""
        raise LoopSailError(f"initialization failed and was rolled back: {exc}{detail}") from exc

    return {
        "created": created,
        "updated": updated,
        "skipped": skipped,
        "touched": touched,
    }


def safe_relative_path(value: str, *, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise LoopSailError(f"{field} must contain non-empty strings")
    normalized = value.replace("\\", "/")
    path = PurePosixPath(normalized)
    if path.is_absolute() or ".." in path.parts or "\x00" in normalized:
        raise LoopSailError(f"{field} must stay within the project: {value!r}")
    return normalized.removeprefix("./")


def ensure_string_list(value: Any, *, field: str, nonempty: bool = False) -> list[str]:
    if not isinstance(value, list) or (nonempty and not value):
        suffix = " and must not be empty" if nonempty else ""
        raise LoopSailError(f"{field} must be an array{suffix}")
    if any(not isinstance(item, str) or not item.strip() for item in value):
        raise LoopSailError(f"{field} must contain non-empty strings")
    if len(set(value)) != len(value):
        raise LoopSailError(f"{field} must not contain duplicates")
    return list(value)


def reject_secret_values(value: Any, *, location: str = "config") -> None:
    if isinstance(value, dict):
        for key, nested in value.items():
            if re.search(r"(?i)(secret|token|password|credential|api[_-]?key)", str(key)):
                raise LoopSailError(f"{location} must not contain secret-bearing key {key!r}")
            reject_secret_values(nested, location=f"{location}.{key}")
    elif isinstance(value, list):
        for index, nested in enumerate(value):
            reject_secret_values(nested, location=f"{location}[{index}]")
    elif isinstance(value, str) and SECRET_VALUE_RE.search(value):
        raise LoopSailError(f"{location} appears to contain an inline secret")


def validate_config(value: dict[str, Any], *, source: Path | None = None) -> None:
    label = str(source) if source else "configuration"
    unknown = set(value) - TOP_CONFIG_KEYS
    if unknown:
        raise LoopSailError(f"unknown keys in {label}: {', '.join(sorted(unknown))}")
    reject_secret_values(value)
    version = value.get("schema_version")
    if version != 2:
        raise LoopSailError(
            f"unsupported schema_version {version!r} in {label}; expected 2",
            code="unsupported_schema_version",
        )
    if value.get("kind") != "loopsail-config":
        raise LoopSailError(f"kind must be 'loopsail-config' in {label}")
    for field in ("verification_output_limit_bytes", "event_log_max_bytes"):
        if field in value and (
            not isinstance(value[field], int)
            or isinstance(value[field], bool)
            or value[field] <= 0
        ):
            raise LoopSailError(f"{field} must be a positive integer in {label}")
    if "protected_paths" in value:
        for item in ensure_string_list(value["protected_paths"], field="protected_paths"):
            safe_relative_path(item, field="protected_paths")


def merge_config(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    result = json.loads(json.dumps(base))
    for key, value in overlay.items():
        result[key] = value
    return result


def load_config(root: Path, explicit: Path | None = None) -> tuple[dict[str, Any], list[str]]:
    config = json.loads(json.dumps(DEFAULT_CONFIG))
    sources: list[str] = ["built-in defaults"]
    candidates = [Path.home() / ".loopsail" / "config.json", root / ".loopsail" / "config.json"]
    if explicit is not None:
        candidates.append(explicit.expanduser().resolve())
    for path in candidates:
        if not path.is_file():
            if explicit is not None and path == explicit.expanduser().resolve():
                raise LoopSailError(f"runner configuration does not exist: {path}")
            continue
        overlay = load_json(path)
        validate_config(overlay, source=path)
        config = merge_config(config, overlay)
        sources.append(str(path))
    validate_config(config)
    return config, sources


def validate_command(value: Any, *, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise LoopSailError(f"{field} entries must be objects")
    unknown = set(value) - {"argv", "cwd", "timeout_seconds"}
    if unknown:
        raise LoopSailError(f"unknown keys in {field}: {', '.join(sorted(unknown))}")
    argv = ensure_string_list(value.get("argv"), field=f"{field}.argv", nonempty=True)
    if any("\n" in part or "\r" in part or "\x00" in part for part in argv):
        raise LoopSailError(f"{field}.argv must not contain control characters")
    executable = Path(argv[0]).name.lower()
    forbidden = {
        "rm",
        "shred",
        "truncate",
        "shutdown",
        "reboot",
        "mkfs",
        "dd",
    }
    if executable in forbidden:
        raise LoopSailError(f"{field} uses forbidden verification command: {argv[0]}")
    if executable == "git" and len(argv) > 1 and argv[1] in {
        "push",
        "merge",
        "rebase",
        "reset",
        "clean",
        "checkout",
        "switch",
        "commit",
        "tag",
    }:
        raise LoopSailError(f"{field} may not mutate Git state")
    cwd = safe_relative_path(value.get("cwd", "."), field=f"{field}.cwd") or "."
    timeout = value.get("timeout_seconds", 900)
    if not isinstance(timeout, int) or isinstance(timeout, bool) or timeout <= 0:
        raise LoopSailError(f"{field}.timeout_seconds must be a positive integer")
    return {"argv": argv, "cwd": cwd, "timeout_seconds": timeout}


def find_cycles(tasks: dict[str, dict[str, Any]]) -> list[list[str]]:
    cycles: list[list[str]] = []
    visiting: list[str] = []
    visited: set[str] = set()

    def visit(task_id: str) -> None:
        if task_id in visiting:
            start = visiting.index(task_id)
            cycles.append(visiting[start:] + [task_id])
            return
        if task_id in visited:
            return
        visiting.append(task_id)
        for dependency in tasks[task_id]["depends_on"]:
            if dependency in tasks:
                visit(dependency)
        visiting.pop()
        visited.add(task_id)

    for task_id in tasks:
        visit(task_id)
    return cycles


def validate_task_list(value: dict[str, Any], root: Path) -> dict[str, Any]:
    if value.get("schema_version") != 2:
        raise LoopSailError(
            f"unsupported task-list schema_version {value.get('schema_version')!r}; expected 2",
            code="unsupported_schema_version",
        )
    unknown = set(value) - LIST_REQUIRED - LIST_OPTIONAL
    missing = LIST_REQUIRED - set(value)
    if missing or unknown:
        details = []
        if missing:
            details.append("missing " + ", ".join(sorted(missing)))
        if unknown:
            details.append("unknown " + ", ".join(sorted(unknown)))
        raise LoopSailError("invalid task-list fields: " + "; ".join(details))
    if value["kind"] != "task-list":
        raise LoopSailError("task-list kind must be 'task-list'")
    for field in ("list_id", "project"):
        if not isinstance(value[field], str) or not value[field].strip():
            raise LoopSailError(f"{field} must be a non-empty string")
    if not ID_RE.fullmatch(value["list_id"]):
        raise LoopSailError("list_id must match [A-Za-z][A-Za-z0-9._-]{0,63}")
    if not isinstance(value["tasks"], list) or not value["tasks"]:
        raise LoopSailError("tasks must be a non-empty array")
    if not isinstance(value["final_verify_commands"], list):
        raise LoopSailError("final_verify_commands must be an array")
    normalized: dict[str, Any] = {
        "schema_version": 2,
        "kind": "task-list",
        "list_id": value["list_id"],
        "project": value["project"],
        "final_verify_commands": [
            validate_command(item, field="final_verify_commands")
            for item in value["final_verify_commands"]
        ],
        "tasks": [],
    }
    if not normalized["final_verify_commands"]:
        raise LoopSailError("final_verify_commands must not be empty")
    task_map: dict[str, dict[str, Any]] = {}
    for index, raw in enumerate(value["tasks"]):
        field = f"tasks[{index}]"
        if not isinstance(raw, dict):
            raise LoopSailError(f"{field} must be an object")
        missing_task = TASK_REQUIRED - set(raw)
        unknown_task = set(raw) - TASK_REQUIRED - TASK_OPTIONAL
        if missing_task or unknown_task:
            raise LoopSailError(
                f"{field} has invalid fields; missing={sorted(missing_task)}, "
                f"unknown={sorted(unknown_task)}"
            )
        task_id = raw["id"]
        if not isinstance(task_id, str) or not ID_RE.fullmatch(task_id):
            raise LoopSailError(f"{field}.id is invalid")
        if task_id in task_map:
            raise LoopSailError(f"duplicate task id: {task_id}")
        for text_field in ("title", "description"):
            if not isinstance(raw[text_field], str) or not raw[text_field].strip():
                raise LoopSailError(f"{field}.{text_field} must be a non-empty string")
        task: dict[str, Any] = {
            "id": task_id,
            "title": raw["title"],
            "description": raw["description"],
            "depends_on": ensure_string_list(raw["depends_on"], field=f"{field}.depends_on"),
            "context_files": [
                safe_relative_path(item, field=f"{field}.context_files")
                for item in ensure_string_list(
                    raw["context_files"], field=f"{field}.context_files", nonempty=True
                )
            ],
            "acceptance": ensure_string_list(
                raw["acceptance"], field=f"{field}.acceptance", nonempty=True
            ),
            "verify_commands": [
                validate_command(item, field=f"{field}.verify_commands")
                for item in raw["verify_commands"]
            ],
        }
        if not task["verify_commands"]:
            raise LoopSailError(f"{field}.verify_commands must not be empty")
        for optional in ("source_refs", "non_goals", "stop_conditions"):
            if optional in raw:
                task[optional] = ensure_string_list(raw[optional], field=f"{field}.{optional}")
        if "allowed_paths" in raw:
            task["allowed_paths"] = [
                safe_relative_path(item, field=f"{field}.allowed_paths")
                for item in ensure_string_list(
                    raw["allowed_paths"], field=f"{field}.allowed_paths", nonempty=True
                )
            ]
        task_map[task_id] = task
        normalized["tasks"].append(task)
    for task in normalized["tasks"]:
        for dependency in task["depends_on"]:
            if dependency not in task_map:
                raise LoopSailError(f"{task['id']}: unknown dependency {dependency}")
            if dependency == task["id"]:
                raise LoopSailError(f"{task['id']}: task cannot depend on itself")
        for pattern in task["context_files"]:
            if not any(root.glob(pattern)):
                raise LoopSailError(f"{task['id']}: context_files pattern matches nothing: {pattern}")
    cycles = find_cycles(task_map)
    if cycles:
        raise LoopSailError("task dependency cycle: " + " -> ".join(cycles[0]))
    return normalized


def task_map(task_list: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {task["id"]: task for task in task_list["tasks"]}


def state_paths(root: Path, list_id: str) -> tuple[Path, Path, Path]:
    run_dir = root / ".loopsail" / "runs" / list_id
    return run_dir / "state.json", run_dir / "task-list.snapshot.json", root / ".loopsail" / "logs"


def new_task_state(task: dict[str, Any]) -> dict[str, Any]:
    return {
        "definition_hash": value_hash(task),
        "status": "pending",
        "attempts": 0,
        "attempt_sequence": 0,
        "ai_retry_count": 0,
        "commit": None,
        "last_failure": None,
        "updated_at": utc_now(),
    }


def create_state(task_list: dict[str, Any], task_file: Path, root: Path) -> dict[str, Any]:
    branch = f"loopsail/{task_list['list_id'].lower()}"
    return {
        "schema_version": 2,
        "kind": "run-state",
        "list_id": task_list["list_id"],
        "project": task_list["project"],
        "task_file": task_file.resolve().relative_to(root).as_posix(),
        "branch": branch,
        "base_commit": run_git(root, "rev-parse", "HEAD").stdout.strip(),
        "project_status": "executing",
        "active_task": None,
        "active_request": None,
        "last_finalized_request": None,
        "final_verification": None,
        "final_verification_attempts": 0,
        "tasks": {task["id"]: new_task_state(task) for task in task_list["tasks"]},
        "created_at": utc_now(),
        "updated_at": utc_now(),
    }


def validate_state(state: dict[str, Any], task_list: dict[str, Any]) -> None:
    if state.get("schema_version") != 2:
        raise LoopSailError(
            f"unsupported runtime schema_version {state.get('schema_version')!r}; expected 2",
            code="unsupported_schema_version",
        )
    if state.get("kind") != "run-state" or state.get("list_id") != task_list["list_id"]:
        raise LoopSailError("runtime state does not match the task list")
    if set(state) != STATE_FIELDS:
        raise LoopSailError(
            "runtime state has missing or unexpected fields",
            code="invalid_run_state",
        )
    if state.get("project_status") not in {"executing", "blocked", "complete"}:
        raise LoopSailError("runtime project_status is invalid")
    if not isinstance(state.get("tasks"), dict):
        raise LoopSailError("runtime tasks must be an object")
    if state.get("active_request") is not None and not isinstance(
        state.get("active_request"), dict
    ):
        raise LoopSailError("runtime active_request must be an object or null")
    for task_id, item in state["tasks"].items():
        if (
            not isinstance(item, dict)
            or not TASK_STATE_FIELDS.issubset(item)
            or set(item) - TASK_STATE_FIELDS - {"verification"}
            or item.get("status") not in STATUS_VALUES
        ):
            raise LoopSailError(f"runtime state is invalid for {task_id}")
    lease = state.get("active_request")
    if isinstance(lease, dict):
        if set(lease) != LEASE_FIELDS:
            raise LoopSailError(
                "active request lease has missing or unexpected fields",
                code="invalid_attempt_lease",
            )
        if (
            lease.get("list_id") != state["list_id"]
            or lease.get("task_id") != state.get("active_task")
            or state["tasks"].get(lease.get("task_id"), {}).get("status") != "running"
        ):
            raise LoopSailError(
                "active request lease binding is invalid",
                code="invalid_attempt_lease",
            )


def is_clean(root: Path) -> bool:
    return not run_git(root, "status", "--porcelain", "--untracked-files=all").stdout.strip()


def current_branch(root: Path) -> str:
    return run_git(root, "branch", "--show-current").stdout.strip()


def branch_exists(root: Path, branch: str) -> bool:
    return run_git(root, "show-ref", "--verify", "--quiet", f"refs/heads/{branch}", check=False).returncode == 0


def ensure_run_branch(root: Path, state: dict[str, Any], *, first_run: bool) -> None:
    branch = state["branch"]
    current = current_branch(root)
    if current == branch:
        return
    if not is_clean(root):
        raise LoopSailError(f"working tree must be clean before switching to {branch}")
    if branch_exists(root, branch):
        run_git(root, "switch", branch)
    elif first_run:
        run_git(root, "switch", "-c", branch)
    else:
        raise LoopSailError(f"recorded run branch is missing: {branch}")


def changed_paths(root: Path) -> list[str]:
    output = run_git(root, "status", "--porcelain=v1", "-z", "--untracked-files=all").stdout
    entries = output.split("\x00")
    paths: list[str] = []
    index = 0
    while index < len(entries):
        entry = entries[index]
        if not entry:
            index += 1
            continue
        status = entry[:2]
        path = entry[3:].replace("\\", "/")
        paths.append(path)
        if "R" in status or "C" in status:
            index += 1
            if index < len(entries) and entries[index]:
                paths.append(entries[index].replace("\\", "/"))
        index += 1
    return sorted(set(paths))


def task_owned_paths(paths: list[str]) -> list[str]:
    return [path for path in paths if path != LESSONS_FILE]


def path_matches(path: str, pattern: str) -> bool:
    if fnmatch.fnmatchcase(path, pattern):
        return True
    if pattern.endswith("/**") and path == pattern[:-3].rstrip("/"):
        return True
    return False


def scope_errors(
    paths: list[str],
    task: dict[str, Any],
    config: dict[str, Any],
    task_file: Path,
    root: Path,
) -> list[str]:
    protected = list(PROTECTED_PATTERNS) + list(config["protected_paths"])
    try:
        relative_task_file = task_file.resolve().relative_to(root).as_posix()
    except ValueError:
        relative_task_file = None
    if relative_task_file:
        protected.append(relative_task_file)
    errors: list[str] = []
    for path in paths:
        if path == LESSONS_FILE:
            continue
        if any(path_matches(path, pattern) for pattern in protected):
            errors.append(f"protected path changed: {path}")
        allowed = task.get("allowed_paths")
        if allowed and not any(path_matches(path, pattern) for pattern in allowed):
            errors.append(f"path is outside allowed_paths: {path}")
    return errors


def diff_fingerprint(root: Path, paths: list[str]) -> str:
    digest = hashlib.sha256()
    for relative in paths:
        if relative == LESSONS_FILE:
            continue
        digest.update(relative.encode("utf-8"))
        path = root / relative
        if path.is_symlink():
            digest.update(b"symlink:")
            digest.update(os.readlink(path).encode("utf-8", errors="replace"))
        elif path.is_file():
            with path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
        else:
            digest.update(b"deleted-or-non-file")
    return digest.hexdigest()


def git_index_hash(root: Path) -> str:
    result = run_git(root, "diff", "--cached", "--binary", "--no-ext-diff")
    return hashlib.sha256(result.stdout.encode("utf-8", errors="replace")).hexdigest()


def normalize_failure(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip().lower())[:1000]


def bounded(value: str, limit: int) -> str:
    encoded = value.encode("utf-8", errors="replace")
    if len(encoded) <= limit:
        return value
    return encoded[-limit:].decode("utf-8", errors="replace")


def require_safe_experience_file(root: Path) -> tuple[Path, str]:
    path = root / LESSONS_FILE
    if path.is_symlink() or not path.is_file():
        raise LoopSailError(
            f"experience log is missing or unsafe: {LESSONS_FILE}; "
            "use /loopsail:init and commit the scaffold"
        )
    try:
        content = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise LoopSailError(f"experience log must be a readable UTF-8 file: {LESSONS_FILE}") from exc
    return path, content


def experience_file_hash(root: Path) -> str:
    path, _ = require_safe_experience_file(root)
    return file_hash(path)


def experience_file_changed(root: Path, expected_hash: str) -> bool:
    try:
        return experience_file_hash(root) != expected_hash
    except (LoopSailError, OSError):
        return True


def sanitize_experience_text(value: str, root: Path) -> str:
    text = re.sub(r"<!--[\s\S]*?-->", " ", value)
    replacements = {
        str(root.resolve()): "[project-root]",
        TOOL_ROOT.resolve().as_posix(): "[loopsail-installation]",
        Path.home().resolve().as_posix(): "[home]",
    }
    for private_path, replacement in sorted(
        replacements.items(), key=lambda item: len(item[0]), reverse=True
    ):
        if private_path and private_path != "/":
            text = text.replace(private_path, replacement)
    text = CREDENTIAL_ASSIGNMENT_RE.sub(
        lambda match: f"{match.group(1)}{match.group(2)}[REDACTED]", text
    )
    text = SECRET_TOKEN_RE.sub("[REDACTED]", text)
    text = ABSOLUTE_PATH_RE.sub("[absolute-path]", text)
    text = re.sub(r"[\x00-\x1f\x7f]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    text = text[:MAX_LESSON_FIELD_LENGTH]
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def experience_log_reference(list_id: str, task_id: str, attempt: int) -> str:
    return f".loopsail/logs/{list_id}/{task_id}-attempt-{attempt}.json"


def summarize_experience_failure(value: str, stage: str, root: Path) -> str:
    if stage in {"任务验证", "中断恢复验证", "最终验证"}:
        return f"{stage}未通过；详细命令输出见对应日志。"
    if stage in {"Worker 执行", "Agent 结果捕获", "Agent 输出协议"}:
        return "子 Agent 未成功返回有效的协议 v2 结果；详细信息见对应日志。"
    if stage == "任务提交":
        return "任务提交失败；详细输出见对应日志。"
    return sanitize_experience_text(value, root)


def append_experience_record(
    root: Path,
    task_list: dict[str, Any],
    *,
    task_id: str,
    title: str,
    attempt: int | None,
    outcome: str,
    stage: str,
    lessons: list[dict[str, Any]],
    failure: str | None = None,
    log_reference: str | None = None,
) -> bool:
    if not lessons and failure is None:
        return False
    path, existing = require_safe_experience_file(root)
    list_id = sanitize_experience_text(str(task_list["list_id"]), root)
    safe_task_id = sanitize_experience_text(task_id, root)
    attempt_label = f"第 {attempt} 次尝试" if attempt is not None else "整轮验证"
    lines = [
        "<!-- loopsail-experience-record -->",
        f"## {utc_now()} | `{list_id}` / `{safe_task_id}` | {attempt_label} | "
        f"{sanitize_experience_text(outcome, root)}",
        "",
        f"- 任务：{sanitize_experience_text(title, root)}",
        f"- 阶段：{sanitize_experience_text(stage, root)}",
    ]
    if log_reference:
        lines.append(f"- 日志：`{sanitize_experience_text(log_reference, root)}`")
    if failure:
        lines.append(f"- 失败摘要：{summarize_experience_failure(failure, stage, root)}")
    if lessons:
        for index, lesson in enumerate(lessons, start=1):
            lines.extend(
                [
                    "",
                    f"### 经验 {index}",
                    "",
                    f"- 困难：{sanitize_experience_text(lesson['challenge'], root)}",
                    "- 绕路："
                    + (
                        sanitize_experience_text(lesson["detour"], root)
                        if lesson["detour"] is not None
                        else "未发生或未报告"
                    ),
                    "- 根因："
                    + (
                        sanitize_experience_text(lesson["root_cause"], root)
                        if lesson["root_cause"] is not None
                        else "尚未确认"
                    ),
                    "- 解决方式："
                    + (
                        sanitize_experience_text(lesson["resolution"], root)
                        if lesson["resolution"] is not None
                        else "尚未解决"
                    ),
                    f"- 可复用经验：{sanitize_experience_text(lesson['takeaway'], root)}",
                ]
            )
    else:
        lines.extend(
            [
                "",
                "### 自动失败记录",
                "",
                "- 子 Agent 未返回可用的结构化复盘；Coordinator 仅记录已知结果，不推测根因。",
            ]
        )
    addition = "\n".join(lines) + "\n"
    separator = "\n" if existing.endswith("\n") else "\n\n"
    try:
        atomic_write_bytes(
            path,
            (existing + separator + addition).encode("utf-8"),
            replace_existing=True,
        )
    except (LoopSailError, OSError) as exc:
        raise LoopSailError(f"cannot safely update experience log: {LESSONS_FILE}") from exc
    return True


def execute_commands(
    root: Path, commands: list[dict[str, Any]], config: dict[str, Any]
) -> tuple[bool, list[dict[str, Any]], str | None]:
    records: list[dict[str, Any]] = []
    output_limit = int(config["verification_output_limit_bytes"])
    for command in commands:
        cwd = (root / command["cwd"]).resolve()
        try:
            cwd.relative_to(root)
        except ValueError as exc:
            raise LoopSailError(f"verification cwd escapes the project: {command['cwd']}") from exc
        if not cwd.is_dir():
            failure = f"verification cwd does not exist: {command['cwd']}"
            records.append({"command": command, "exit_code": None, "failure": failure})
            return False, records, failure
        started = time.monotonic()
        try:
            result = run_process(
                command["argv"], cwd=cwd, timeout=command["timeout_seconds"]
            )
            duration = round(time.monotonic() - started, 3)
            record = {
                "command": command,
                "exit_code": result.returncode,
                "duration_seconds": duration,
                "stdout_tail": bounded(result.stdout, output_limit),
                "stderr_tail": bounded(result.stderr, output_limit),
            }
        except LoopSailError as exc:
            duration = round(time.monotonic() - started, 3)
            record = {
                "command": command,
                "exit_code": None,
                "duration_seconds": duration,
                "failure": str(exc),
            }
        records.append(record)
        if record.get("exit_code") != 0:
            detail = record.get("failure") or record.get("stderr_tail") or record.get("stdout_tail")
            return False, records, f"verification failed: {' '.join(command['argv'])}: {detail}"
    return True, records, None


def record_failure(
    state: dict[str, Any],
    task_id: str,
    summary: str,
    fingerprint: str,
    *,
    immediate: bool = False,
) -> bool:
    item = state["tasks"][task_id]
    normalized = normalize_failure(summary)
    previous = item.get("last_failure")
    repeated = bool(
        previous
        and previous.get("summary") == normalized
        and previous.get("fingerprint") == fingerprint
    )
    item["last_failure"] = {
        "summary": normalized,
        "fingerprint": fingerprint,
        "recorded_at": utc_now(),
    }
    blocked = immediate or item["attempts"] >= MAX_ATTEMPTS or repeated
    item["status"] = "blocked" if blocked else "pending"
    item["updated_at"] = utc_now()
    state["active_task"] = task_id if blocked else None
    state["project_status"] = "blocked" if blocked else "executing"
    state["updated_at"] = utc_now()
    return blocked


def commit_task(root: Path, task_list: dict[str, Any], task: dict[str, Any]) -> str:
    paths = changed_paths(root)
    if paths:
        run_git(root, "add", "-A", "--", *paths)
    subject = f"loopsail({task['id']}): {task['title']}"[:72]
    body = (
        f"LoopSail-List: {task_list['list_id']}\n"
        f"LoopSail-Task: {task['id']}\n"
        f"LoopSail-Task-Hash: {value_hash(task)}"
    )
    args = ["commit"]
    if not paths:
        args.append("--allow-empty")
    args.extend(["-m", subject, "-m", body])
    run_git(root, *args)
    return run_git(root, "rev-parse", "HEAD").stdout.strip()


def recover_done_from_git(root: Path, task_list: dict[str, Any], state: dict[str, Any]) -> None:
    result = run_git(root, "log", "--format=%H%x1f%B%x1e", check=False)
    if result.returncode != 0:
        return
    tasks = task_map(task_list)
    for record in result.stdout.split("\x1e"):
        if "\x1f" not in record:
            continue
        commit, message = record.strip().split("\x1f", 1)
        trailers = dict(
            match.groups()
            for match in re.finditer(r"^(LoopSail-[A-Za-z-]+):\s*(\S+)\s*$", message, re.MULTILINE)
        )
        task_id = trailers.get("LoopSail-Task")
        if trailers.get("LoopSail-List") != task_list["list_id"] or task_id not in tasks:
            continue
        if trailers.get("LoopSail-Task-Hash") != value_hash(tasks[task_id]):
            continue
        item = state["tasks"][task_id]
        item.update({"status": "done", "commit": commit.strip(), "updated_at": utc_now()})


def reconcile_task_list(
    root: Path,
    task_list: dict[str, Any],
    state: dict[str, Any],
    previous: dict[str, Any],
) -> None:
    validate_state(state, task_list)
    if state.get("active_request") is not None and previous != task_list:
        raise LoopSailError(
            "TASKS.json changed while an attempt lease is active; finalize the attempt",
            code="task_input_changed",
        )
    old_tasks = task_map(previous)
    new_tasks = task_map(task_list)
    blocked_task = state.get("active_task") if state["project_status"] == "blocked" else None
    blocked_task_resolved = False
    changed = False
    task_changed = False
    if previous.get("project") != task_list["project"]:
        raise LoopSailError("project cannot change within an existing list_id")
    final_commands_changed = previous.get("final_verify_commands") != task_list["final_verify_commands"]
    for task_id, task in new_tasks.items():
        definition_hash = value_hash(task)
        if task_id not in state["tasks"]:
            state["tasks"][task_id] = new_task_state(task)
            changed = True
            task_changed = True
            continue
        item = state["tasks"][task_id]
        if item["definition_hash"] == definition_hash:
            continue
        if item["status"] == "done":
            raise LoopSailError(f"completed task definition cannot change: {task_id}")
        if task_owned_paths(changed_paths(root)):
            raise LoopSailError(
                f"cannot change unfinished task {task_id} while task-owned changes exist"
            )
        attempt_sequence = max(
            int(item.get("attempt_sequence", 0)), int(item.get("attempts", 0))
        )
        ai_retry_count = int(item.get("ai_retry_count", 0))
        item.update(new_task_state(task))
        item["attempt_sequence"] = attempt_sequence
        item["ai_retry_count"] = ai_retry_count
        changed = True
        task_changed = True
        if task_id == blocked_task:
            blocked_task_resolved = True
    for task_id in old_tasks:
        if task_id in new_tasks:
            continue
        item = state["tasks"].get(task_id)
        if item and item["status"] == "done":
            raise LoopSailError(f"completed task cannot be removed: {task_id}")
        if item:
            item["status"] = "superseded"
            item["updated_at"] = utc_now()
            changed = True
            task_changed = True
            if task_id == blocked_task:
                blocked_task_resolved = True
    if final_commands_changed:
        changed = True
        if (
            state["project_status"] == "blocked"
            and state.get("active_task") is None
            and not task_changed
        ):
            raise LoopSailError(
                "failed final verification requires a new repair task, not only new commands"
            )
    if state["project_status"] == "complete" and changed:
        raise LoopSailError("a completed task list is frozen; use a new list_id")
    if changed:
        if blocked_task and not blocked_task_resolved:
            state["project_status"] = "blocked"
            state["active_task"] = blocked_task
        else:
            state["project_status"] = "executing"
            state["active_task"] = None
            state["final_verification"] = None
        state["updated_at"] = utc_now()


def ready_task(task_list: dict[str, Any], state: dict[str, Any]) -> dict[str, Any] | None:
    tasks = task_map(task_list)
    for task in task_list["tasks"]:
        item = state["tasks"][task["id"]]
        if item["status"] != "pending":
            continue
        if all(state["tasks"][dependency]["status"] == "done" for dependency in task["depends_on"]):
            return tasks[task["id"]]
    return None


def all_tasks_finished(task_list: dict[str, Any], state: dict[str, Any]) -> bool:
    return all(state["tasks"][task["id"]]["status"] in {"done", "superseded"} for task in task_list["tasks"])


@contextlib.contextmanager
def project_lock(root: Path) -> Iterator[None]:
    lock_path = root / ".loopsail" / "lock"
    if lock_path.parent.is_symlink() or lock_path.is_symlink():
        raise LoopSailError("loopsail lock path must not be a symbolic link")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a+", encoding="utf-8")
    acquired = False
    try:
        try:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
        except (ImportError, BlockingIOError) as exc:
            raise LoopSailError("another loopsail process is already running in this project") from exc
        handle.seek(0)
        handle.truncate()
        handle.write(str(os.getpid()))
        handle.flush()
        yield
    finally:
        if acquired:
            with contextlib.suppress(Exception):
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def load_task_input(path: Path, root: Path) -> tuple[Path, dict[str, Any]]:
    resolved = path.expanduser()
    if not resolved.is_absolute():
        resolved = (Path.cwd() / resolved).resolve()
    else:
        resolved = resolved.resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise LoopSailError("task list must be stored inside the project root") from exc
    return resolved, validate_task_list(load_json(resolved), root)


def initialize_or_load_state(
    root: Path, task_file: Path, task_list: dict[str, Any]
) -> tuple[dict[str, Any], Path, Path, Path, bool]:
    state_path, snapshot_path, logs_root = state_paths(root, task_list["list_id"])
    if state_path.is_file():
        state = load_json(state_path)
        previous = load_json(snapshot_path)
        first_run = False
    else:
        if not is_clean(root):
            raise LoopSailError("working tree must be clean before starting a new task list")
        state = create_state(task_list, task_file, root)
        previous = task_list
        first_run = True
    ensure_run_branch(root, state, first_run=first_run)
    if first_run:
        recover_done_from_git(root, task_list, state)
    else:
        reconcile_task_list(root, task_list, state, previous)
    atomic_write_json(state_path, state)
    atomic_write_json(snapshot_path, task_list)
    return state, state_path, snapshot_path, logs_root, first_run


def complete_task(
    root: Path,
    task_file: Path,
    task_list: dict[str, Any],
    task: dict[str, Any],
    state: dict[str, Any],
    state_path: Path,
    config: dict[str, Any],
    verification_records: list[dict[str, Any]],
) -> str:
    paths = changed_paths(root)
    violations = scope_errors(paths, task, config, task_file, root)
    if violations:
        raise LoopSailError("; ".join(violations))
    commit = commit_task(root, task_list, task)
    item = state["tasks"][task["id"]]
    item.update(
        {
            "status": "done",
            "commit": commit,
            "last_failure": None,
            "verification": verification_records,
            "updated_at": utc_now(),
        }
    )
    state["active_task"] = None
    state["active_request"] = None
    state["project_status"] = "executing"
    state["updated_at"] = utc_now()
    atomic_write_json(state_path, state)
    return commit


CommandResult = tuple[dict[str, Any], int, dict[str, Any] | None]


def result(
    data: dict[str, Any],
    exit_code: int = 0,
    *,
    error_code: str | None = None,
    error_message: str | None = None,
) -> CommandResult:
    error = None
    if error_code is not None:
        error = {
            "code": error_code,
            "message": error_message or error_code,
            "details": None,
        }
    return data, exit_code, error


def create_initial_commit(root: Path, paths: list[str]) -> str:
    for variable in ("GIT_AUTHOR_IDENT", "GIT_COMMITTER_IDENT"):
        identity = run_git(root, "var", variable, check=False)
        if identity.returncode != 0:
            raise LoopSailError(
                "initialization files were created, but Git user.name/user.email are "
                "not configured, so the initial commit could not be created"
            )
    try:
        run_git(root, "add", "--", *paths)
        run_git(root, "commit", "--only", "-m", INIT_COMMIT_MESSAGE, "--", *paths)
    except LoopSailError as exc:
        raise LoopSailError(
            f"initialization files were created but the initial commit failed: {exc}"
        ) from exc
    return run_git(root, "rev-parse", "--short", "HEAD").stdout.strip()


def command_init(args: argparse.Namespace) -> CommandResult:
    start = Path.cwd().resolve()
    root = maybe_discover_project_root(start)
    git_initialized = False
    if root is None:
        if not args.yes:
            raise LoopSailError(
                "current directory is not a Git repository; confirmation is required"
            )
        run_process(["git", "init"], cwd=start, check=True)
        git_initialized = True
        root = discover_project_root(start)

    had_head = run_git(root, "rev-parse", "--verify", "HEAD", check=False).returncode == 0
    scaffold = initialize_scaffold(root)
    commit: str | None = None
    commit_skipped = False
    if not had_head and scaffold["touched"]:
        should_commit = bool(args.yes)
        if should_commit:
            commit = create_initial_commit(root, scaffold["touched"])
        else:
            commit_skipped = True
    return result(
        {
            "schema_version": 2,
            "kind": "init-report",
            "project_root": str(root),
            "git_initialized": git_initialized,
            "created": scaffold["created"],
            "updated": scaffold["updated"],
            "preserved": scaffold["skipped"],
            "commit": commit,
            "commit_skipped": commit_skipped,
            "next_action": "complete CLAUDE.md and TASKS.json, then run /loopsail:validate",
            "at": utc_now(),
        }
    )


def command_validate(args: argparse.Namespace) -> CommandResult:
    root = discover_project_root()
    config, sources = load_config(root, args.runner_config)
    task_file, task_list = load_task_input(args.task_list, root)
    require_safe_experience_file(root)
    return result(
        {
            "schema_version": 2,
            "kind": "validation-report",
            "valid": True,
            "list_id": task_list["list_id"],
            "task_file": task_file.relative_to(root).as_posix(),
            "tasks": len(task_list["tasks"]),
            "config_sources": sources,
            "config": config,
            "worker_agent": "loopsail:worker",
            "at": utc_now(),
        }
    )


def command_doctor(args: argparse.Namespace) -> CommandResult:
    root = maybe_discover_project_root() or Path.cwd().resolve()
    config, sources = load_config(root, args.runner_config)
    required = [
        TOOL_ROOT / "scripts" / "loopsail.py",
        TOOL_ROOT / "scripts" / "hook.py",
        TOOL_ROOT / "scripts" / "guard.py",
        TOOL_ROOT.parent.parent / "agents" / "worker.md",
        TOOL_ROOT.parent.parent / "hooks" / "hooks.json",
    ]
    missing = [str(path) for path in required if not path.is_file()]
    if missing:
        raise LoopSailError("plugin runtime files are missing: " + ", ".join(missing))
    hooks = load_json(TOOL_ROOT.parent.parent / "hooks" / "hooks.json")
    schemas: list[str] = []
    for path in sorted(SCHEMA_DIR.glob("*.schema.json")):
        schema = load_json(path)
        if schema.get("$schema") != "http://json-schema.org/draft-07/schema#":
            raise LoopSailError(
                f"schema does not declare Draft-07: {path.name}",
                code="schema_dialect_mismatch",
            )
        schemas.append(path.name)
    return result(
        {
            "schema_version": 2,
            "kind": "doctor-report",
            "healthy": True,
            "python": sys.version.split()[0],
            "config_sources": sources,
            "config": config,
            "worker_agent": "loopsail:worker",
            "hooks": sorted(hooks.get("hooks", {})),
            "schemas": schemas,
            "at": utc_now(),
        }
    )


def status_report(
    root: Path,
    task_list: dict[str, Any],
    state: dict[str, Any] | None,
) -> dict[str, Any]:
    started = state is not None
    if state is None:
        branch = f"loopsail/{task_list['list_id'].lower()}"
        project_status = "not_started"
        active_task = None
        active_request = None
        final_verification = None
        rows = [
            {
                "id": task["id"],
                "title": task["title"],
                "status": "pending",
                "attempts": 0,
                "attempt_sequence": 0,
                "ai_retry_count": 0,
                "commit": None,
                "last_failure": None,
                "definition_changed": False,
            }
            for task in task_list["tasks"]
        ]
    else:
        branch = state["branch"]
        project_status = state["project_status"]
        active_task = state.get("active_task")
        lease = state.get("active_request")
        active_request = (
            {
                "request_id": lease["request_id"],
                "task_id": lease["task_id"],
                "attempt": lease["attempt"],
                "agent_id": lease.get("agent_id"),
                "result_captured": bool(lease.get("captured_at")),
            }
            if isinstance(lease, dict)
            else None
        )
        final_verification = state.get("final_verification")
        rows = []
        for task in task_list["tasks"]:
            item = state["tasks"].get(task["id"], new_task_state(task))
            rows.append(
                {
                    "id": task["id"],
                    "title": task["title"],
                    "status": item["status"],
                    "attempts": int(item.get("attempts", 0)),
                    "attempt_sequence": int(item.get("attempt_sequence", 0)),
                    "ai_retry_count": int(item.get("ai_retry_count", 0)),
                    "commit": item.get("commit"),
                    "last_failure": item.get("last_failure"),
                    "definition_changed": item["definition_hash"] != value_hash(task),
                }
            )
    return {
        "schema_version": 2,
        "kind": "status-report",
        "started": started,
        "list_id": task_list["list_id"],
        "project_status": project_status,
        "branch": branch,
        "active_task": active_task,
        "active_request": active_request,
        "final_verification": final_verification,
        "tasks": rows,
        "at": utc_now(),
    }


def command_status(args: argparse.Namespace) -> CommandResult:
    root = discover_project_root()
    load_config(root, args.runner_config)
    _, task_list = load_task_input(args.task_list, root)
    state_path, _, _ = state_paths(root, task_list["list_id"])
    state: dict[str, Any] | None = None
    if state_path.is_file():
        state = load_json(state_path)
        validate_state(state, task_list)
    return result(status_report(root, task_list, state))


def command_retry(args: argparse.Namespace) -> CommandResult:
    root = discover_project_root()
    _, task_list = load_task_input(args.task_list, root)
    state_path, _, _ = state_paths(root, task_list["list_id"])
    actor = getattr(args, "actor", "human")
    if actor not in {"human", "ai"}:
        raise LoopSailError(f"invalid retry actor: {actor}")
    with project_lock(root):
        if not state_path.is_file():
            raise LoopSailError("task list has not been started")
        state = load_json(state_path)
        validate_state(state, task_list)
        if args.task_id not in state["tasks"]:
            raise LoopSailError(f"unknown task: {args.task_id}")
        item = state["tasks"][args.task_id]
        if item["status"] != "blocked":
            raise LoopSailError(f"only a blocked task can be retried: {args.task_id}")
        if state.get("active_request") is not None:
            raise LoopSailError("cannot retry while an active request lease exists")
        ai_retry_count = int(item.get("ai_retry_count", 0))
        if actor == "ai" and ai_retry_count >= AI_RETRY_LIMIT:
            raise LoopSailError(
                f"AI retry limit reached for {args.task_id}; human confirmation is required"
            )
        item["attempt_sequence"] = max(
            int(item.get("attempt_sequence", 0)), int(item.get("attempts", 0))
        )
        item.update({"status": "pending", "attempts": 0, "updated_at": utc_now()})
        item["ai_retry_count"] = ai_retry_count + 1 if actor == "ai" else 0
        state["active_task"] = None
        state["active_request"] = None
        state["project_status"] = "executing"
        state["updated_at"] = utc_now()
        atomic_write_json(state_path, state)
    return result(
        {
            "schema_version": 2,
            "kind": "retry-report",
            "list_id": task_list["list_id"],
            "task_id": args.task_id,
            "actor": actor,
            "ai_retry_count": item["ai_retry_count"],
            "reason": args.reason,
            "next_action": "/loopsail:run-once",
            "at": utc_now(),
        }
    )


def task_step(state: dict[str, Any], task: dict[str, Any] | None) -> dict[str, Any] | None:
    if task is None:
        return None
    item = state["tasks"][task["id"]]
    failure = item.get("last_failure")
    return {
        "id": task["id"],
        "title": task["title"],
        "status": item["status"],
        "attempts": int(item.get("attempts", 0)),
        "attempt": int(item.get("attempt_sequence", 0)) or None,
        "commit": item.get("commit"),
        "failure": failure.get("summary") if isinstance(failure, dict) else None,
        "ai_retry_count": int(item.get("ai_retry_count", 0)),
    }


def step_report(
    task_list: dict[str, Any],
    state: dict[str, Any],
    *,
    action: str,
    performed: bool,
    task: dict[str, Any] | None = None,
    lease: dict[str, Any] | None = None,
    blocked_reason: str | None = None,
    next_action: str | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": 2,
        "kind": "step-report",
        "action": action,
        "performed": performed,
        "list_id": task_list["list_id"],
        "branch": state["branch"],
        "project_status": state["project_status"],
        "request_id": lease.get("request_id") if lease else None,
        "request_path": lease.get("request_path") if lease else None,
        "worker_agent": "loopsail:worker" if action == "spawn_worker" else None,
        "task": task_step(state, task),
        "blocked_reason": blocked_reason,
        "tasks_remaining": sum(
            state["tasks"][item["id"]]["status"] not in {"done", "superseded"}
            for item in task_list["tasks"]
        ),
        "next_action": next_action,
        "at": utc_now(),
    }


def write_step_report(state_path: Path, report: dict[str, Any]) -> None:
    atomic_write_json(state_path.parent / STEP_REPORT_FILE, report)


def attempt_paths(
    list_id: str, task_id: str, attempt: int, request_id: str
) -> tuple[str, str, str]:
    stem = f"{task_id}-attempt-{attempt}-{request_id}"
    return (
        f".loopsail/input/{list_id}/{stem}.json",
        f".loopsail/output/{list_id}/{stem}.json",
        f".loopsail/logs/{list_id}/{stem}.events.jsonl",
    )


def worker_request(
    root: Path,
    task_file: Path,
    task_list: dict[str, Any],
    task: dict[str, Any],
    item: dict[str, Any],
    config: dict[str, Any],
    request_id: str,
    attempt: int,
    request_path: str,
) -> dict[str, Any]:
    protected = list(
        dict.fromkeys(
            [
                *PROTECTED_PATTERNS,
                LESSONS_FILE,
                task_file.resolve().relative_to(root).as_posix(),
                *config["protected_paths"],
            ]
        )
    )
    return {
        "schema_version": 2,
        "kind": "worker-request",
        "request_id": request_id,
        "list_id": task_list["list_id"],
        "project": task_list["project"],
        "branch": f"loopsail/{task_list['list_id'].lower()}",
        "task_id": task["id"],
        "attempt": attempt,
        "task": task,
        "previous_failure": item.get("last_failure"),
        "policy": {
            "allowed_paths": task.get("allowed_paths") or [],
            "protected_paths": protected,
            "request_path": request_path,
        },
        "created_at": utc_now(),
    }


def attempt_log_path(root: Path, list_id: str, task_id: str, attempt: int) -> Path:
    return root / ".loopsail" / "logs" / list_id / f"{task_id}-attempt-{attempt}.json"


def write_attempt_log(
    root: Path,
    *,
    lease: dict[str, Any],
    status: str,
    failure_code: str | None,
    failure: str | None,
    actual_diff: list[str],
    fingerprint: str,
    verification: list[dict[str, Any]],
    commit: str | None,
    experience_recorded: bool,
) -> str:
    relative = (
        Path(".loopsail")
        / "logs"
        / lease["list_id"]
        / f"{lease['task_id']}-attempt-{lease['attempt']}.json"
    ).as_posix()
    payload = {
        "schema_version": 2,
        "kind": "attempt-log",
        "request_id": lease["request_id"],
        "list_id": lease["list_id"],
        "task_id": lease["task_id"],
        "attempt": lease["attempt"],
        "agent_id": lease.get("agent_id"),
        "status": status,
        "failure_code": failure_code,
        "failure": failure,
        "actual_diff": actual_diff,
        "diff_fingerprint": fingerprint,
        "worker_result_path": lease.get("output_path"),
        "event_log_path": lease.get("event_log_path"),
        "verification": verification,
        "commit": commit,
        "experience_recorded": experience_recorded,
        "at": utc_now(),
    }
    atomic_write_json(root / relative, payload)
    return relative


def record_orphaned_lease(
    root: Path,
    task_file: Path,
    task_list: dict[str, Any],
    state: dict[str, Any],
    state_path: Path,
    task: dict[str, Any],
    lease: dict[str, Any],
) -> dict[str, Any]:
    paths = changed_paths(root)
    fingerprint = diff_fingerprint(root, paths)
    message = "active worker request ended without a captured result"
    record_failure(state, task["id"], message, fingerprint, immediate=True)
    state["active_request"] = None
    state["last_finalized_request"] = {
        "request_id": lease["request_id"],
        "task_id": task["id"],
        "attempt": lease["attempt"],
        "status": "blocked",
        "at": utc_now(),
    }
    experience_recorded = False
    if not experience_file_changed(root, lease["experience_hash"]):
        experience_recorded = append_experience_record(
            root,
            task_list,
            task_id=task["id"],
            title=task["title"],
            attempt=lease["attempt"],
            outcome="阻塞",
            stage="Agent 中断恢复",
            lessons=[],
            failure=message,
            log_reference=experience_log_reference(
                task_list["list_id"], task["id"], lease["attempt"]
            ),
        )
    write_attempt_log(
        root,
        lease=lease,
        status="blocked",
        failure_code="orphaned_attempt_lease",
        failure=message,
        actual_diff=paths,
        fingerprint=fingerprint,
        verification=[],
        commit=None,
        experience_recorded=experience_recorded,
    )
    state["updated_at"] = utc_now()
    atomic_write_json(state_path, state)
    return step_report(
        task_list,
        state,
        action="blocked",
        performed=True,
        task=task,
        blocked_reason=message,
        next_action=f"/loopsail:retry {task['id']}",
    )


def run_final_verification_v2(
    root: Path,
    task_list: dict[str, Any],
    state: dict[str, Any],
    state_path: Path,
    snapshot_path: Path,
    config: dict[str, Any],
) -> tuple[bool, str | None]:
    attempt = int(state.get("final_verification_attempts", 0)) + 1
    passed, records, failure = execute_commands(
        root, task_list["final_verify_commands"], config
    )
    state["final_verification_attempts"] = attempt
    state["final_verification"] = {
        "status": "passed" if passed else "failed",
        "commands": records,
        "failure": failure,
        "at": utc_now(),
    }
    state["project_status"] = "complete" if passed else "blocked"
    state["active_task"] = None
    state["active_request"] = None
    experience_recorded = False
    if not passed:
        experience_recorded = append_experience_record(
            root,
            task_list,
            task_id="FINAL",
            title="最终验证",
            attempt=attempt,
            outcome="最终验证阻塞",
            stage="最终验证",
            lessons=[],
            failure=failure or "final verification failed",
            log_reference=experience_log_reference(
                task_list["list_id"], "FINAL", attempt
            ),
        )
    paths = changed_paths(root)
    write_attempt_log(
        root,
        lease={
            "request_id": f"final-{task_list['list_id']}-{attempt}",
            "list_id": task_list["list_id"],
            "task_id": "FINAL",
            "attempt": attempt,
            "agent_id": None,
            "output_path": None,
            "event_log_path": None,
        },
        status="done" if passed else "blocked",
        failure_code=None if passed else "final_verification_failed",
        failure=failure,
        actual_diff=paths,
        fingerprint=diff_fingerprint(root, paths),
        verification=records,
        commit=None,
        experience_recorded=experience_recorded,
    )
    state["updated_at"] = utc_now()
    atomic_write_json(state_path, state)
    atomic_write_json(snapshot_path, task_list)
    return passed, failure


def prepare_step(args: argparse.Namespace) -> CommandResult:
    root = discover_project_root()
    config, _ = load_config(root, args.runner_config)
    task_file, task_list = load_task_input(args.task_list, root)
    require_safe_experience_file(root)
    with project_lock(root):
        expected_state_path, _, _ = state_paths(root, task_list["list_id"])
        for other_path in sorted((root / ".loopsail" / "runs").glob("*/state.json")):
            if other_path == expected_state_path:
                continue
            other = load_json(other_path)
            if other.get("schema_version") == 2 and isinstance(
                other.get("active_request"), dict
            ):
                raise LoopSailError(
                    "another task list already owns an active attempt lease",
                    code="concurrent_attempt_lease",
                )
        state, state_path, snapshot_path, _, _ = initialize_or_load_state(
            root, task_file, task_list
        )
        if state["project_status"] == "complete":
            report = step_report(
                task_list,
                state,
                action="already_complete",
                performed=False,
                next_action=None,
            )
            write_step_report(state_path, report)
            return result(report)
        if state["project_status"] == "blocked":
            task = task_map(task_list).get(state.get("active_task"))
            failure = (
                state["tasks"][task["id"]].get("last_failure")
                if task is not None
                else state.get("final_verification")
            )
            reason = (
                failure.get("summary") or failure.get("failure")
                if isinstance(failure, dict)
                else "run is blocked"
            )
            report = step_report(
                task_list,
                state,
                action="blocked",
                performed=False,
                task=task,
                blocked_reason=reason,
                next_action=f"/loopsail:retry {task['id']}" if task else None,
            )
            write_step_report(state_path, report)
            return result(
                report, 2, error_code="run_blocked", error_message=reason
            )

        lease = state.get("active_request")
        if isinstance(lease, dict):
            task = task_map(task_list).get(lease.get("task_id"))
            if task is None:
                raise LoopSailError(
                    "active request task is absent from TASKS.json",
                    code="task_input_changed",
                )
            if (root / lease["output_path"]).is_file():
                report = step_report(
                    task_list,
                    state,
                    action="finalize_pending",
                    performed=False,
                    task=task,
                    lease=lease,
                    next_action="run finalize-step before spawning another worker",
                )
                write_step_report(state_path, report)
                return result(report, EXIT_PROGRESSED)
            report = record_orphaned_lease(
                root, task_file, task_list, state, state_path, task, lease
            )
            write_step_report(state_path, report)
            return result(
                report,
                2,
                error_code="orphaned_attempt_lease",
                error_message=str(report["blocked_reason"]),
            )

        if all_tasks_finished(task_list, state):
            passed, failure = run_final_verification_v2(
                root, task_list, state, state_path, snapshot_path, config
            )
            report = step_report(
                task_list,
                state,
                action="complete" if passed else "blocked",
                performed=True,
                blocked_reason=failure,
                next_action=None,
            )
            write_step_report(state_path, report)
            if passed:
                return result(report)
            return result(
                report,
                2,
                error_code="final_verification_failed",
                error_message=failure or "final verification failed",
            )

        task = ready_task(task_list, state)
        if task is None:
            report = step_report(
                task_list,
                state,
                action="idle",
                performed=False,
                blocked_reason="no dependency-ready task exists",
                next_action="/loopsail:status",
            )
            write_step_report(state_path, report)
            return result(report, EXIT_IDLE)

        item = state["tasks"][task["id"]]
        item["attempts"] = int(item.get("attempts", 0)) + 1
        item["attempt_sequence"] = max(
            int(item.get("attempt_sequence", 0)) + 1, item["attempts"]
        )
        attempt = item["attempt_sequence"]
        request_id = uuid.uuid4().hex
        request_path, output_path, event_log_path = attempt_paths(
            task_list["list_id"], task["id"], attempt, request_id
        )
        request = worker_request(
            root,
            task_file,
            task_list,
            task,
            item,
            config,
            request_id,
            attempt,
            request_path,
        )
        request_target = root / request_path
        if request_target.exists():
            raise LoopSailError("immutable worker request path already exists")
        atomic_write_json(request_target, request)
        lease = {
            "request_id": request_id,
            "list_id": task_list["list_id"],
            "task_id": task["id"],
            "attempt": attempt,
            "request_path": request_path,
            "output_path": output_path,
            "event_log_path": event_log_path,
            "task_input_hash": file_hash(task_file),
            "task_definition_hash": value_hash(task),
            "experience_hash": experience_file_hash(root),
            "head_commit": run_git(root, "rev-parse", "HEAD").stdout.strip(),
            "index_hash": git_index_hash(root),
            "agent_id": None,
            "session_id": None,
            "started_at": None,
            "captured_at": None,
            "correction_used": False,
            "last_protocol_error": None,
            "protocol_failure": False,
            "result_status": None,
            "event_sequence": 0,
            "event_truncated": False,
            "event_log_max_bytes": config["event_log_max_bytes"],
            "fatal_error": None,
            "created_at": utc_now(),
        }
        item["status"] = "running"
        item["updated_at"] = utc_now()
        state["active_task"] = task["id"]
        state["active_request"] = lease
        state["updated_at"] = utc_now()
        atomic_write_json(state_path, state)
        atomic_write_json(snapshot_path, task_list)
        report = step_report(
            task_list,
            state,
            action="spawn_worker",
            performed=True,
            task=task,
            lease=lease,
            next_action=(
                "invoke Agent with subagent_type loopsail:worker and "
                "run_in_background=false, then always run finalize-step"
            ),
        )
        write_step_report(state_path, report)
        return result(report, EXIT_PROGRESSED)


def failure_stage(code: str) -> str:
    return {
        "worker_result_missing": "Agent 结果捕获",
        "worker_protocol_failure": "Agent 输出协议",
        "agent_binding_error": "Agent 绑定校验",
        "task_input_changed": "任务清单安全校验",
        "experience_changed": "经验文件安全校验",
        "scope_violation": "修改范围校验",
        "worker_blocked": "Worker 主动阻塞",
        "verification_failed": "任务验证",
        "commit_failed": "任务提交",
        "git_state_changed": "Git 安全校验",
    }.get(code, "Coordinator 验收")


def finalize_step(args: argparse.Namespace) -> CommandResult:
    root = discover_project_root()
    config, _ = load_config(root, args.runner_config)
    task_file, task_list = load_task_input(args.task_list, root)
    state_path, _, _ = state_paths(root, task_list["list_id"])
    with project_lock(root):
        if not state_path.is_file():
            raise LoopSailError("task list has not been started")
        state = load_json(state_path)
        validate_state(state, task_list)
        lease = state.get("active_request")
        if not isinstance(lease, dict):
            previous = state.get("last_finalized_request")
            if isinstance(previous, dict):
                task = task_map(task_list).get(previous.get("task_id"))
                report = step_report(
                    task_list,
                    state,
                    action="finalized",
                    performed=False,
                    task=task,
                    next_action="/loopsail:run-once",
                )
                write_step_report(state_path, report)
                return result(report, EXIT_PROGRESSED)
            raise LoopSailError(
                "there is no active worker request to finalize",
                code="no_active_request",
            )
        task = task_map(task_list).get(lease.get("task_id"))
        if task is None:
            raise LoopSailError(
                "active request task is absent from TASKS.json",
                code="task_input_changed",
            )
        item = state["tasks"][task["id"]]
        output_path = root / lease["output_path"]
        worker_result: dict[str, Any] | None = None
        failure: str | None = None
        failure_code: str | None = None
        immediate = False
        verification: list[dict[str, Any]] = []
        actual_diff = changed_paths(root)
        fingerprint = diff_fingerprint(root, actual_diff)
        experience_integrity = not experience_file_changed(
            root, lease["experience_hash"]
        )

        if (
            run_git(root, "rev-parse", "HEAD").stdout.strip() != lease["head_commit"]
            or git_index_hash(root) != lease["index_hash"]
        ):
            failure_code = "git_state_changed"
            failure = "worker changed Git HEAD or the index; only Coordinator may mutate Git"
            immediate = True
        elif lease.get("fatal_error"):
            failure_code = "agent_binding_error"
            failure = str(lease["fatal_error"])
            immediate = True
        elif not output_path.is_file():
            failure_code = "worker_result_missing"
            failure = "worker agent ended without a captured worker-result"
        else:
            try:
                worker_result = validate_worker_result_v2(
                    load_json(output_path), binding=lease
                )
            except (ProtocolError, LoopSailError) as exc:
                failure_code = "worker_protocol_failure"
                failure = str(exc)
                immediate = True

        if failure is None and (
            not isinstance(lease.get("agent_id"), str) or not lease.get("captured_at")
        ):
            failure_code = "agent_binding_error"
            failure = "captured result is not bound to a started worker agent"
            immediate = True
        if failure is None and (
            not task_file.is_file()
            or file_hash(task_file) != lease["task_input_hash"]
            or value_hash(task) != lease["task_definition_hash"]
        ):
            failure_code = "task_input_changed"
            failure = "worker changed the protected task-list input"
            immediate = True
        if failure is None and not experience_integrity:
            failure_code = "experience_changed"
            failure = f"worker changed the protected experience log: {LESSONS_FILE}"
            immediate = True
        if failure is None and lease.get("protocol_failure"):
            failure_code = "worker_protocol_failure"
            failure = worker_result["blocker"] if worker_result else "worker protocol failed"
            immediate = True
        if failure is None and worker_result and worker_result["status"] == "blocked":
            failure_code = "worker_blocked"
            failure = worker_result["blocker"]
            immediate = True
        if failure is None:
            violations = scope_errors(actual_diff, task, config, task_file, root)
            if violations:
                failure_code = "scope_violation"
                failure = "; ".join(violations)
                immediate = True
        if failure is None:
            passed, verification, failure = execute_commands(
                root, task["verify_commands"], config
            )
            if not passed:
                failure_code = "verification_failed"

        experience_recorded = False
        commit: str | None = None
        if failure is None and worker_result is not None:
            experience_recorded = append_experience_record(
                root,
                task_list,
                task_id=task["id"],
                title=task["title"],
                attempt=lease["attempt"],
                outcome="验证通过",
                stage="子 Agent 实现与 Coordinator 验证",
                lessons=worker_result["lessons"],
                log_reference=experience_log_reference(
                    task_list["list_id"], task["id"], lease["attempt"]
                ),
            )
            try:
                commit = complete_task(
                    root,
                    task_file,
                    task_list,
                    task,
                    state,
                    state_path,
                    config,
                    verification,
                )
            except LoopSailError as exc:
                failure_code = "commit_failed"
                failure = str(exc)
                immediate = True

        blocked = False
        if failure is not None:
            blocked = record_failure(
                state,
                task["id"],
                failure,
                fingerprint,
                immediate=immediate,
            )
            if experience_integrity:
                try:
                    experience_recorded = append_experience_record(
                        root,
                        task_list,
                        task_id=task["id"],
                        title=task["title"],
                        attempt=lease["attempt"],
                        outcome="阻塞" if blocked else "等待重试",
                        stage=failure_stage(failure_code or "coordinator_failure"),
                        lessons=worker_result["lessons"] if worker_result else [],
                        failure=failure,
                        log_reference=experience_log_reference(
                            task_list["list_id"], task["id"], lease["attempt"]
                        ),
                    )
                except LoopSailError as exc:
                    failure_code = "experience_changed"
                    failure = f"experience recording failed: {exc}"
                    blocked = record_failure(
                        state, task["id"], failure, fingerprint, immediate=True
                    )
        state["active_request"] = None
        state["last_finalized_request"] = {
            "request_id": lease["request_id"],
            "task_id": task["id"],
            "attempt": lease["attempt"],
            "status": "done" if commit else ("blocked" if blocked else "retry"),
            "at": utc_now(),
        }
        state["updated_at"] = utc_now()
        atomic_write_json(state_path, state)
        write_attempt_log(
            root,
            lease=lease,
            status="done" if commit else ("blocked" if blocked else "retry"),
            failure_code=failure_code,
            failure=failure,
            actual_diff=actual_diff,
            fingerprint=fingerprint,
            verification=verification,
            commit=commit,
            experience_recorded=experience_recorded,
        )
        report = step_report(
            task_list,
            state,
            action="blocked" if blocked else "finalized",
            performed=True,
            task=task,
            blocked_reason=failure if blocked else None,
            next_action=(
                f"/loopsail:retry {task['id']}"
                if blocked
                else "/loopsail:run-once"
            ),
        )
        write_step_report(state_path, report)
        if blocked:
            return result(
                report,
                2,
                error_code=failure_code or "run_blocked",
                error_message=failure or "run blocked",
            )
        return result(report, EXIT_PROGRESSED)


def slash_args(
    *, actor: str | None = None, requested_task_id: str | None = None
) -> argparse.Namespace:
    root = discover_project_root()
    values: dict[str, Any] = {
        "runner_config": None,
        "task_list": root / INIT_TASK_FILE,
    }
    if actor is not None:
        if not requested_task_id or not ID_RE.fullmatch(requested_task_id):
            raise LoopSailError("retry requires exactly one valid task ID")
        _, task_list = load_task_input(root / INIT_TASK_FILE, root)
        state_path, _, _ = state_paths(root, task_list["list_id"])
        if not state_path.is_file():
            raise LoopSailError("task list has not been started")
        state = load_json(state_path)
        validate_state(state, task_list)
        if state.get("project_status") != "blocked":
            raise LoopSailError("there is no blocked task to retry")
        if state.get("active_task") != requested_task_id:
            raise LoopSailError(
                f"only the active blocked task can be retried: {state.get('active_task')}"
            )
        values.update(
            {
                "task_id": requested_task_id,
                "actor": actor,
                "reason": (
                    "AI supervisor classified the recorded failure as transient"
                    if actor == "ai"
                    else "human explicitly confirmed retry"
                ),
            }
        )
    return argparse.Namespace(**values)


def command_init_check(_: argparse.Namespace) -> CommandResult:
    start = Path.cwd().resolve()
    root = maybe_discover_project_root(start)
    has_head = bool(
        root
        and run_git(root, "rev-parse", "--verify", "HEAD", check=False).returncode
        == 0
    )
    return result(
        {
            "schema_version": 2,
            "kind": "init-check-report",
            "directory": str(start),
            "git_repository": root is not None,
            "project_root": str(root) if root else None,
            "has_head": has_head,
            "at": utc_now(),
        }
    )


def command_slash(args: argparse.Namespace) -> CommandResult:
    action = args.action
    if action not in {"retry-ai", "retry-human"} and args.task_id is not None:
        raise LoopSailError(f"slash action {action} does not accept arguments")
    if action == "doctor":
        return command_doctor(argparse.Namespace(runner_config=None))
    if action == "init-check":
        return command_init_check(args)
    if action in {"init", "init-confirmed"}:
        return command_init(
            argparse.Namespace(yes=action == "init-confirmed", runner_config=None)
        )
    if action == "validate":
        return command_validate(slash_args())
    if action == "status":
        return command_status(slash_args())
    if action == "prepare-step":
        return prepare_step(slash_args())
    if action == "finalize-step":
        return finalize_step(slash_args())
    if action in {"retry-ai", "retry-human"}:
        actor = "ai" if action == "retry-ai" else "human"
        return command_retry(
            slash_args(actor=actor, requested_task_id=args.task_id)
        )
    raise LoopSailError(f"unknown slash action: {action}")


class EnvelopeArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise LoopSailError(message, code="invalid_arguments")


def build_parser() -> argparse.ArgumentParser:
    parser = EnvelopeArgumentParser(description=__doc__)
    parser.add_argument(
        "--runner-config", type=Path, help="highest-priority loopsail configuration file"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    init = subparsers.add_parser("init", help="initialize a LoopSail project")
    init.add_argument("--yes", action="store_true")
    init.set_defaults(handler=command_init)
    doctor = subparsers.add_parser("doctor", help="validate the plugin runtime")
    doctor.set_defaults(handler=command_doctor)
    for name, handler in (
        ("validate", command_validate),
        ("status", command_status),
        ("prepare-step", prepare_step),
        ("finalize-step", finalize_step),
    ):
        command = subparsers.add_parser(name)
        command.add_argument("task_list", type=Path)
        command.set_defaults(handler=handler)
    retry = subparsers.add_parser("retry")
    retry.add_argument("task_list", type=Path)
    retry.add_argument("task_id")
    retry.add_argument("--reason", required=True)
    retry.add_argument("--actor", choices=("human", "ai"), default="human")
    retry.set_defaults(handler=command_retry)
    slash = subparsers.add_parser("slash")
    slash.add_argument(
        "action",
        choices=(
            "doctor",
            "init-check",
            "init",
            "init-confirmed",
            "validate",
            "prepare-step",
            "finalize-step",
            "status",
            "retry-ai",
            "retry-human",
        ),
    )
    slash.add_argument("task_id", nargs="?")
    slash.set_defaults(handler=command_slash)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    try:
        parser = build_parser()
        args = parser.parse_args(argv)
        data, exit_code, error = args.handler(args)
        envelope = command_envelope(
            ok=error is None,
            exit_code=exit_code,
            data=data,
            error=error,
        )
    except ProtocolError as exc:
        exit_code = 2
        envelope = command_envelope(
            ok=False,
            exit_code=exit_code,
            data=None,
            error={"code": exc.code, "message": str(exc), "details": None},
        )
    except LoopSailError as exc:
        exit_code = 2
        envelope = command_envelope(
            ok=False,
            exit_code=exit_code,
            data=None,
            error={"code": exc.code, "message": str(exc), "details": None},
        )
    except Exception as exc:
        exit_code = 2
        envelope = command_envelope(
            ok=False,
            exit_code=exit_code,
            data=None,
            error={
                "code": "internal_error",
                "message": f"{type(exc).__name__}: {exc}",
                "details": None,
            },
        )
    print(json.dumps(envelope, ensure_ascii=False, separators=(",", ":")))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
