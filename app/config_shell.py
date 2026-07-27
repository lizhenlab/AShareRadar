from __future__ import annotations

import os
from pathlib import Path
import re
import shlex
import stat


LLM_SHELL_SECRET_ENV_NAMES = {"ASHARE_RADAR_LLM_API_KEY"}
_SHELL_BLOCK_WORD_RE = re.compile(r"(?:^|[;&|])\s*(if|for|while|until|case|select|repeat|fi|done|esac)\b")
_SHELL_BLOCK_OPENERS = frozenset({"if", "for", "while", "until", "case", "select", "repeat"})


def load_shell_env(path: Path, names: set[str]) -> dict[str, str]:
    values: dict[str, str] = {}
    group_depth = 0
    block_depth = 0
    quote: str | None = None
    continued = False
    heredocs: list[tuple[str, bool]] = []
    for line in _shell_env_lines(path):
        if heredocs:
            if _shell_heredoc_closed(line, heredocs[0]):
                heredocs.pop(0)
            continue
        if group_depth == 0 and block_depth == 0 and quote is None and not continued:
            values.update(_shell_env_line_values(line, names))
        control_text, quote = _shell_control_text(line, quote)
        group_depth, block_depth = _shell_nesting_after_control_text(control_text, group_depth, block_depth)
        heredocs.extend(_shell_heredoc_delimiters(line))
        continued = quote is not None or _shell_command_continues(line, control_text)
    _validate_shell_secret_permissions(path, values)
    return values


def _validate_shell_secret_permissions(path: Path, values: dict[str, str]) -> None:
    if not any(values.get(name) for name in LLM_SHELL_SECRET_ENV_NAMES):
        return
    try:
        metadata = path.stat()
    except OSError:
        raise ValueError("无法验证包含 LLM API Key 的 shell 配置文件权限") from None
    owned_by_current_user = not hasattr(os, "getuid") or metadata.st_uid == os.getuid()
    private_mode = stat.S_IMODE(metadata.st_mode) & 0o077 == 0
    if not stat.S_ISREG(metadata.st_mode) or not owned_by_current_user or not private_mode:
        raise ValueError("包含 LLM API Key 的 shell 配置文件权限过宽，请设置为仅当前用户可读写（chmod 600）")


def _shell_env_lines(path: Path) -> tuple[str, ...]:
    try:
        return tuple(path.read_text(encoding="utf-8").splitlines())
    except OSError:
        return ()


def _shell_env_line_values(line: str, names: set[str]) -> dict[str, str]:
    parts = _shell_env_words(line)
    if len(parts) == 1:
        part = parts[0]
    elif len(parts) == 2 and parts[0] == "export":
        part = parts[1]
    else:
        return {}
    assignment = _shell_env_assignment(part, names)
    return {assignment[0]: assignment[1]} if assignment is not None else {}


def _shell_env_words(line: str) -> tuple[str, ...]:
    try:
        lexer = shlex.shlex(line, posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        return tuple(lexer)
    except ValueError:
        return ()


def _shell_env_assignment(part: str, names: set[str]) -> tuple[str, str] | None:
    if "=" not in part:
        return None
    key, value = part.split("=", 1)
    stripped = value.strip()
    if key not in names or not stripped or any(marker in stripped for marker in ("$(", "`", "<(", ">(")):
        return None
    return key, stripped


def _shell_nesting_after_control_text(control_text: str, group_depth: int, block_depth: int) -> tuple[int, int]:
    for char in control_text:
        if char in "({":
            group_depth += 1
        elif char in ")}":
            group_depth = max(0, group_depth - 1)
    for match in _SHELL_BLOCK_WORD_RE.finditer(control_text):
        if match.group(1) in _SHELL_BLOCK_OPENERS:
            block_depth += 1
        else:
            block_depth = max(0, block_depth - 1)
    return group_depth, block_depth


def _shell_control_text(line: str, quote: str | None) -> tuple[str, str | None]:
    output: list[str] = []
    escaped = False
    for char in line:
        if escaped:
            output.append(" ")
            escaped = False
        elif char == "\\" and quote != "'":
            output.append(" ")
            escaped = True
        elif quote is not None:
            output.append(" ")
            if char == quote:
                quote = None
        elif char in {"'", '"'}:
            output.append(" ")
            quote = char
        elif char == "#":
            break
        else:
            output.append(char)
    return "".join(output), quote


def _shell_heredoc_delimiters(line: str) -> tuple[tuple[str, bool], ...]:
    parts = _shell_env_words(line)
    delimiters: list[tuple[str, bool]] = []
    for index, part in enumerate(parts[:-1]):
        if part != "<<":
            continue
        raw = parts[index + 1]
        strip_tabs = raw.startswith("-")
        delimiter = raw.removeprefix("-") if strip_tabs else raw
        if delimiter:
            delimiters.append((delimiter, strip_tabs))
    return tuple(delimiters)


def _shell_heredoc_closed(line: str, heredoc: tuple[str, bool]) -> bool:
    delimiter, strip_tabs = heredoc
    candidate = line.lstrip("\t") if strip_tabs else line
    return candidate == delimiter


def _shell_command_continues(line: str, control_text: str) -> bool:
    trailing_backslashes = len(line) - len(line.rstrip("\\"))
    if trailing_backslashes % 2 == 1:
        return True
    return control_text.rstrip().endswith(("&&", "||", "|", "|&"))


_load_shell_env = load_shell_env


__all__ = ["LLM_SHELL_SECRET_ENV_NAMES", "load_shell_env"]
