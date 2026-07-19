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
from pathlib import Path, PurePosixPath
from typing import Any, Iterator, Sequence


TOOL_ROOT = Path(__file__).resolve().parent.parent
SCHEMA_DIR = TOOL_ROOT / "references"
PROMPT_PATH = TOOL_ROOT / "references" / "worker.md"
SETTINGS_PATH = TOOL_ROOT / "references" / "claude-settings.json"
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
    ".loopsail/runs/",
    ".loopsail/logs/",
    ".loopsail/lock",
)
INIT_COMMIT_MESSAGE = "chore: initialize loopsail"

DEFAULT_CONFIG: dict[str, Any] = {
    "claude": {"command_prefix": ["claude"], "extra_args": []},
    "worker_timeout_seconds": 2700,
    "max_budget_usd": None,
    "log_output_limit_bytes": 65536,
    "protected_paths": [],
}
TOP_CONFIG_KEYS = set(DEFAULT_CONFIG)
CLAUDE_CONFIG_KEYS = {"command_prefix", "extra_args"}
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
LIST_REQUIRED = {"schema_version", "list_id", "project", "final_verify_commands", "tasks"}
LIST_OPTIONAL = {"$schema"}
STATUS_VALUES = {"pending", "running", "done", "blocked", "superseded"}
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
MAX_LESSONS_PER_ATTEMPT = 10
MAX_LESSON_FIELD_LENGTH = 2000
LESSON_FIELDS = {"challenge", "detour", "root_cause", "resolution", "takeaway"}
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


def claude_child_environment() -> dict[str, str]:
    """Return an environment suitable for a nested Claude CLI process."""
    return {
        key: value
        for key, value in os.environ.items()
        if key != "CLAUDECODE" and not key.startswith("CLAUDE_CODE_")
    }


def claude_launcher_argv(config: dict[str, Any], *args: str) -> list[str]:
    return (
        list(config["claude"]["command_prefix"])
        + list(config["claude"]["extra_args"])
        + list(args)
    )


def claude_launcher_overridden(config: dict[str, Any]) -> bool:
    return config["claude"] != DEFAULT_CONFIG["claude"]


def active_claude_profile() -> dict[str, str]:
    configured = os.environ.get("CLAUDE_CONFIG_DIR", "").strip()
    if configured:
        path = Path(configured).expanduser()
        if not path.is_absolute():
            path = Path.cwd() / path
        return {
            "inherited_config_dir": str(path.resolve()),
            "source": "CLAUDE_CONFIG_DIR",
        }
    return {
        "inherited_config_dir": str((Path.home() / ".claude").resolve()),
        "source": "default",
    }


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


def confirm(prompt: str) -> bool:
    try:
        answer = input(f"{prompt} [y/N] ")
    except EOFError:
        return False
    return answer.strip().lower() in {"y", "yes"}


def stdin_is_interactive() -> bool:
    return bool(getattr(sys.stdin, "isatty", lambda: False)())


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
    if "claude" in value:
        claude = value["claude"]
        if not isinstance(claude, dict):
            raise LoopSailError(f"claude must be an object in {label}")
        unknown_claude = set(claude) - CLAUDE_CONFIG_KEYS
        if unknown_claude:
            raise LoopSailError(
                f"unknown claude keys in {label}: {', '.join(sorted(unknown_claude))}"
            )
        for field in CLAUDE_CONFIG_KEYS & set(claude):
            ensure_string_list(
                claude[field], field=f"claude.{field}", nonempty=field == "command_prefix"
            )
    for field in ("worker_timeout_seconds", "log_output_limit_bytes"):
        if field in value and (
            not isinstance(value[field], int)
            or isinstance(value[field], bool)
            or value[field] <= 0
        ):
            raise LoopSailError(f"{field} must be a positive integer in {label}")
    if "max_budget_usd" in value:
        budget = value["max_budget_usd"]
        if budget is not None and (
            not isinstance(budget, (int, float))
            or isinstance(budget, bool)
            or budget <= 0
        ):
            raise LoopSailError(f"max_budget_usd must be null or positive in {label}")
    if "protected_paths" in value:
        for item in ensure_string_list(value["protected_paths"], field="protected_paths"):
            safe_relative_path(item, field="protected_paths")


def merge_config(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    result = json.loads(json.dumps(base))
    for key, value in overlay.items():
        if key == "claude":
            result["claude"].update(value)
        else:
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
    unknown = set(value) - LIST_REQUIRED - LIST_OPTIONAL
    missing = LIST_REQUIRED - set(value)
    if missing or unknown:
        details = []
        if missing:
            details.append("missing " + ", ".join(sorted(missing)))
        if unknown:
            details.append("unknown " + ", ".join(sorted(unknown)))
        raise LoopSailError("invalid task-list fields: " + "; ".join(details))
    if value["schema_version"] != 1:
        raise LoopSailError("schema_version must be 1")
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
        "schema_version": 1,
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
        "schema_version": 1,
        "list_id": task_list["list_id"],
        "project": task_list["project"],
        "task_file": str(task_file),
        "branch": branch,
        "base_commit": run_git(root, "rev-parse", "HEAD").stdout.strip(),
        "project_status": "executing",
        "active_task": None,
        "final_verification": None,
        "final_verification_attempts": 0,
        "tasks": {task["id"]: new_task_state(task) for task in task_list["tasks"]},
        "created_at": utc_now(),
        "updated_at": utc_now(),
    }


