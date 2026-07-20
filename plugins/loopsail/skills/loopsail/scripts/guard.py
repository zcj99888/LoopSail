#!/usr/bin/env python3
"""Fail-closed policy guard for the bound loopsail:worker subagent."""

from __future__ import annotations

import fnmatch
import json
import os
import re
import sys
from pathlib import Path
from typing import Any


ALLOWED_TOOLS = {"Read", "Edit", "Write", "Glob", "Grep", "Bash"}
FILE_TOOLS = {"Read", "Edit", "Write", "Glob", "Grep"}
MUTATING_FILE_TOOLS = {"Edit", "Write"}
LESSONS_FILE = "经验记录.md"
BUILTIN_PROTECTED = [
    "LOOP.md",
    "TASKS.template.json",
    LESSONS_FILE,
    ".claude/commands/loopsail-*.md",
    ".claude/skills/loopsail/**",
    ".loopsail/**",
    ".git/**",
]
SECRET_RE = re.compile(
    r"(?:^|[/\s'\"])(?:\.env(?:\.[^/\s'\"]+)?)($|[/\s'\"])|"
    r"(?:^|/)(?:secrets|credentials)(?:/|$)|"
    r"\.(?:pem|key|p12|pfx)(?:$|[\s'\"])",
    re.I,
)
BLOCKED_COMMANDS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(
            r"\bgit\b[^\n;&|]*\b(?:add|commit|push|merge|rebase|"
            r"cherry-pick|revert|tag|reset|clean|restore|checkout|switch|"
            r"worktree)\b|\bgit\b[^\n;&|]*\bbranch\b[^\n;&|]*\s-[dDmM]\b",
            re.I,
        ),
        "the coordinator exclusively owns Git mutations",
    ),
    (
        re.compile(
            r"\b(?:claude\s+plugin|npm|pnpm|yarn)\s+(?:install|uninstall|update|publish)\b|"
            r"\b(?:twine\s+upload|docker\s+push)\b",
            re.I,
        ),
        "installation and publishing are outside worker authorization",
    ),
    (
        re.compile(
            r"\b(?:curl|wget)\b[^\n]*(?:--data(?:-[a-z-]+)?|-d\b|-F\b|--form|"
            r"--upload-file|-T\b|--post-data|--post-file|--method\s*=|"
            r"(?:-X|--request)\s*(?:POST|PUT|PATCH|DELETE))",
            re.I,
        ),
        "external writes are outside worker authorization",
    ),
    (
        re.compile(
            r"\brm\s+(?:-(?:[^\s]*r[^\s]*f|[^\s]*f[^\s]*r)\b|"
            r"(?=[^\n]*(?:--recursive|-r)\b)(?=[^\n]*(?:--force|-f)\b))",
            re.I,
        ),
        "recursive forced deletion is prohibited",
    ),
    (re.compile(r"\bfind\b[^\n]*\s-delete\b", re.I), "bulk deletion is prohibited"),
    (
        re.compile(r"\b(?:shred|truncate|mkfs|shutdown|reboot)\b", re.I),
        "destructive command is prohibited",
    ),
)


def matches(path: str, pattern: str) -> bool:
    return fnmatch.fnmatchcase(path, pattern) or (
        pattern.endswith("/**") and path == pattern[:-3].rstrip("/")
    )


def strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        result: list[str] = []
        for nested in value.values():
            result.extend(strings(nested))
        return result
    if isinstance(value, list):
        result = []
        for nested in value:
            result.extend(strings(nested))
        return result
    return []


def normalize_project_path(root: Path, value: str) -> str | None:
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        candidate = root / candidate
    try:
        return candidate.resolve().relative_to(root.resolve()).as_posix()
    except (OSError, ValueError):
        return None


def sensitive(value: str) -> bool:
    normalized = value.replace("\\", "/").replace(".env.example", "")
    return bool(SECRET_RE.search(normalized))


