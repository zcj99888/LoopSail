#!/usr/bin/env python3
"""Fail-closed tool guard shipped with loopsail."""

from __future__ import annotations

import fnmatch
import json
import os
import re
import sys
from pathlib import Path
from typing import Any


FILE_TOOLS = {"Read", "Edit", "Write", "Glob", "Grep"}
MUTATING_FILE_TOOLS = {"Edit", "Write"}
LESSONS_FILE = "经验记录.md"
PROTECTED = [
    "LOOP.md",
    "TASKS.template.json",
    LESSONS_FILE,
    ".claude/commands/loopsail-*.md",
    ".claude/skills/loopsail/**",
    ".loopsail/**",
    ".git/**",
]
BLOCKED_COMMANDS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(
            r"\bgit(?:\s+-C\s+\S+)?\s+(?:add|commit|push|merge|rebase|cherry-pick|"
            r"revert|tag|reset|clean|restore|checkout|switch|worktree|branch\s+-[dDmM])\b",
            re.I,
        ),
        "the coordinator exclusively owns Git mutations",
    ),
    (
        re.compile(r"\b(?:npm|pnpm|yarn)\s+publish\b|\b(?:twine\s+upload|docker\s+push)\b", re.I),
        "publishing is outside task authorization",
    ),
    (
        re.compile(
            r"\b(?:curl|wget)\b[^\n]*(?:--data(?:-[a-z-]+)?|-d\b|-F\b|--form|"
            r"--upload-file|-T\b|--post-data|--post-file|--method\s*=|"
            r"(?:-X|--request)\s*(?:POST|PUT|PATCH|DELETE))",
            re.I,
        ),
        "external writes are outside task authorization",
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
    (re.compile(r"\b(?:shred|truncate|mkfs|shutdown|reboot)\b", re.I), "destructive command is prohibited"),
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


def normalize_project_path(value: str) -> str | None:
    root_value = os.environ.get("LOOPSAIL_PROJECT_ROOT")
    if not root_value:
        return None
    root = Path(root_value).resolve()
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        candidate = root / candidate
    try:
        return candidate.resolve().relative_to(root).as_posix()
    except (OSError, ValueError):
        return None


def is_in_tool_directory(value: str) -> bool:
    root_value = os.environ.get("LOOPSAIL_TOOL_DIR")
    project_value = os.environ.get("LOOPSAIL_PROJECT_ROOT")
    if not root_value:
        return False
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        if not project_value:
            return False
        candidate = Path(project_value) / candidate
    try:
        candidate.resolve().relative_to(Path(root_value).resolve())
        return True
    except (OSError, ValueError):
        return False


def matches(path: str, pattern: str) -> bool:
    return fnmatch.fnmatchcase(path, pattern) or (
        pattern.endswith("/**") and path == pattern[:-3].rstrip("/")
    )


def sensitive(value: str) -> bool:
    normalized = value.replace("\\", "/").replace(".env.example", "")
    return bool(
        re.search(r"(?:^|[/\s'\"])(?:\.env(?:\.[^/\s'\"]+)?)($|[/\s'\"])", normalized)
        or re.search(r"(?:^|/)(?:secrets|credentials)(?:/|$)", normalized, re.I)
        or re.search(r"\.(?:pem|key|p12|pfx)(?:$|[\s'\"])", normalized, re.I)
    )


def decide(tool_name: str, tool_input: dict[str, Any]) -> str | None:
    values = strings(tool_input)
    if any(sensitive(value) for value in values):
        return "access to secret-bearing paths is prohibited"
    task_file = os.environ.get("LOOPSAIL_TASK_FILE")
    extra_protected = []
    if task_file:
        relative = normalize_project_path(task_file)
        if relative:
            extra_protected.append(relative)
    if tool_name in MUTATING_FILE_TOOLS:
        try:
            configured_protected = json.loads(os.environ.get("LOOPSAIL_PROTECTED_PATHS", "[]"))
        except json.JSONDecodeError:
            return "invalid loopsail protected-path policy"
        for value in values:
            if is_in_tool_directory(value):
                return "the loopsail installation directory may not be edited"
            relative = normalize_project_path(value)
            if relative and any(
                matches(relative, pattern)
                for pattern in PROTECTED + extra_protected + configured_protected
            ):
                return f"loopsail protected path may not be edited: {relative}"
        try:
            allowed = json.loads(os.environ.get("LOOPSAIL_ALLOWED_PATHS", "[]"))
        except json.JSONDecodeError:
            return "invalid loopsail allowed-path policy"
        if allowed:
            for value in values:
                relative = normalize_project_path(value)
                if relative and not any(matches(relative, pattern) for pattern in allowed):
                    return f"path is outside task allowed_paths: {relative}"
    if tool_name == "Bash":
        command = str(tool_input.get("command", ""))
        normalized_command = command.replace("\\", "/")
        if LESSONS_FILE in normalized_command:
            return "the coordinator exclusively owns the experience log"
        tool_root = os.environ.get("LOOPSAIL_TOOL_DIR")
        project_root = os.environ.get("LOOPSAIL_PROJECT_ROOT")
        if tool_root:
            tool_markers = {Path(tool_root).resolve().as_posix()}
            if project_root:
                tool_markers.add(
                    Path(os.path.relpath(Path(tool_root).resolve(), Path(project_root).resolve())).as_posix()
                )
            if any(marker and marker in normalized_command for marker in tool_markers):
                return "Bash access to the loopsail installation directory is prohibited"
        if re.search(r"(?:^|[/\s'\"])\.loopsail(?:/|$|[\s'\"])", normalized_command):
            return "Bash access to loopsail control files is prohibited"
        if task_file and task_file.replace("\\", "/") in normalized_command:
            return "Bash access to the task-list input is prohibited"
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
        tool_name = str(payload["tool_name"])
        tool_input = payload.get("tool_input", {})
        if not isinstance(tool_input, dict):
            raise ValueError("tool_input is not an object")
    except (KeyError, ValueError, TypeError, json.JSONDecodeError) as exc:
        deny(f"invalid guard input: {exc}")
        return 0
    reason = decide(tool_name, tool_input)
    if reason:
        deny(reason)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