def validate_state(state: dict[str, Any], task_list: dict[str, Any]) -> None:
    if state.get("schema_version") != 1 or state.get("list_id") != task_list["list_id"]:
        raise LoopSailError("runtime state does not match the task list")
    if state.get("project_status") not in {"executing", "blocked", "complete"}:
        raise LoopSailError("runtime project_status is invalid")
    if not isinstance(state.get("tasks"), dict):
        raise LoopSailError("runtime tasks must be an object")
    for task_id, item in state["tasks"].items():
        if not isinstance(item, dict) or item.get("status") not in STATUS_VALUES:
            raise LoopSailError(f"runtime state is invalid for {task_id}")


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
    if stage == "Worker 执行":
        return "Worker 进程未成功返回有效结构化结果；详细输出见对应日志。"
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
                "- Worker 未返回可用的结构化复盘；Coordinator 仅记录已知结果，不推测根因。",
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
    output_limit = int(config["log_output_limit_bytes"])
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


def parse_json_tail(output: str) -> dict[str, Any]:
    decoder = json.JSONDecoder()
    starts = [index for index, char in enumerate(output) if char == "{"]
    for start in reversed(starts):
        try:
            value, end = decoder.raw_decode(output[start:])
        except json.JSONDecodeError:
            continue
        if output[start + end :].strip():
            continue
        if isinstance(value, dict):
            return value
    raise LoopSailError("Claude output did not end with a valid JSON object")


def extract_worker_result(payload: dict[str, Any]) -> dict[str, Any]:
    if isinstance(payload.get("structured_output"), dict):
        return payload["structured_output"]
    result = payload.get("result")
    if isinstance(result, str):
        with contextlib.suppress(json.JSONDecodeError):
            nested = json.loads(result)
            if isinstance(nested, dict):
                return nested
    return payload


def validate_worker_result(value: dict[str, Any], task_id: str) -> dict[str, Any]:
    required = {
        "task_id",
        "status",
        "summary",
        "changed_files",
        "verification_results",
        "lessons",
        "blocker",
    }
    if set(value) != required:
        raise LoopSailError("Worker result has invalid fields")
    if value["task_id"] != task_id or value["status"] not in {"completed", "blocked"}:
        raise LoopSailError("Worker result has an invalid task_id or status")
    if not isinstance(value["summary"], str):
        raise LoopSailError("Worker result summary must be a string")
    ensure_string_list(value["changed_files"], field="Worker changed_files")
    if not isinstance(value["verification_results"], list):
        raise LoopSailError("Worker verification_results must be an array")
    lessons = value["lessons"]
    if not isinstance(lessons, list) or len(lessons) > MAX_LESSONS_PER_ATTEMPT:
        raise LoopSailError(
            f"Worker lessons must be an array with at most {MAX_LESSONS_PER_ATTEMPT} items"
        )
    for index, lesson in enumerate(lessons):
        if not isinstance(lesson, dict) or set(lesson) != LESSON_FIELDS:
            raise LoopSailError(f"Worker lessons[{index}] has invalid fields")
        for field in ("challenge", "takeaway"):
            text = lesson[field]
            if (
                not isinstance(text, str)
                or not text.strip()
                or len(text) > MAX_LESSON_FIELD_LENGTH
            ):
                raise LoopSailError(f"Worker lessons[{index}].{field} is invalid")
        for field in ("detour", "root_cause", "resolution"):
            text = lesson[field]
            if text is not None and (
                not isinstance(text, str)
                or not text.strip()
                or len(text) > MAX_LESSON_FIELD_LENGTH
            ):
                raise LoopSailError(f"Worker lessons[{index}].{field} is invalid")
    if value["blocker"] is not None and not isinstance(value["blocker"], str):
        raise LoopSailError("Worker blocker must be null or a string")
    if value["status"] == "blocked" and not value["blocker"]:
        raise LoopSailError("blocked Worker result requires a blocker")
    return value


def worker_prompt(
    root: Path,
    task_file: Path,
    task: dict[str, Any],
    attempt: int,
    last_failure: dict[str, Any] | None,
) -> str:
    template = PROMPT_PATH.read_text(encoding="utf-8")
    payload = {
        "project_root": str(root),
        "task_file": str(task_file),
        "attempt": attempt,
        "task": task,
        "previous_failure": last_failure,
    }
    return template + "\n\n## Execution payload\n\n```json\n" + json.dumps(
        payload, ensure_ascii=False, indent=2
    ) + "\n```\n"