def context_from_environment() -> dict[str, Any]:
    """Compatibility helper for direct tests; production hooks pass explicit state."""
    root = Path(os.environ.get("LOOPSAIL_PROJECT_ROOT", ".")).resolve()
    request = os.environ.get("LOOPSAIL_REQUEST_PATH", ".loopsail/input/active.json")
    return {
        "root": root,
        "tool_root": Path(os.environ.get("LOOPSAIL_TOOL_DIR", root.parent / "loopsail")).resolve(),
        "request_path": normalize_project_path(root, request) or request,
        "task_file": normalize_project_path(
            root, os.environ.get("LOOPSAIL_TASK_FILE", "TASKS.json")
        )
        or "TASKS.json",
        "allowed_paths": json.loads(os.environ.get("LOOPSAIL_ALLOWED_PATHS", "[]")),
        "protected_paths": json.loads(os.environ.get("LOOPSAIL_PROTECTED_PATHS", "[]")),
    }


def decide(
    tool_name: str,
    tool_input: dict[str, Any],
    context: dict[str, Any] | None = None,
) -> str | None:
    """Return a denial reason, or None when the bound worker action is allowed."""
    if tool_name not in ALLOWED_TOOLS:
        return f"tool is not allowed for loopsail:worker: {tool_name}"
    if not isinstance(tool_input, dict):
        return "tool input must be an object"
    try:
        policy = context or context_from_environment()
        root = Path(policy["root"]).resolve()
        tool_root = Path(policy["tool_root"]).resolve()
        request_path = str(policy["request_path"])
        task_file = str(policy["task_file"])
        allowed = list(policy.get("allowed_paths") or [])
        protected = BUILTIN_PROTECTED + list(policy.get("protected_paths") or []) + [task_file]
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return "invalid loopsail worker policy"

    values = strings(tool_input)
    if any(sensitive(value) for value in values):
        return "access to secret-bearing paths is prohibited"

    if tool_name in FILE_TOOLS:
        file_values = [
            value
            for key, value in tool_input.items()
            if key in {"file_path", "path", "notebook_path"}
            and isinstance(value, str)
        ]
        paths: list[tuple[str, str | None]] = [
            (value, normalize_project_path(root, value)) for value in file_values
        ]
        if not paths and tool_name in {"Glob", "Grep"}:
            paths = [(".", ".")]
        if not paths:
            return "file tool input does not identify a target path"
        for raw, relative in paths:
            candidate = Path(raw).expanduser()
            if not candidate.is_absolute():
                candidate = root / candidate
            try:
                candidate.resolve().relative_to(tool_root)
                return "the loopsail installation directory is outside worker authorization"
            except (OSError, ValueError):
                pass
            if relative is None:
                return "path is outside the project"
            if relative.startswith(".loopsail/") or relative == ".loopsail":
                if tool_name == "Read" and relative == request_path:
                    continue
                return "only the bound immutable worker request may be read from .loopsail"
            if tool_name in MUTATING_FILE_TOOLS:
                if any(matches(relative, pattern) for pattern in protected):
                    return f"loopsail protected path may not be edited: {relative}"
                if allowed and not any(matches(relative, pattern) for pattern in allowed):
                    return f"path is outside task allowed_paths: {relative}"

    if tool_name == "Bash":
        command = str(tool_input.get("command", ""))
        normalized = command.replace("\\", "/")
        if not command.strip():
            return "empty Bash command is not allowed"
        if SECRET_RE.search(normalized):
            return "access to secret-bearing paths is prohibited"
        if re.search(r"(?:^|[/\s'\"])\.loopsail(?:/|$|[\s'\"])", normalized):
            return "Bash access to loopsail control files is prohibited"
        if task_file in normalized or LESSONS_FILE in normalized:
            return "the coordinator exclusively owns task and experience control files"
        markers = {tool_root.as_posix()}
        try:
            markers.add(Path(os.path.relpath(tool_root, root)).as_posix())
        except ValueError:
            pass
        if any(marker and marker not in {".", ".."} and marker in normalized for marker in markers):
            return "Bash access to the loopsail installation directory is prohibited"
        for pattern, reason in BLOCKED_COMMANDS:
            if pattern.search(command):
                return reason
    return None


def deny(reason: str) -> None:
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


def main() -> int:
    try:
        payload = json.load(sys.stdin)
        reason = decide(str(payload["tool_name"]), payload.get("tool_input", {}))
    except (KeyError, ValueError, TypeError, json.JSONDecodeError) as exc:
        reason = f"invalid guard input: {exc}"
    if reason:
        deny(reason)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