def invoke_worker(
    root: Path,
    task_file: Path,
    task: dict[str, Any],
    task_state: dict[str, Any],
    config: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    schema = load_json(SCHEMA_DIR / "worker-result.schema.json")
    prompt = worker_prompt(
        root, task_file, task, task_state["attempts"], task_state.get("last_failure")
    )
    argv = claude_launcher_argv(config)
    argv.extend(
        [
            "-p",
            "--no-session-persistence",
            "--strict-mcp-config",
            "--disable-slash-commands",
            "--output-format",
            "json",
            "--json-schema",
            json.dumps(schema, ensure_ascii=False),
            "--permission-mode",
            "dontAsk",
            "--allowedTools",
            "Read,Edit,Write,Glob,Grep,Bash",
            "--tools",
            "Read,Edit,Write,Glob,Grep,Bash",
            "--settings",
            str(SETTINGS_PATH),
            prompt,
        ]
    )
    if config["max_budget_usd"] is not None:
        insert_at = len(config["claude"]["command_prefix"]) + len(config["claude"]["extra_args"])
        argv[insert_at:insert_at] = ["--max-budget-usd", str(config["max_budget_usd"])]
    env = claude_child_environment()
    env.update(
        {
            "LOOPSAIL_TOOL_DIR": str(TOOL_ROOT),
            "LOOPSAIL_PROJECT_ROOT": str(root),
            "LOOPSAIL_TASK_FILE": str(task_file),
            "LOOPSAIL_ALLOWED_PATHS": json.dumps(task.get("allowed_paths") or []),
            "LOOPSAIL_PROTECTED_PATHS": json.dumps(config["protected_paths"]),
        }
    )
    started = time.monotonic()
    result = run_process(
        argv,
        cwd=root,
        timeout=int(config["worker_timeout_seconds"]),
        env=env,
    )
    metadata = {
        "exit_code": result.returncode,
        "duration_seconds": round(time.monotonic() - started, 3),
        "stdout_tail": bounded(result.stdout, int(config["log_output_limit_bytes"])),
        "stderr_tail": bounded(result.stderr, int(config["log_output_limit_bytes"])),
    }
    if result.returncode != 0:
        raise LoopSailError(
            f"Claude worker exited with {result.returncode}: "
            f"{metadata['stderr_tail'] or metadata['stdout_tail']}"
        )
    payload = parse_json_tail(result.stdout)
    worker_result = validate_worker_result(extract_worker_result(payload), task["id"])
    return worker_result, metadata


def save_attempt_log(
    logs_root: Path,
    list_id: str,
    task_id: str,
    attempt: int,
    payload: dict[str, Any],
) -> None:
    path = logs_root / list_id / f"{task_id}-attempt-{attempt}.json"
    atomic_write_json(path, payload)


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
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a+", encoding="utf-8")
    acquired = False
    lock_identity: tuple[int, int] | None = None
    try:
        try:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
            descriptor = os.fstat(handle.fileno())
            lock_identity = (descriptor.st_dev, descriptor.st_ino)
        except (ImportError, BlockingIOError) as exc:
            raise LoopSailError("another loopsail process is already running in this project") from exc
        handle.seek(0)
        handle.truncate()
        handle.write(str(os.getpid()))
        handle.flush()
        yield
    finally:
        if acquired:
            with contextlib.suppress(OSError):
                current = lock_path.lstat()
                if lock_identity == (current.st_dev, current.st_ino):
                    lock_path.unlink()
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
    state["project_status"] = "executing"
    state["updated_at"] = utc_now()
    atomic_write_json(state_path, state)
    return commit


def run_one_attempt(
    root: Path,
    task_file: Path,
    task_list: dict[str, Any],
    task: dict[str, Any],
    state: dict[str, Any],
    state_path: Path,
    logs_root: Path,
    config: dict[str, Any],
) -> bool:
    task_input_hash = file_hash(task_file)
    expected_experience_hash = experience_file_hash(root)
    item = state["tasks"][task["id"]]
    item["attempts"] += 1
    item["attempt_sequence"] = max(
        int(item.get("attempt_sequence", 0)) + 1, item["attempts"]
    )
    attempt_number = item["attempt_sequence"]
    item["status"] = "running"
    item["updated_at"] = utc_now()
    state["active_task"] = task["id"]
    state["updated_at"] = utc_now()
    atomic_write_json(state_path, state)
    worker_result: dict[str, Any] | None = None
    worker_meta: dict[str, Any] = {}
    failure: str | None = None
    failure_stage = "Worker 执行"
    immediate = False
    verification: list[dict[str, Any]] = []
    experience_integrity_ok = True
    experience_recorded = False
    try:
        worker_result, worker_meta = invoke_worker(root, task_file, task, item, config)
        if experience_file_changed(root, expected_experience_hash):
            failure = f"Worker changed or removed the protected experience log: {LESSONS_FILE}"
            failure_stage = "经验文件安全校验"
            immediate = True
            experience_integrity_ok = False
        elif not task_file.is_file() or file_hash(task_file) != task_input_hash:
            failure = "Worker changed or removed the protected task-list input"
            failure_stage = "任务清单安全校验"
            immediate = True
        elif worker_result["status"] == "blocked":
            failure = worker_result["blocker"]
            failure_stage = "Worker 主动阻塞"
            immediate = True
        else:
            failure_stage = "修改范围校验"
            paths = changed_paths(root)
            violations = scope_errors(paths, task, config, task_file, root)
            if violations:
                failure = "; ".join(violations)
                immediate = True
            else:
                failure_stage = "任务验证"
                passed, verification, failure = execute_commands(
                    root, task["verify_commands"], config
                )
                if passed:
                    failure_stage = "经验记录"
                    experience_recorded = append_experience_record(
                        root,
                        task_list,
                        task_id=task["id"],
                        title=task["title"],
                        attempt=attempt_number,
                        outcome="验证通过",
                        stage="Worker 实现与任务验证",
                        lessons=worker_result["lessons"],
                        log_reference=experience_log_reference(
                            task_list["list_id"], task["id"], attempt_number
                        ),
                    )
                    failure_stage = "任务提交"
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
                    save_attempt_log(
                        logs_root,
                        task_list["list_id"],
                        task["id"],
                        attempt_number,
                        {
                            "task_id": task["id"],
                            "attempt": attempt_number,
                            "retry_attempt": item["attempts"],
                            "status": "done",
                            "commit": commit,
                            "worker": worker_result,
                            "worker_process": worker_meta,
                            "verification": verification,
                            "experience_recorded": experience_recorded,
                        },
                    )
                    print(f"DONE {task['id']} {commit[:12]} {task['title']}")
                    return True
    except LoopSailError as exc:
        failure = str(exc)
        if failure_stage == "经验记录":
            immediate = True
    if not task_file.is_file() or file_hash(task_file) != task_input_hash:
        failure = "Worker changed or removed the protected task-list input"
        failure_stage = "任务清单安全校验"
        immediate = True
    if experience_file_changed(root, expected_experience_hash) and not experience_recorded:
        failure = f"Worker changed or removed the protected experience log: {LESSONS_FILE}"
        failure_stage = "经验文件安全校验"
        immediate = True
        experience_integrity_ok = False
    paths = changed_paths(root)
    fingerprint = diff_fingerprint(root, paths)
    blocked = record_failure(
        state, task["id"], failure or "unknown worker failure", fingerprint, immediate=immediate
    )
    if experience_integrity_ok:
        try:
            experience_recorded = append_experience_record(
                root,
                task_list,
                task_id=task["id"],
                title=task["title"],
                attempt=attempt_number,
                outcome="阻塞" if blocked else "等待重试",
                stage=failure_stage,
                lessons=(
                    worker_result["lessons"]
                    if worker_result is not None and not experience_recorded
                    else []
                ),
                failure=failure or "unknown worker failure",
                log_reference=experience_log_reference(
                    task_list["list_id"], task["id"], attempt_number
                ),
            )
        except LoopSailError as exc:
            failure = f"experience recording failed: {exc}"
            failure_stage = "经验记录"
            blocked = record_failure(state, task["id"], failure, fingerprint, immediate=True)
    atomic_write_json(state_path, state)
    save_attempt_log(
        logs_root,
        task_list["list_id"],
        task["id"],
        attempt_number,
        {
            "task_id": task["id"],
            "attempt": attempt_number,
            "retry_attempt": item["attempts"],
            "status": "blocked" if blocked else "retry",
            "failure": failure,
            "worker": worker_result,
            "worker_process": worker_meta,
            "verification": verification,
            "diff_fingerprint": fingerprint,
            "experience_recorded": experience_recorded,
        },
    )
    print(f"{'BLOCKED' if blocked else 'RETRY'} {task['id']}: {failure}")
    return not blocked


def resume_running_task(
    root: Path,
    task_file: Path,
    task_list: dict[str, Any],
    state: dict[str, Any],
    state_path: Path,
    logs_root: Path,
    config: dict[str, Any],
) -> bool:
    task_id = state.get("active_task")
    if not task_id or state["tasks"].get(task_id, {}).get("status") != "running":
        return False
    task = task_map(task_list).get(task_id)
    if task is None:
        raise LoopSailError(f"running task is absent from the task list: {task_id}")
    paths = changed_paths(root)
    task_paths = task_owned_paths(paths)
    attempt = int(
        state["tasks"][task_id].get(
            "attempt_sequence", state["tasks"][task_id]["attempts"]
        )
    )
    log_reference = experience_log_reference(task_list["list_id"], task_id, attempt)
    if task_paths:
        violations = scope_errors(paths, task, config, task_file, root)
        if violations:
            fingerprint = diff_fingerprint(root, paths)
            failure = "; ".join(violations)
            record_failure(state, task_id, failure, fingerprint, immediate=True)
            append_experience_record(
                root,
                task_list,
                task_id=task_id,
                title=task["title"],
                attempt=attempt,
                outcome="阻塞",
                stage="中断恢复范围校验",
                lessons=[],
                failure=failure,
                log_reference=log_reference,
            )
            atomic_write_json(state_path, state)
            save_attempt_log(
                logs_root,
                task_list["list_id"],
                task_id,
                attempt,
                {
                    "task_id": task_id,
                    "attempt": attempt,
                    "status": "blocked-after-interruption",
                    "failure": failure,
                    "diff_fingerprint": fingerprint,
                    "experience_recorded": True,
                },
            )
            return True
        passed, verification, failure = execute_commands(root, task["verify_commands"], config)
        if passed:
            append_experience_record(
                root,
                task_list,
                task_id=task_id,
                title=task["title"],
                attempt=attempt,
                outcome="中断后恢复成功",
                stage="执行中断恢复",
                lessons=[
                    {
                        "challenge": "上一次 Worker 执行被中断，但工作区保留了任务改动。",
                        "detour": None,
                        "root_cause": None,
                        "resolution": "Coordinator 对现有改动重新执行任务验证并完成提交。",
                        "takeaway": "中断后先检查并验证已有 diff，可以避免重复实现和丢失有效改动。",
                    }
                ],
                log_reference=log_reference,
            )
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
            save_attempt_log(
                logs_root,
                task_list["list_id"],
                task_id,
                attempt,
                {
                    "task_id": task_id,
                    "attempt": attempt,
                    "status": "done-after-interruption",
                    "commit": commit,
                    "verification": verification,
                    "experience_recorded": True,
                },
            )
            print(f"DONE {task_id} {commit[:12]} recovered after interruption")
            return True
        fingerprint = diff_fingerprint(root, paths)
        blocked = record_failure(state, task_id, failure or "verification failed", fingerprint)
        append_experience_record(
            root,
            task_list,
            task_id=task_id,
            title=task["title"],
            attempt=attempt,
            outcome="阻塞" if blocked else "等待重试",
            stage="中断恢复验证",
            lessons=[],
            failure=failure or "verification failed",
            log_reference=log_reference,
        )
        atomic_write_json(state_path, state)
        save_attempt_log(
            logs_root,
            task_list["list_id"],
            task_id,
            attempt,
            {
                "task_id": task_id,
                "attempt": attempt,
                "status": "blocked-after-interruption" if blocked else "retry-after-interruption",
                "failure": failure,
                "verification": verification,
                "diff_fingerprint": fingerprint,
                "experience_recorded": True,
            },
        )
        if blocked:
            print(f"BLOCKED {task_id}: {failure}")
            return True
    else:
        item = state["tasks"][task_id]
        item["status"] = "blocked" if item["attempts"] >= MAX_ATTEMPTS else "pending"
        state["project_status"] = "blocked" if item["status"] == "blocked" else "executing"
        state["active_task"] = task_id if item["status"] == "blocked" else None
        failure = "上一次 Worker 执行被中断，且没有留下任务范围内的工作区改动"
        item["last_failure"] = {
            "summary": normalize_failure(failure),
            "fingerprint": diff_fingerprint(root, paths),
            "recorded_at": utc_now(),
        }
        append_experience_record(
            root,
            task_list,
            task_id=task_id,
            title=task["title"],
            attempt=attempt,
            outcome="阻塞" if item["status"] == "blocked" else "等待重试",
            stage="执行中断恢复",
            lessons=[],
            failure=failure,
            log_reference=log_reference,
        )
        atomic_write_json(state_path, state)
        save_attempt_log(
            logs_root,
            task_list["list_id"],
            task_id,
            attempt,
            {
                "task_id": task_id,
                "attempt": attempt,
                "status": "blocked-after-interruption"
                if item["status"] == "blocked"
                else "retry-after-interruption",
                "failure": failure,
                "diff_fingerprint": item["last_failure"]["fingerprint"],
                "experience_recorded": True,
            },
        )
    return True


def print_init_summary(root: Path, result: dict[str, list[str]], *, git_initialized: bool) -> None:
    print(f"loopsail 初始化目录：{root}")
    if git_initialized:
        print("git: initialized")
    for label in ("created", "updated", "skipped"):
        values = result[label]
        print(f"{label}:")
        if values:
            for value in values:
                print(f"  - {value}")
        else:
            print("  - (none)")


def print_init_next_steps() -> None:
    print("下一步：")
    print("  1. 完善 CLAUDE.md 中的 TODO 项目规范。")
    print("  2. 填写 TASKS.json 中的全部必填项。")
    print("  3. 在 Claude Code 中运行 /loopsail:validate。")


def create_initial_commit(root: Path, paths: list[str]) -> str:
    for variable in ("GIT_AUTHOR_IDENT", "GIT_COMMITTER_IDENT"):
        identity = run_git(root, "var", variable, check=False)
        if identity.returncode != 0:
            raise LoopSailError(
                "初始化文件已生成，但 Git 用户身份未配置，无法创建初始提交；"
                "请配置 user.name 和 user.email 后手工提交"
            )
    try:
        run_git(root, "add", "--", *paths)
        run_git(root, "commit", "--only", "-m", INIT_COMMIT_MESSAGE, "--", *paths)
    except LoopSailError as exc:
        raise LoopSailError(
            "初始化文件已生成，但初始提交失败；本次文件可能仍处于暂存状态："
            f"{exc}"
        ) from exc
    return run_git(root, "rev-parse", "--short", "HEAD").stdout.strip()


def command_init(args: argparse.Namespace) -> int:
    start = Path.cwd().resolve()
    root = maybe_discover_project_root(start)
    git_initialized = False
    if root is None:
        if not args.yes:
            if not stdin_is_interactive():
                raise LoopSailError(
                    "当前目录不是 Git 仓库；请先执行 git init，或使用 loopsail init --yes"
                )
            if not confirm(f"当前目录不是 Git 仓库，是否在 {start} 执行 git init？"):
                raise LoopSailError("已取消初始化；未创建 Git 仓库或 loopsail 文件")
        run_process(["git", "init"], cwd=start, check=True)
        git_initialized = True
        root = discover_project_root(start)

    had_head = run_git(root, "rev-parse", "--verify", "HEAD", check=False).returncode == 0
    result = initialize_scaffold(root)
    print_init_summary(root, result, git_initialized=git_initialized)

    touched = result["touched"]
    if not had_head and touched:
        should_commit = args.yes
        if not should_commit and stdin_is_interactive():
            should_commit = confirm("当前仓库还没有提交，是否提交本次初始化文件？")
        if should_commit:
            commit = create_initial_commit(root, touched)
            print(f"commit: {commit} ({INIT_COMMIT_MESSAGE})")
        else:
            print("commit: skipped；请审阅后手工创建初始提交。")

    print_init_next_steps()
    return 0


def command_validate(args: argparse.Namespace) -> int:
    root = discover_project_root()
    config, sources = load_config(root, args.runner_config)
    _, task_list = load_task_input(args.task_list, root)
    print(
        json.dumps(
            {
                "valid": True,
                "list_id": task_list["list_id"],
                "tasks": len(task_list["tasks"]),
                "config_sources": sources,
                "launcher_kind": (
                    "configured-prefix" if claude_launcher_overridden(config) else "active-profile"
                ),
                "worker_timeout_seconds": config["worker_timeout_seconds"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def command_doctor(args: argparse.Namespace) -> int:
    root = maybe_discover_project_root() or Path.cwd().resolve()
    config, sources = load_config(root, args.runner_config)
    timeout = min(60, config["worker_timeout_seconds"])
    env = claude_child_environment()
    version_result = run_process(
        claude_launcher_argv(config, "--version"),
        cwd=root,
        timeout=timeout,
        env=env,
    )
    if version_result.returncode != 0:
        raise LoopSailError(
            "Claude launcher failed version check with exit code "
            f"{version_result.returncode}"
        )
    version = (
        version_result.stdout.strip().splitlines()
        or version_result.stderr.strip().splitlines()
        or ["ok"]
    )[-1]

    auth_result = run_process(
        claude_launcher_argv(config, "auth", "status", "--json"),
        cwd=root,
        timeout=timeout,
        env=env,
    )
    if auth_result.returncode != 0:
        raise LoopSailError(
            "Claude launcher failed authentication check with exit code "
            f"{auth_result.returncode}"
        )
    try:
        auth = parse_json_tail(auth_result.stdout)
    except LoopSailError as exc:
        raise LoopSailError("Claude authentication status was not valid JSON") from exc
    if auth.get("loggedIn") is not True:
        raise LoopSailError("Claude launcher is not authenticated")
    auth_method = auth.get("authMethod")
    api_provider = auth.get("apiProvider")
    if auth_method is not None and not isinstance(auth_method, str):
        raise LoopSailError("Claude authentication method was invalid")
    if api_provider is not None and not isinstance(api_provider, str):
        raise LoopSailError("Claude API provider was invalid")

    overridden = claude_launcher_overridden(config)
    print(
        json.dumps(
            {
                "healthy": True,
                "config_sources": sources,
                "launcher_kind": "configured-prefix" if overridden else "active-profile",
                "launcher_overridden": overridden,
                "claude_profile": active_claude_profile(),
                "claude_version": version[-200:],
                "authentication": {
                    "logged_in": True,
                    "method": auth_method[:100] if auth_method is not None else None,
                    "provider": api_provider[:100] if api_provider is not None else None,
                },
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def command_status(args: argparse.Namespace) -> int:
    root = discover_project_root()
    load_config(root, args.runner_config)
    _, task_list = load_task_input(args.task_list, root)
    state_path, _, _ = state_paths(root, task_list["list_id"])
    if state_path.is_file():
        state = load_json(state_path)
        validate_state(state, task_list)
    else:
        state = create_state(task_list, args.task_list.resolve(), root)
    rows = []
    for task in task_list["tasks"]:
        item = state["tasks"].get(task["id"], new_task_state(task))
        rows.append(
            {
                "id": task["id"],
                "status": item["status"],
                "attempts": item["attempts"],
                "ai_retry_count": int(item.get("ai_retry_count", 0)),
                "last_failure": (
                    item.get("last_failure", {}).get("summary")
                    if isinstance(item.get("last_failure"), dict)
                    else None
                ),
                "definition_changed": item["definition_hash"] != value_hash(task),
                "title": task["title"],
            }
        )
    print(
        json.dumps(
            {
                "list_id": task_list["list_id"],
                "project_status": state["project_status"],
                "branch": state["branch"],
                "active_task": state.get("active_task"),
                "final_verification": state.get("final_verification"),
                "tasks": rows,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def command_retry(args: argparse.Namespace) -> int:
    root = discover_project_root()
    _, task_list = load_task_input(args.task_list, root)
    state_path, _, logs_root = state_paths(root, task_list["list_id"])
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
        ai_retry_count = int(item.get("ai_retry_count", 0))
        if actor == "ai" and ai_retry_count >= AI_RETRY_LIMIT:
            raise LoopSailError(
                f"AI retry limit reached for {args.task_id}; "
                "human confirmation is required through /loopsail:retry"
            )
        item["attempt_sequence"] = max(
            int(item.get("attempt_sequence", 0)), int(item["attempts"])
        )
        item.update({"status": "pending", "attempts": 0, "updated_at": utc_now()})
        item["ai_retry_count"] = ai_retry_count + 1 if actor == "ai" else 0
        state["active_task"] = None
        state["project_status"] = "executing"
        state["updated_at"] = utc_now()
        atomic_write_json(state_path, state)
        save_attempt_log(
            logs_root,
            task_list["list_id"],
            args.task_id,
            0,
            {
                "task_id": args.task_id,
                "status": f"{actor}-retry",
                "actor": actor,
                "ai_retry_count": item["ai_retry_count"],
                "reason": args.reason,
                "at": utc_now(),
            },
        )
    print(f"retry enabled for {args.task_id}")
    return 0


def slash_task_args(
    *, actor: str | None = None, requested_task_id: str | None = None
) -> argparse.Namespace:
    root = discover_project_root()
    values: dict[str, Any] = {
        "runner_config": None,
        "task_list": root / INIT_TASK_FILE,
    }
    if actor is not None:
        _, task_list = load_task_input(root / INIT_TASK_FILE, root)
        state_path, _, _ = state_paths(root, task_list["list_id"])
        if not state_path.is_file():
            raise LoopSailError("task list has not been started")
        state = load_json(state_path)
        validate_state(state, task_list)
        active_task = state.get("active_task")
        if state.get("project_status") != "blocked" or not active_task:
            raise LoopSailError("there is no active blocked task to retry")
        if not requested_task_id or not ID_RE.fullmatch(requested_task_id):
            raise LoopSailError("retry requires one valid task ID")
        if requested_task_id != active_task:
            raise LoopSailError(
                f"only the active blocked task can be retried: {active_task}"
            )
        item = state["tasks"][active_task]
        failure = item.get("last_failure")
        failure_summary = (
            failure.get("summary") if isinstance(failure, dict) else "recorded blocker"
        )
        values.update(
            {
                "task_id": active_task,
                "actor": actor,
                "reason": (
                    "AI supervisor classified the recorded blocker as transient after "
                    f"reviewing the public report and attempt log: {failure_summary}"
                    if actor == "ai"
                    else "human confirmed retry after reviewing the reported blocker: "
                    f"{failure_summary}"
                ),
            }
        )
    return argparse.Namespace(**values)


def command_slash(args: argparse.Namespace) -> int:
    action = args.action
    if action not in {"retry-ai", "retry-human"} and args.task_id is not None:
        raise LoopSailError(f"slash action {action} does not accept arguments")
    if action == "doctor":
        return command_doctor(argparse.Namespace(runner_config=None))
    if action == "init-check":
        start = Path.cwd().resolve()
        root = maybe_discover_project_root(start)
        has_head = bool(
            root
            and run_git(root, "rev-parse", "--verify", "HEAD", check=False).returncode == 0
        )
        print(
            json.dumps(
                {
                    "directory": str(start),
                    "git_repository": root is not None,
                    "project_root": str(root) if root else None,
                    "has_head": has_head,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    if action in {"init", "init-confirmed"}:
        return command_init(
            argparse.Namespace(yes=action == "init-confirmed", runner_config=None)
        )
    if action == "validate":
        return command_validate(slash_task_args())
    if action == "status":
        return command_status(slash_task_args())
    if action in {"run-once", "run-all"}:
        run_args = slash_task_args()
        run_args.once = action == "run-once"
        return command_run(run_args)
    if action in {"retry-ai", "retry-human"}:
        actor = "ai" if action == "retry-ai" else "human"
        return command_retry(
            slash_task_args(actor=actor, requested_task_id=args.task_id)
        )
    raise LoopSailError(f"unknown slash action: {action}")


def run_final_verification(
    root: Path,
    task_list: dict[str, Any],
    state: dict[str, Any],
    state_path: Path,
    snapshot_path: Path,
    logs_root: Path,
    config: dict[str, Any],
) -> tuple[bool, str | None, list[dict[str, Any]]]:
    final_attempt = int(state.get("final_verification_attempts", 0)) + 1
    passed, records, failure = execute_commands(
        root, task_list["final_verify_commands"], config
    )
    if not passed:
        log_reference = experience_log_reference(
            task_list["list_id"], "FINAL", final_attempt
        )
        append_experience_record(
            root,
            task_list,
            task_id="FINAL",
            title="最终验证",
            attempt=final_attempt,
            outcome="最终验证阻塞",
            stage="最终验证",
            lessons=[],
            failure=failure or "final verification failed",
            log_reference=log_reference,
        )
        save_attempt_log(
            logs_root,
            task_list["list_id"],
            "FINAL",
            final_attempt,
            {
                "task_id": "FINAL",
                "attempt": final_attempt,
                "status": "blocked",
                "failure": failure,
                "verification": records,
                "experience_recorded": True,
            },
        )
    state["final_verification_attempts"] = final_attempt
    state["final_verification"] = {
        "status": "passed" if passed else "failed",
        "commands": records,
        "failure": failure,
        "at": utc_now(),
    }
    state["project_status"] = "complete" if passed else "blocked"
    state["active_task"] = None
    state["updated_at"] = utc_now()
    atomic_write_json(state_path, state)
    atomic_write_json(snapshot_path, task_list)
    return passed, failure, records


def step_task_report(
    root: Path,
    task_list: dict[str, Any],
    state: dict[str, Any],
    task_id: str | None,
) -> dict[str, Any] | None:
    if task_id is None:
        return None
    task = task_map(task_list).get(task_id)
    item = state["tasks"].get(task_id)
    if task is None or item is None:
        return None
    attempt_value = int(item.get("attempt_sequence", item.get("attempts", 0)))
    attempt = attempt_value if attempt_value > 0 else None
    attempt_log = (
        experience_log_reference(task_list["list_id"], task_id, attempt)
        if attempt is not None
        else None
    )
    last_failure = item.get("last_failure")
    failure = last_failure.get("summary") if isinstance(last_failure, dict) else None
    ai_retry_count = int(item.get("ai_retry_count", 0))
    return {
        "id": task_id,
        "title": task["title"],
        "status": item["status"],
        "attempts": int(item.get("attempts", 0)),
        "attempt": attempt,
        "commit": item.get("commit"),
        "failure": failure,
        "attempt_log": attempt_log,
        "ai_retry_count": ai_retry_count,
        "ai_retries_remaining": max(0, AI_RETRY_LIMIT - ai_retry_count),
    }


def step_experience_records(root: Path, reference: str | None) -> list[str]:
    if reference is None:
        return []
    path = root / reference
    if not path.is_file():
        return []
    try:
        payload = load_json(path)
    except LoopSailError:
        return []
    return [reference] if payload.get("experience_recorded") is True else []


def build_step_report(
    root: Path,
    task_list: dict[str, Any],
    state: dict[str, Any],
    *,
    kind: str,
    performed: bool,
    exit_code: int,
    task_id: str | None = None,
    blocked_reason: str | None = None,
    experience_records: list[str] | None = None,
) -> dict[str, Any]:
    if blocked_reason is None and state["project_status"] == "blocked":
        active_task = state.get("active_task")
        if active_task is not None:
            active_state = state["tasks"].get(active_task, {})
            failure = active_state.get("last_failure")
            if isinstance(failure, dict):
                blocked_reason = failure.get("summary")
        if blocked_reason is None:
            final_verification = state.get("final_verification")
            if isinstance(final_verification, dict):
                blocked_reason = final_verification.get("failure")

    next_task = None
    if state["project_status"] == "executing" and not all_tasks_finished(task_list, state):
        ready = ready_task(task_list, state)
        if ready is not None:
            next_task = {"id": ready["id"], "title": ready["title"]}

    final_report = None
    final_verification = state.get("final_verification")
    if isinstance(final_verification, dict):
        final_report = {
            "status": final_verification.get("status"),
            "failure": final_verification.get("failure"),
        }

    return {
        "schema_version": 1,
        "kind": kind,
        "performed": performed,
        "list_id": task_list["list_id"],
        "branch": state["branch"],
        "project_status": state["project_status"],
        "exit_code": exit_code,
        "task": step_task_report(root, task_list, state, task_id),
        "blocked_reason": blocked_reason,
        "next_ready_task": next_task,
        "tasks_remaining": sum(
            state["tasks"][task["id"]]["status"] not in {"done", "superseded"}
            for task in task_list["tasks"]
        ),
        "final_verification": final_report,
        "experience_records": experience_records or [],
        "at": utc_now(),
    }


def write_step_report(state_path: Path, report: dict[str, Any]) -> dict[str, Any]:
    atomic_write_json(state_path.parent / STEP_REPORT_FILE, report)
    return report


def run_step(
    root: Path,
    task_file: Path,
    task_list: dict[str, Any],
    state: dict[str, Any],
    state_path: Path,
    snapshot_path: Path,
    logs_root: Path,
    config: dict[str, Any],
) -> dict[str, Any]:
    if state["project_status"] == "complete":
        return write_step_report(
            state_path,
            build_step_report(
                root,
                task_list,
                state,
                kind="already-complete",
                performed=False,
                exit_code=0,
            ),
        )

    if state["project_status"] == "blocked":
        return write_step_report(
            state_path,
            build_step_report(
                root,
                task_list,
                state,
                kind="blocked",
                performed=False,
                exit_code=2,
                task_id=state.get("active_task"),
            ),
        )

    active_task = state.get("active_task")
    if active_task and state["tasks"].get(active_task, {}).get("status") == "running":
        resume_running_task(
            root, task_file, task_list, state, state_path, logs_root, config
        )
        task_report = step_task_report(root, task_list, state, active_task)
        attempt_log = task_report["attempt_log"] if task_report is not None else None
        exit_code = 2 if state["project_status"] == "blocked" else EXIT_PROGRESSED
        return write_step_report(
            state_path,
            build_step_report(
                root,
                task_list,
                state,
                kind="resume",
                performed=True,
                exit_code=exit_code,
                task_id=active_task,
                experience_records=step_experience_records(root, attempt_log),
            ),
        )

    if not all_tasks_finished(task_list, state):
        task = ready_task(task_list, state)
        if task is None:
            pending = [
                item["id"]
                for item in task_list["tasks"]
                if state["tasks"][item["id"]]["status"] == "pending"
            ]
            reason = f"no runnable task; pending tasks: {', '.join(pending)}"
            return write_step_report(
                state_path,
                build_step_report(
                    root,
                    task_list,
                    state,
                    kind="idle",
                    performed=False,
                    exit_code=EXIT_IDLE,
                    blocked_reason=reason,
                ),
            )
        run_one_attempt(
            root,
            task_file,
            task_list,
            task,
            state,
            state_path,
            logs_root,
            config,
        )
        task_report = step_task_report(root, task_list, state, task["id"])
        attempt_log = task_report["attempt_log"] if task_report is not None else None
        exit_code = 2 if state["project_status"] == "blocked" else EXIT_PROGRESSED
        return write_step_report(
            state_path,
            build_step_report(
                root,
                task_list,
                state,
                kind="attempt",
                performed=True,
                exit_code=exit_code,
                task_id=task["id"],
                experience_records=step_experience_records(root, attempt_log),
            ),
        )

    passed, failure, _ = run_final_verification(
        root, task_list, state, state_path, snapshot_path, logs_root, config
    )
    final_attempt = int(state.get("final_verification_attempts", 0))
    final_log = (
        experience_log_reference(task_list["list_id"], "FINAL", final_attempt)
        if not passed
        else None
    )
    if not passed:
        print(f"BLOCKED final verification: {failure}")
    else:
        print(f"COMPLETE {task_list['list_id']} on {state['branch']}")
    return write_step_report(
        state_path,
        build_step_report(
            root,
            task_list,
            state,
            kind="final-verification",
            performed=True,
            exit_code=0 if passed else 2,
            experience_records=step_experience_records(root, final_log),
        ),
    )


def command_run(args: argparse.Namespace) -> int:
    root = discover_project_root()
    config, _ = load_config(root, args.runner_config)
    task_file, task_list = load_task_input(args.task_list, root)
    require_safe_experience_file(root)
    once = getattr(args, "once", False)
    with project_lock(root):
        state, state_path, snapshot_path, logs_root, _ = initialize_or_load_state(
            root, task_file, task_list
        )
        while True:
            report = run_step(
                root,
                task_file,
                task_list,
                state,
                state_path,
                snapshot_path,
                logs_root,
                config,
            )
            if once:
                print(json.dumps(report, ensure_ascii=False))
                return int(report["exit_code"])
            if report["kind"] == "already-complete":
                print(f"COMPLETE {task_list['list_id']} (already complete)")
                return 0
            if report["kind"] == "blocked":
                task_id = state.get("active_task") or "final verification"
                raise LoopSailError(
                    f"run is blocked at {task_id}; update the list or use "
                    f"/loopsail:retry {task_id}"
                )
            if report["kind"] == "idle":
                raise LoopSailError(str(report["blocked_reason"]))
            if report["exit_code"] in {0, 2}:
                return int(report["exit_code"])


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--runner-config", type=Path, help="highest-priority loopsail configuration file"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    init = subparsers.add_parser("init", help="在当前仓库生成通用 loopsail 项目骨架")
    init.add_argument(
        "--yes",
        action="store_true",
        help="自动同意创建 Git 仓库和初始提交的确认",
    )
    init.set_defaults(handler=command_init)
    doctor = subparsers.add_parser("doctor", help="validate the configured Claude launcher")
    doctor.set_defaults(handler=command_doctor)
    for name, help_text, handler in (
        ("validate", "validate configuration and a task list", command_validate),
        (
            "run",
            "execute or resume a task list; use --once for one JSON-reported step",
            command_run,
        ),
        ("status", "show task-list runtime status", command_status),
    ):
        command = subparsers.add_parser(name, help=help_text)
        command.add_argument("task_list", type=Path)
        if name == "run":
            command.add_argument(
                "--once",
                action="store_true",
                help="execute one progress unit and emit a JSON step report",
            )
        command.set_defaults(handler=handler)
    retry = subparsers.add_parser(
        "retry", help="retry a blocked task with --actor human or ai"
    )
    retry.add_argument("task_list", type=Path)
    retry.add_argument("task_id")
    retry.add_argument("--reason", required=True)
    retry.add_argument("--actor", choices=("human", "ai"), default="human")
    retry.set_defaults(handler=command_retry)
    slash = subparsers.add_parser(
        "slash",
        help="internal fixed-argument adapter for Claude Code slash commands",
    )
    slash.add_argument(
        "action",
        choices=(
            "doctor",
            "init-check",
            "init",
            "init-confirmed",
            "validate",
            "run-once",
            "run-all",
            "status",
            "retry-ai",
            "retry-human",
        ),
    )
    slash.add_argument("task_id", nargs="?")
    slash.set_defaults(handler=command_slash)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.handler(args))
    except LoopSailError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
