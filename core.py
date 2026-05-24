from __future__ import annotations

import argparse
from html import unescape
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import shutil
import signal
import sys
import tempfile
import threading
import time
from typing import Any
import unicodedata
from types import SimpleNamespace

# --- Sibling Runtime Bridge -------------------------------------------------
#
# `core.py` 和 `projectling.py` 被刻意保持在同一目录下，不拆包。
# 为了让外部入口、单测式导入、以及从其它 cwd 调用时都能稳定找到
# 同目录的 `projectling.py`，这里显式把当前目录压进 `sys.path` 前部。
PROJECTLING_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECTLING_DIR))

from projectling import (
    ChatResult,
    DeepSeekClient,
    DeepSeekAPIError,
    LauncherRole,
    PersonaBundle,
    load_context_budget,
    ProjectLingConfig,
    ProjectLingEngine,
    PromptBundle,
    ToolContext,
    ToolRegistry,
    _collab_mode_value,
    _format_remaining_text,
    _remaining_seconds_for_role,
    build_roll_sequence,
    confirm_pending_command,
    load_config,
    load_external_context,
    load_role_context,
    load_roster,
    persona_path_for_role,
    reject_pending_command,
    render_animation_frame,
    render_motd_card,
    reroll_active_role,
    resolve_active_role,
    resolve_current_role,
    resolve_persona_bundle,
    save_env_config,
    scrub_volatile_memory_entries,
    select_current_role_by_name,
    select_liaison_role_by_name,
    show_pending_command,
)
from tooling import (
    _execute_apply_patch_tool,
    _execute_contextmanage_tool,
    _execute_update_plan_tool,
    context_entries_status,
    ensure_memory_layout,
    memory_status,
)


# --- Static Model Choices ---------------------------------------------------
#
# DeepSeek 当前这条接入链路不提供稳定的官方模型 list 接口，因此这里保留
# 一组保守默认项，同时允许用户手输自定义模型名。
MODEL_CHOICES: list[tuple[str, str]] = [
    ("deepseek-chat", "通用聊天"),
    ("deepseek-reasoner", "深度推理"),
]

COLLAB_MODE_CHOICES: tuple[tuple[str, str, str], ...] = (
    ("rapid", "快速模式", "chat+chat"),
    ("standard", "标准模式", "reasoner+chat"),
    ("precise", "精确模式", "reasoner+reasoner"),
)
COLLAB_MODE_ORDER = tuple(mode for mode, _label, _desc in COLLAB_MODE_CHOICES)
COLLAB_MODE_ALIASES = {
    "1": "rapid",
    "fast": "rapid",
    "quick": "rapid",
    "迅速": "rapid",
    "快速": "rapid",
    "2": "standard",
    "normal": "standard",
    "std": "standard",
    "标准": "standard",
    "3": "precise",
    "accurate": "precise",
    "exact": "precise",
    "精确": "precise",
    "精准": "precise",
}
COLLAB_MODE_CYCLE_ALIASES = {"next", "cycle", "toggle", "切换", "下一个", "轮换"}
COLLAB_MODE_STATUS_ALIASES = {"status", "current", "show", "当前", "状态"}

SHELL_DISPATCH_MODES = {"chat", "command_not_found", "send"}
LEGACY_RUNTIME_FILES = ("shell_history.json",)
LEGACY_ROOT_RUNTIME_FILES = ("pending-command.json", "update-plan.json")
THINKING_PREVIEW_MAX_LINES = 8
THINKING_FOLD_DELAY_SECONDS = 0.7
THINKING_RENDER_INTERVAL_SECONDS = 0.28
WORKING_ANIMATION_INTERVAL_SECONDS = 0.75
TOOL_PREVIEW_HEAD_LINES = 2
TOOL_PREVIEW_TAIL_LINES = 3

# --- ANSI + Markdown Rendering ---------------------------------------------
ANSI_RESET = "\033[0m"
ANSI_BOLD = "\033[1m"
ANSI_DIM = "\033[2m"
ANSI_ITALIC = "\033[3m"
ANSI_UNDERLINE = "\033[4m"
ANSI_CYAN = "\033[38;2;0;255;229m"
ANSI_MAGENTA = "\033[38;2;255;92;218m"
ANSI_WHITE = "\033[97m"
ANSI_GOLD = "\033[38;2;255;220;120m"
ANSI_QUOTE = "\033[38;2;182;194;224m"
ANSI_RULE = "\033[38;2;96;108;138m"
ANSI_LINK = "\033[38;2;120;220;255m"
ANSI_VIOLET = "\033[1;38;2;170;120;255m"
ANSI_SOFT_PINK = "\033[38;2;255;178;214m"
ANSI_SOFT_RED = "\033[38;2;255;120;152m"
ANSI_SOFT_BLUE = "\033[38;2;150;218;255m"
ANSI_MUTED_BLUE = "\033[38;2;148;178;196m"
ANSI_MUTED_TEXT = "\033[38;2;184;194;210m"
ANSI_SOFT_GREEN = "\033[38;2;142;220;184m"
ANSI_BADGE_BG = "\033[48;2;26;38;45m"
ANSI_CTX_BG = "\033[48;2;22;43;47m"
ANSI_CTX_FG = "\033[38;2;191;232;224m"
ANSI_BG_INVERT = "\033[47m"
ANSI_FG_INVERT = "\033[30m"

ANSI_PATTERN = re.compile(r"\x1b\[[0-9;]*m")
PATHLIKE_TOKEN_RE = re.compile(r"(?<![A-Za-z0-9_.-])(?:~(?:/[^\s\"'<>|;&]+)?|\$PREFIX(?:/[^\s\"'<>|;&]+)?|/[^\s\"'<>|;&]+)")
TOOL_OMISSION_RE = re.compile(r"^\.\.\.\s+\+\d+\s+lines$")
MARKDOWN_LINK_RE = re.compile(r"\[([^\]\n]+)\]\(([^)\n]+)\)")
MARKDOWN_IMAGE_RE = re.compile(r"!\[([^\]\n]*)\]\(([^)\n]+)\)")
MARKDOWN_AUTO_LINK_RE = re.compile(r"<(https?://[^>\n]+)>")
MARKDOWN_BARE_URL_RE = re.compile(r"(?<![<(/])\b(https?://[^\s)>]+)")
MARKDOWN_REFERENCE_LINK_RE = re.compile(r"!\[([^\]\n]*)\]\[([^\]\n]*)\]|\[([^\]\n]+)\]\[([^\]\n]*)\]")
MARKDOWN_REFERENCE_DEF_RE = re.compile(r'^\s*\[([^\]\n]+)\]:\s*<?(\S+?)>?(?:\s+(?:"[^"]*"|\'[^\']*\'|\([^)]+\)))?\s*$')
MARKDOWN_FOOTNOTE_REF_RE = re.compile(r"\[\^([^\]\n]+)\]")
MARKDOWN_FOOTNOTE_DEF_RE = re.compile(r"^\s*\[\^([^\]\n]+)\]:\s*(.*)$")
MARKDOWN_CODE_RE = re.compile(r"`([^`\n]+)`")
MARKDOWN_STREAM_BLOCK_RE = re.compile(
    r"(?m)^\s*(?:#{1,6}\s+|[-+*]\s+(?:\[[ xX]\]\s+)?|\d+\.\s+(?:\[[ xX]\]\s+)?|>\s*|```+|~~~+|\|)"
)
MARKDOWN_BOLD_ITALIC_RE = re.compile(r"(\*\*\*|___)(.+?)\1")
MARKDOWN_BOLD_RE = re.compile(r"(\*\*|__)(.+?)\1")
MARKDOWN_ITALIC_STAR_RE = re.compile(r"(?<!\*)\*(?!\s)(.+?)(?<!\s)\*(?!\*)")
MARKDOWN_ITALIC_UNDERSCORE_RE = re.compile(r"(?<!\w)_(?!\s)(.+?)(?<!\s)_(?!\w)")
MARKDOWN_STRIKE_RE = re.compile(r"~~(.+?)~~")
MARKDOWN_HIGHLIGHT_RE = re.compile(r"==(.+?)==")
MARKDOWN_HTML_BREAK_RE = re.compile(r"<br\s*/?>", re.IGNORECASE)
MARKDOWN_SETEXT_H1_RE = re.compile(r"^\s*=+\s*$")
MARKDOWN_SETEXT_H2_RE = re.compile(r"^\s*-+\s*$")
MARKDOWN_TABLE_ALIGN_RE = re.compile(r"^:?-{3,}:?$")
MARKDOWN_HTML_ANCHOR_RE = re.compile(r'<a\s+[^>]*href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', re.IGNORECASE)
MARKDOWN_HTML_IMG_RE = re.compile(
    r'<img\s+[^>]*src=["\']([^"\']+)["\'][^>]*alt=["\']([^"\']*)["\'][^>]*>|<img\s+[^>]*alt=["\']([^"\']*)["\'][^>]*src=["\']([^"\']+)["\'][^>]*>',
    re.IGNORECASE,
)
MARKDOWN_HTML_STRONG_RE = re.compile(r"<(?:strong|b)>(.*?)</(?:strong|b)>", re.IGNORECASE)
MARKDOWN_HTML_EM_RE = re.compile(r"<(?:em|i)>(.*?)</(?:em|i)>", re.IGNORECASE)
MARKDOWN_HTML_DEL_RE = re.compile(r"<(?:del|s|strike)>(.*?)</(?:del|s|strike)>", re.IGNORECASE)
MARKDOWN_HTML_CODE_RE = re.compile(r"<(?:code|kbd)>(.*?)</(?:code|kbd)>", re.IGNORECASE)
MARKDOWN_HTML_MARK_RE = re.compile(r"<mark>(.*?)</mark>", re.IGNORECASE)
MARKDOWN_HTML_UNDERLINE_RE = re.compile(r"<u>(.*?)</u>", re.IGNORECASE)
MARKDOWN_HTML_STRIP_RE = re.compile(r"</?(?:details|summary|div|span|p|section|article|main|small|sub|sup|ul|ol|li|table|thead|tbody|tr|td|th|blockquote|center|font)[^>]*>", re.IGNORECASE)
STREAM_SENTENCE_ENDINGS = "。！？.!?；;：:"
ESCAPED_MARKDOWN_TOKENS = {
    r"\*": "\uFFF0",
    r"\_": "\uFFF1",
    r"\`": "\uFFF2",
    r"\[": "\uFFF3",
    r"\]": "\uFFF4",
    r"\(": "\uFFF5",
    r"\)": "\uFFF6",
    r"\~": "\uFFF7",
    r"\#": "\uFFF8",
    r"\+": "\uFFF9",
    r"\-": "\uFFFA",
    r"\!": "\uFFFB",
    r"\>": "\uFFFC",
}
EXPLORE_SEARCH_COMMANDS = {"find", "grep", "rg"}
EXPLORE_READ_COMMANDS = {"cat", "file", "head", "readlink", "sed", "stat", "tail", "wc"}
EXPLORE_LIST_COMMANDS: set[str] = set()
GROUPABLE_BASH_COMMANDS = {
    "date",
    "df",
    "du",
    "echo",
    "env",
    "free",
    "git",
    "id",
    "ip",
    "netstat",
    "printenv",
    "ps",
    "pwd",
    "ss",
    "test",
    "uname",
    "whoami",
    "which",
}
GROUPABLE_GIT_READONLY_SUBCOMMANDS = {"branch", "describe", "diff", "log", "remote", "rev-parse", "show", "status", "tag"}
STATUS_SUCCESS_TEXT = {"ok": "Succeed", "empty": "Succeed", "stopped": "Succeed"}
FIND_MUTATING_TOKENS = {"-delete", "-exec", "-execdir", "-ok", "-okdir", "-fdelete"}


def _supports_tty_control() -> bool:
    term = os.environ.get("TERM", "")
    return bool(sys.stdout.isatty() and term and term.lower() != "dumb")


def _style_heading(text: str, kind: str = "deepseek") -> str:
    if not _supports_tty_control():
        return text
    color = ANSI_CYAN if kind == "deepseek" else ANSI_MAGENTA if kind == "thinking" else ANSI_WHITE
    return f"{ANSI_BOLD}{color}{text}{ANSI_RESET}"


def _style_status(text: str, kind: str) -> str:
    if not _supports_tty_control():
        return text
    color = ANSI_MAGENTA if kind == "thinking" else ANSI_WHITE
    return f"{ANSI_DIM}{color}{text}{ANSI_RESET}"


def _style_context_text(text: str) -> str:
    if not _supports_tty_control():
        return text
    return f"{ANSI_CTX_BG}{ANSI_CTX_FG}{ANSI_BOLD} {text} {ANSI_RESET}"


def _style_badge(text: str, *, color: str = ANSI_MUTED_TEXT, background: str = ANSI_BADGE_BG) -> str:
    if not _supports_tty_control():
        return f"[{text}]"
    return f"{background}{color}{ANSI_BOLD} {text} {ANSI_RESET}"


def _style_thought_text(text: str) -> str:
    if not _supports_tty_control():
        return text
    return f"{ANSI_DIM}{ANSI_RULE}{text}{ANSI_RESET}"


def _format_thought_summary(elapsed_seconds: float | None) -> str:
    if elapsed_seconds is None:
        return "THOUGHT"
    seconds = max(0.0, float(elapsed_seconds))
    if seconds < 0.1:
        seconds = 0.1
    if seconds < 10:
        return f"THOUGHT FOR {seconds:.1f}s"
    return f"THOUGHT FOR {seconds:.0f}s"


def _context_budget_percent(state: dict[str, Any] | None) -> int:
    if not state:
        return 100
    try:
        raw_percent = state.get("percent")
        if raw_percent is None or raw_percent == "":
            return 100
        return max(0, min(100, int(raw_percent)))
    except (TypeError, ValueError):
        return 100


def _payload_percent(payload: dict[str, Any], default: int = 100) -> int:
    for key in ("context_budget_percent", "percent"):
        value = payload.get(key)
        if value is None or value == "":
            continue
        try:
            return max(0, min(100, int(value)))
        except (TypeError, ValueError):
            continue
    return max(0, min(100, int(default)))


def _context_budget_bar(percent: int, *, width: int = 8) -> str:
    width = max(1, int(width))
    percent = max(0, min(100, int(percent)))
    filled = round(width * percent / 100)
    filled = max(0, min(width, filled))
    return f"[{'█' * filled}{'░' * (width - filled)}]"


def _context_budget_line(state: dict[str, Any] | None) -> str:
    percent = _context_budget_percent(state)
    return _style_context_text(f"ctx {percent}%")


def _format_role_heading(role: LauncherRole, persona_bundle: PersonaBundle | None = None) -> str:
    bundle = persona_bundle or PersonaBundle(main=role)
    liaison_sources = {"speaker_handoff", "executor_handoff", "persona_link_mission", "liaison_tool"}
    speaker_label = "辅导位" if bundle.source in liaison_sources else "主角色"
    mode_badge = ""
    speaker = bundle.main
    if not _supports_tty_control():
        suffix = f" · {mode_badge}" if mode_badge else ""
        return f"● {speaker_label} · {speaker.name_zh} · {speaker.name_en}{suffix}"
    dot = f"{ANSI_DIM}{ANSI_WHITE} · {ANSI_RESET}"
    label = f"{ANSI_DIM}{ANSI_WHITE}{speaker_label}{ANSI_RESET}"
    if speaker_label == "辅导位":
        role_color = ANSI_VIOLET if mode_badge else ANSI_CYAN
        name = f"{role_color}{speaker.name_zh}{ANSI_RESET}"
        name_en = f"{ANSI_ITALIC}{role_color}{speaker.name_en}{ANSI_RESET}"
    else:
        name = f"{ANSI_BOLD}{ANSI_GOLD}{speaker.name_zh}{ANSI_RESET}"
        name_en = f"{ANSI_BOLD}{ANSI_ITALIC}{ANSI_GOLD}{speaker.name_en}{ANSI_RESET}"
    badge = f" {_style_badge(mode_badge, color=ANSI_CTX_FG)}" if mode_badge else ""
    return f"● {label}{dot}{name}{dot}{name_en}{badge}"


def _role_from_roster_payload(payload: dict[str, Any], *keys: str) -> LauncherRole | None:
    names = [str(payload.get(key) or "").strip() for key in keys]
    names = [name for name in names if name]
    if not names:
        return None
    try:
        roster = load_roster(load_config())
    except Exception:
        return None
    expanded_names: set[str] = set()
    for name in names:
        expanded_names.add(name)
        for part in re.split(r"[/·|]", name):
            part = part.strip()
            if part:
                expanded_names.add(part)
    normalized = {name.lower() for name in expanded_names}
    for role in roster:
        if role.name_en.lower() in normalized or role.name_zh.lower() in normalized:
            return role
    return None


def _role_from_roster_payload_priority(payload: dict[str, Any], *keys: str) -> LauncherRole | None:
    for key in keys:
        role = _role_from_roster_payload(payload, key)
        if role is not None:
            return role
    return None


def _persona_from_handoff_payload(payload: dict[str, Any]) -> tuple[LauncherRole, PersonaBundle] | None:
    tool_name = str(payload.get("tool") or "")
    action_name = str(payload.get("action") or payload.get("speaker_mode") or "").strip().lower()
    if tool_name == "persona_link" and action_name != "switch":
        return None
    if tool_name == "link" and action_name != "switch":
        return None
    if tool_name not in {"persona_handoff", "persona_link", "link"}:
        return None
    if str(payload.get("status") or "") != "ok":
        return None
    target = str(payload.get("target") or payload.get("speaker_mode") or "").strip().lower()
    speaker = _role_from_roster_payload(payload, "speaker_name_en", "speaker_name_zh", "speaker_name")
    main_role = _role_from_roster_payload(payload, "main_name_en", "main_name_zh", "main_name")
    liaison_role = _role_from_roster_payload(payload, "liaison_name_en", "liaison_name_zh", "liaison_name")
    if target == "liaison" and speaker is not None:
        return speaker, PersonaBundle(main=speaker, liaison=main_role, source="speaker_handoff")
    if target == "main" and (main_role is not None or speaker is not None):
        active = main_role or speaker
        return active, PersonaBundle(main=active, liaison=liaison_role, source="selected" if liaison_role else "solo")
    return None


def _split_shell_words(text: str) -> list[str]:
    command = str(text or "").strip()
    if not command:
        return []
    try:
        return shlex.split(command)
    except ValueError:
        return command.split()


def _shorten_path_token(token: str) -> str:
    text = str(token or "")
    if _display_width(text) <= 52 or "/" not in text:
        return text
    if text.startswith("~/"):
        root = "~/"
        parts = [part for part in text[2:].split("/") if part]
    elif text.startswith("$PREFIX/"):
        root = "$PREFIX/"
        parts = [part for part in text[len("$PREFIX/") :].split("/") if part]
    elif text.startswith("/"):
        root = "/"
        parts = [part for part in text[1:].split("/") if part]
    else:
        return _middle_truncate_display(text, 52)
    if len(parts) <= 3:
        return _middle_truncate_display(text, 52)
    for head_count, tail_count in ((2, 2), (1, 2), (1, 1)):
        head = "/".join(parts[:head_count])
        tail = "/".join(parts[-tail_count:])
        candidate = f"{root}{head}/…/{tail}".replace("//", "/")
        if _display_width(candidate) <= 52:
            return candidate
    return _middle_truncate_display(text, 52)


def _style_tool_omission(text: str) -> str:
    if not _supports_tty_control() or not TOOL_OMISSION_RE.match(str(text or "").strip()):
        return text
    return f"{ANSI_BOLD}{ANSI_SOFT_RED}{text}{ANSI_RESET}"


def _style_tool_line(text: str, color: str = ANSI_WHITE, *, bold: bool = False, dim: bool = False) -> str:
    if not _supports_tty_control():
        return text
    style = ""
    if bold:
        style += ANSI_BOLD
    if dim:
        style += ANSI_DIM
    style += color
    return f"{style}{text}{ANSI_RESET}"


def _cleanup_legacy_runtime(config: ProjectLingConfig) -> None:
    for name in LEGACY_RUNTIME_FILES:
        target = config.runtime_dir / name
        try:
            if target.is_file():
                target.unlink()
        except OSError:
            continue
    for name in LEGACY_ROOT_RUNTIME_FILES:
        target = config.root_dir / name
        try:
            if target.is_file() and target.parent != config.runtime_dir:
                target.unlink()
        except OSError:
            continue
    try:
        env_text = config.env_file_path.read_text(encoding="utf-8")
    except OSError:
        env_text = ""
    if re.search(r"(?m)^DEEPSEEK_(MODEL|ENABLE_THINKING)=", env_text):
        save_env_config({"DEEPSEEK_MODEL": None, "DEEPSEEK_ENABLE_THINKING": None}, path=config.env_file_path)


def _normalize_status_label(text: str | None, fallback: str) -> str:
    cleaned = str(text or "").strip().strip(". ")
    if not cleaned:
        return fallback
    return " ".join(part.capitalize() for part in cleaned.replace("_", " ").split())


def _indent_block(text: str) -> str:
    body = (text or "").strip() or "我没有得到有效回复。"
    lines = [f"  {line.rstrip()}" if line.strip() else "" for line in body.splitlines()]
    return "\n".join(lines) if lines else "  我没有得到有效回复。"


def _collapse_blank_lines(text: str) -> str:
    while "\n\n\n" in text:
        text = text.replace("\n\n\n", "\n\n")
    return text


def _repair_unordered_list_markers_in_line(line: str) -> str:
    if not re.match(r"^\s*[-+*•]\s+", line):
        return line
    markers = list(re.finditer(r"[-+*•]\s+", line))
    if len(markers) <= 1:
        return line

    indent = re.match(r"^\s*", line).group(0)
    out: list[str] = []
    last = 0
    for marker in markers[1:]:
        out.append(line[last:marker.start()].rstrip())
        out.append("\n")
        out.append(indent)
        last = marker.start()
    out.append(line[last:])
    return "".join(out)


def _repair_ordered_list_markers_in_line(line: str) -> str:
    markers = list(re.finditer(r"(?<!\d)(\d{1,3})\.\s+", line))
    if len(markers) <= 1:
        return line

    sequence_start = None
    for index, marker in enumerate(markers):
        number = int(marker.group(1))
        if number == 1 or re.match(r"^\s*$", line[: marker.start()]):
            if index + 1 < len(markers) and int(markers[index + 1].group(1)) == number + 1:
                sequence_start = index
                break
    if sequence_start is None:
        return line

    indent = re.match(r"^\s*", line).group(0)
    out: list[str] = []
    last = 0
    expected = int(markers[sequence_start].group(1))
    for marker in markers[sequence_start:]:
        number = int(marker.group(1))
        if number != expected:
            expected = number + 1
            continue
        prefix = line[last:marker.start()].rstrip()
        if prefix:
            out.append(prefix)
            out.append("\n")
            out.append(indent)
        elif last == 0 and marker.start() > 0:
            out.append(line[last:marker.start()])
        elif out and not out[-1].endswith("\n"):
            out.append("\n")
            out.append(indent)
        last = marker.start()
        expected = number + 1
    out.append(line[last:])
    return "".join(out)


def _repair_collapsed_table_rows_in_line(line: str) -> str:
    if "||" not in line or "|" not in line:
        return line
    repaired = re.sub(r"\|\s*\|", "|\n|", line)
    parts = repaired.splitlines()
    if len(parts) < 2:
        return line
    has_separator = any(_split_table_separator_candidate(part) for part in parts)
    return repaired if has_separator else line


def _split_table_separator_candidate(line: str) -> bool:
    cells = [cell.strip().replace(" ", "") for cell in line.strip().strip("|").split("|")]
    return bool(cells) and all(re.fullmatch(r":?-{3,}:?", cell or "") for cell in cells)


def _repair_markdown_list_boundaries(text: str) -> str:
    repaired = re.sub(r"([^\s\n])(\s*-\s+\*\*)", r"\1\n\2", str(text or ""))
    repaired_lines: list[str] = []
    fence_marker: str | None = None
    for line in repaired.split("\n"):
        stripped = line.lstrip()
        fence_match = re.match(r"^(```+|~~~+)", stripped)
        if fence_match:
            marker = fence_match.group(1)[:3]
            fence_marker = None if fence_marker == marker else marker
            repaired_lines.append(line)
            continue

        if fence_marker is not None:
            repaired_lines.append(line)
            continue

        line = _repair_collapsed_table_rows_in_line(line)
        for part in line.split("\n"):
            part = _repair_unordered_list_markers_in_line(part)
            part = _repair_ordered_list_markers_in_line(part)
            repaired_lines.extend(part.split("\n"))
    return "\n".join(repaired_lines)


def _restore_escaped_markdown(text: str) -> str:
    restored = text
    for raw, token in ESCAPED_MARKDOWN_TOKENS.items():
        restored = restored.replace(token, raw[1:])
    return restored


def _tokenize_ansi(text: str) -> list[str]:
    parts: list[str] = []
    last = 0
    for match in ANSI_PATTERN.finditer(text):
        if match.start() > last:
            parts.append(text[last:match.start()])
        parts.append(match.group(0))
        last = match.end()
    if last < len(text):
        parts.append(text[last:])
    return parts


def _strip_ansi(text: str) -> str:
    return ANSI_PATTERN.sub("", text)


def _display_width(text: str) -> int:
    width = 0
    for char in _strip_ansi(text):
        if unicodedata.combining(char):
            continue
        width += 2 if unicodedata.east_asian_width(char) in {"W", "F"} else 1
    return width


def _truncate_display(text: str, max_width: int) -> str:
    if max_width <= 0:
        return ""
    out: list[str] = []
    used = 0
    for char in text:
        char_width = 0 if unicodedata.combining(char) else (2 if unicodedata.east_asian_width(char) in {"W", "F"} else 1)
        if used + char_width > max_width:
            break
        out.append(char)
        used += char_width
    return "".join(out)


def _truncate_display_ellipsis(text: str, max_width: int) -> str:
    if _display_width(text) <= max_width:
        return text
    if max_width <= 1:
        return _truncate_display(text, max_width)
    return f"{_truncate_display(text, max_width - 1)}…"


def _middle_truncate_display(text: str, max_width: int, *, head_ratio: float = 0.55) -> str:
    if max_width <= 0:
        return ""
    if _display_width(text) <= max_width:
        return text
    ellipsis = "…"
    ellipsis_width = _display_width(ellipsis)
    if max_width <= ellipsis_width:
        return ellipsis[:max_width]
    budget = max_width - ellipsis_width
    head_width = max(1, int(budget * head_ratio))
    tail_width = max(1, budget - head_width)
    while head_width + tail_width > budget:
        tail_width = max(1, tail_width - 1)
    while head_width + tail_width < budget:
        head_width += 1
    head = _truncate_display(text, head_width)
    tail = _truncate_display(text[::-1], tail_width)[::-1]
    return f"{head}{ellipsis}{tail}"


def _char_display_width(char: str) -> int:
    if unicodedata.combining(char):
        return 0
    return 2 if unicodedata.east_asian_width(char) in {"W", "F"} else 1


def _wrap_ansi_display(text: str, max_width: int) -> list[str]:
    if max_width <= 0:
        return [""]

    lines: list[str] = []
    current: list[str] = []
    used = 0

    for token in _tokenize_ansi(text):
        if not token:
            continue
        if ANSI_PATTERN.fullmatch(token):
            current.append(token)
            continue

        for char in token:
            if char == "\n":
                lines.append("".join(current).rstrip())
                current = []
                used = 0
                continue

            char_width = _char_display_width(char)
            if used > 0 and used + char_width > max_width:
                lines.append("".join(current).rstrip())
                current = []
                used = 0

            current.append(char)
            used += char_width

    if current or not lines:
        lines.append("".join(current).rstrip())

    return lines


def _pad_display(text: str, width: int) -> str:
    plain = _truncate_display(text, width)
    return plain + (" " * max(0, width - _display_width(plain)))


def _terminal_render_width(default: int = 80) -> int:
    return max(24, shutil.get_terminal_size((default, 24)).columns - 2)


def _normalize_reference_key(key: str) -> str:
    return " ".join(key.strip().lower().split())


def _footnote_marker(label: str) -> str:
    if label.isdigit():
        return label.translate(str.maketrans("0123456789", "⁰¹²³⁴⁵⁶⁷⁸⁹"))
    return f"〔{label.strip()}〕"


def _style_span(text: str, style: str, *, base_style: str = "") -> str:
    if not text:
        return ""
    return f"{style}{text}{ANSI_RESET}{base_style}"


def _render_inline_markdown(text: str, *, base_style: str = "", tty: bool) -> str:
    if not tty or not text:
        return text

    rendered = unescape(text)
    for raw, token in ESCAPED_MARKDOWN_TOKENS.items():
        rendered = rendered.replace(raw, token)

    placeholder_index = 0
    placeholders: dict[str, str] = {}

    def stash(value: str) -> str:
        nonlocal placeholder_index
        key = f"\uFFFDU{placeholder_index}\uFFFE"
        placeholder_index += 1
        placeholders[key] = value
        return key

    def image_repl(match: re.Match[str]) -> str:
        alt = match.group(1).strip() or "image"
        target = match.group(2).strip()
        if " " in target:
            target = target.split(" ", 1)[0]
        return stash(
            _style_span("▣ ", f"{ANSI_BOLD}{ANSI_MAGENTA}", base_style=base_style)
            + _style_span(alt, f"{ANSI_BOLD}{ANSI_WHITE}", base_style=base_style)
            + _style_span(f" <{target}>", f"{ANSI_DIM}{ANSI_WHITE}", base_style=base_style)
        )

    def link_repl(match: re.Match[str]) -> str:
        label = match.group(1).strip()
        target = match.group(2).strip()
        if not label or not target:
            return match.group(0)
        if " " in target:
            target = target.split(" ", 1)[0]
        return stash(
            _style_span(label, f"{ANSI_UNDERLINE}{ANSI_LINK}", base_style=base_style)
            + _style_span(f" <{target}>", f"{ANSI_DIM}{ANSI_WHITE}", base_style=base_style)
        )

    def auto_link_repl(match: re.Match[str]) -> str:
        target = match.group(1).strip()
        return stash(_style_span(target, f"{ANSI_UNDERLINE}{ANSI_LINK}", base_style=base_style))

    rendered = MARKDOWN_CODE_RE.sub(
        lambda match: stash(
            _style_span(match.group(1), f"{ANSI_BOLD}{ANSI_GOLD}", base_style=base_style)
        ),
        rendered,
    )
    rendered = MARKDOWN_BARE_URL_RE.sub(
        lambda match: stash(
            _style_span(match.group(1), f"{ANSI_UNDERLINE}{ANSI_LINK}", base_style=base_style)
        ),
        rendered,
    )
    rendered = MARKDOWN_AUTO_LINK_RE.sub(auto_link_repl, rendered)
    rendered = MARKDOWN_IMAGE_RE.sub(image_repl, rendered)
    rendered = MARKDOWN_LINK_RE.sub(link_repl, rendered)
    rendered = MARKDOWN_HIGHLIGHT_RE.sub(
        lambda match: _style_span(match.group(1), f"{ANSI_BOLD}{ANSI_GOLD}", base_style=base_style),
        rendered,
    )
    rendered = MARKDOWN_BOLD_ITALIC_RE.sub(
        lambda match: _style_span(match.group(2), f"{ANSI_BOLD}{ANSI_ITALIC}", base_style=base_style),
        rendered,
    )
    rendered = MARKDOWN_BOLD_RE.sub(
        lambda match: _style_span(match.group(2), ANSI_BOLD, base_style=base_style),
        rendered,
    )
    rendered = MARKDOWN_ITALIC_STAR_RE.sub(
        lambda match: _style_span(match.group(1), ANSI_ITALIC, base_style=base_style),
        rendered,
    )
    rendered = MARKDOWN_ITALIC_UNDERSCORE_RE.sub(
        lambda match: _style_span(match.group(1), ANSI_ITALIC, base_style=base_style),
        rendered,
    )
    rendered = MARKDOWN_STRIKE_RE.sub(
        lambda match: _style_span(match.group(1), ANSI_DIM, base_style=base_style),
        rendered,
    )

    rendered = MARKDOWN_HTML_ANCHOR_RE.sub(
        lambda match: stash(
            _style_span(match.group(2).strip() or match.group(1).strip(), f"{ANSI_UNDERLINE}{ANSI_LINK}", base_style=base_style)
            + _style_span(f" <{match.group(1).strip()}>", f"{ANSI_DIM}{ANSI_WHITE}", base_style=base_style)
        ),
        rendered,
    )
    rendered = MARKDOWN_HTML_IMG_RE.sub(
        lambda match: stash(
            _style_span("▣ ", f"{ANSI_BOLD}{ANSI_MAGENTA}", base_style=base_style)
            + _style_span(
                (match.group(2) or match.group(3) or "image").strip() or "image",
                f"{ANSI_BOLD}{ANSI_WHITE}",
                base_style=base_style,
            )
            + _style_span(
                f" <{(match.group(1) or match.group(4) or '').strip()}>",
                f"{ANSI_DIM}{ANSI_WHITE}",
                base_style=base_style,
            )
        ),
        rendered,
    )
    rendered = MARKDOWN_HTML_STRONG_RE.sub(
        lambda match: stash(_style_span(match.group(1), ANSI_BOLD, base_style=base_style)),
        rendered,
    )
    rendered = MARKDOWN_HTML_EM_RE.sub(
        lambda match: stash(_style_span(match.group(1), ANSI_ITALIC, base_style=base_style)),
        rendered,
    )
    rendered = MARKDOWN_HTML_DEL_RE.sub(
        lambda match: stash(_style_span(match.group(1), ANSI_DIM, base_style=base_style)),
        rendered,
    )
    rendered = MARKDOWN_HTML_CODE_RE.sub(
        lambda match: stash(
            _style_span(match.group(1), f"{ANSI_BOLD}{ANSI_GOLD}", base_style=base_style)
        ),
        rendered,
    )
    rendered = MARKDOWN_HTML_MARK_RE.sub(
        lambda match: stash(
            _style_span(match.group(1), f"{ANSI_BOLD}{ANSI_GOLD}", base_style=base_style)
        ),
        rendered,
    )
    rendered = MARKDOWN_HTML_UNDERLINE_RE.sub(
        lambda match: stash(_style_span(match.group(1), ANSI_UNDERLINE, base_style=base_style)),
        rendered,
    )
    rendered = MARKDOWN_HTML_STRIP_RE.sub("", rendered)
    rendered = _restore_escaped_markdown(rendered)
    for key, value in placeholders.items():
        rendered = rendered.replace(key, value)

    if base_style:
        return f"{base_style}{rendered}{ANSI_RESET}"
    return rendered


class MarkdownAnsiRenderer:
    def __init__(self, *, tty: bool) -> None:
        self.tty = tty
        self.render_width = _terminal_render_width()
        self._reset_render_state()

    def _reset_render_state(self) -> None:
        self.code_fence_marker = None
        self.reference_links = {}
        self.footnotes = {}
        self.referenced_footnotes = []
        self.emitted_footnotes: set[str] = set()

    def _resolve_reference_link(self, label: str, reference: str, *, image: bool) -> str:
        ref_key = _normalize_reference_key(reference or label)
        target = self.reference_links.get(ref_key, "").strip()
        if not target:
            if image:
                return f"▣ {label.strip() or 'image'}"
            return label.strip() or reference.strip()
        if image:
            return f"![{label}]({target})"
        return f"[{label}]({target})"

    def _preprocess_reference_style(self, text: str) -> str:
        def repl(match: re.Match[str]) -> str:
            if match.group(1) is not None:
                return self._resolve_reference_link(match.group(1), match.group(2), image=True)
            return self._resolve_reference_link(match.group(3), match.group(4), image=False)

        return MARKDOWN_REFERENCE_LINK_RE.sub(repl, text)

    def _preprocess_footnotes(self, text: str) -> str:
        def repl(match: re.Match[str]) -> str:
            label = match.group(1).strip()
            key = _normalize_reference_key(label)
            if key and key not in self.referenced_footnotes:
                self.referenced_footnotes.append(key)
            return _style_span(_footnote_marker(label), f"{ANSI_BOLD}{ANSI_MAGENTA}")

        return MARKDOWN_FOOTNOTE_REF_RE.sub(repl, text)

    def _preprocess_inline_text(self, text: str) -> str:
        return self._preprocess_footnotes(self._preprocess_reference_style(text))

    def _extract_reference_blocks(self, lines: list[str]) -> list[str]:
        cleaned_lines: list[str] = []
        index = 0
        while index < len(lines):
            line = lines[index]
            ref_match = MARKDOWN_REFERENCE_DEF_RE.match(line)
            if ref_match:
                self.reference_links[_normalize_reference_key(ref_match.group(1))] = ref_match.group(2).strip()
                index += 1
                continue

            footnote_match = MARKDOWN_FOOTNOTE_DEF_RE.match(line)
            if footnote_match:
                key = _normalize_reference_key(footnote_match.group(1))
                parts = [footnote_match.group(2).rstrip()]
                index += 1
                while index < len(lines):
                    continuation = lines[index]
                    if continuation.startswith("    "):
                        parts.append(continuation[4:].rstrip())
                        index += 1
                        continue
                    if continuation.startswith("\t"):
                        parts.append(continuation[1:].rstrip())
                        index += 1
                        continue
                    if (
                        not continuation.strip()
                        and index + 1 < len(lines)
                        and (lines[index + 1].startswith("    ") or lines[index + 1].startswith("\t"))
                    ):
                        parts.append("")
                        index += 1
                        continue
                    break
                self.footnotes[key] = "\n".join(parts).strip()
                continue

            cleaned_lines.append(line)
            index += 1
        return cleaned_lines

    @staticmethod
    def _split_table_row(line: str) -> list[str]:
        text = line.strip()
        if text.startswith("|"):
            text = text[1:]
        if text.endswith("|"):
            text = text[:-1]
        cells = re.split(r"(?<!\\)\|", text)
        return [unescape(cell.replace(r"\|", "|").strip()) for cell in cells]

    def _is_table_separator(self, line: str) -> bool:
        cells = self._split_table_row(line)
        if not cells:
            return False
        return all(MARKDOWN_TABLE_ALIGN_RE.fullmatch(cell.replace(" ", "")) for cell in cells)

    def _render_table_lines(self, rows: list[list[str]]) -> list[str]:
        headers = rows[0]
        body_rows = rows[1:]
        col_count = max(len(row) for row in rows)
        headers = headers + [""] * (col_count - len(headers))
        body_rows = [row + [""] * (col_count - len(row)) for row in body_rows]

        if self.render_width < 72 or col_count > 3:
            rendered: list[str] = []
            if not body_rows:
                rendered.append(
                    _style_span("▥ " + " · ".join(headers), f"{ANSI_BOLD}{ANSI_CYAN}")
                )
                return rendered
            for row_index, row in enumerate(body_rows, start=1):
                if row_index > 1:
                    rendered.append("")
                rendered.append(_style_span(f"▥ Row {row_index}", f"{ANSI_BOLD}{ANSI_CYAN}"))
                for header, value in zip(headers, row):
                    label = header.strip() or f"Field {len(rendered)}"
                    rendered.append(
                        f"{_style_span(f'{label}: ', f'{ANSI_BOLD}{ANSI_CYAN}')}"
                        f"{_render_inline_markdown(value or '—', tty=True)}"
                    )
            return rendered

        max_cell_width = max(8, (self.render_width - (col_count - 1) * 3 - 4) // col_count)
        widths: list[int] = []
        for index in range(col_count):
            candidates = [headers[index], *[row[index] for row in body_rows]]
            widths.append(
                min(max(_display_width(cell) for cell in candidates if cell is not None), max_cell_width)
            )

        def render_table_row(cells: list[str], *, header: bool) -> str:
            parts: list[str] = []
            for idx, cell in enumerate(cells):
                plain = _truncate_display_ellipsis(cell or "—", widths[idx])
                styled = _render_inline_markdown(
                    self._preprocess_inline_text(plain),
                    base_style=f"{ANSI_BOLD}{ANSI_CYAN}" if header else "",
                    tty=True,
                )
                parts.append(f" {styled}{' ' * max(0, widths[idx] - _display_width(plain))} ")
            return "│" + "│".join(parts) + "│"

        top = "┌" + "┬".join("─" * (width + 2) for width in widths) + "┐"
        mid = "├" + "┼".join("─" * (width + 2) for width in widths) + "┤"
        bottom = "└" + "┴".join("─" * (width + 2) for width in widths) + "┘"
        rendered = [top, render_table_row(headers, header=True), mid]
        rendered.extend(render_table_row(row, header=False) for row in body_rows)
        rendered.append(bottom)
        return rendered

    def _render_line(self, line: str) -> str:
        if not self.tty:
            return line

        stripped = MARKDOWN_HTML_BREAK_RE.sub("", line).strip()
        preprocessed_line = self._preprocess_inline_text(line)

        fence_match = re.match(r"^\s*(```+|~~~+)\s*(.*)$", stripped)
        if fence_match:
            marker = fence_match.group(1)[:3]
            if self.code_fence_marker:
                self.code_fence_marker = None
                return ""
            self.code_fence_marker = marker
            language = fence_match.group(2).strip()
            if not language:
                return _style_span("▌ code", f"{ANSI_DIM}{ANSI_MAGENTA}")
            return _style_span(f"▌ {language}", f"{ANSI_DIM}{ANSI_MAGENTA}")

        if self.code_fence_marker:
            return _render_inline_markdown(preprocessed_line, base_style=f"{ANSI_BOLD}{ANSI_GOLD}", tty=True)

        if not stripped:
            return ""

        heading = re.match(r"^(#{1,6})\s+(.*)$", preprocessed_line)
        if heading:
            level = len(heading.group(1))
            title = heading.group(2).strip()
            prefix = "■ " if level <= 2 else "▸ "
            base = f"{ANSI_BOLD}{ANSI_CYAN}" if level <= 2 else f"{ANSI_BOLD}{ANSI_WHITE}"
            return _render_inline_markdown(f"{prefix}{title}", base_style=base, tty=True)

        if re.fullmatch(r"\s*(?:[-*_]\s*){3,}\s*", line):
            return _style_span("─" * min(32, max(12, self.render_width)), f"{ANSI_DIM}{ANSI_RULE}")

        quote = re.match(r"^(\s*)((?:>\s*)+)(.*)$", preprocessed_line)
        if quote:
            indent = quote.group(1)
            depth = quote.group(2).count(">")
            body = _render_inline_markdown(quote.group(3), base_style=f"{ANSI_DIM}{ANSI_QUOTE}", tty=True)
            return f"{indent}{_style_span('│ ' * max(1, depth), f'{ANSI_BOLD}{ANSI_QUOTE}')}{body}"

        task = re.match(r"^(\s*)[-+*]\s+\[([ xX])\]\s+(.*)$", preprocessed_line)
        if task:
            indent = task.group(1)
            level = max(0, len(indent.expandtabs(2)) // 2)
            marker = "☑ " if task.group(2).lower() == "x" else "☐ "
            body = _render_inline_markdown(task.group(3), tty=True)
            bullet_style = f"{ANSI_BOLD}{ANSI_CYAN}" if level <= 1 else f"{ANSI_BOLD}{ANSI_WHITE}"
            return f"{indent}{_style_span(marker, bullet_style)}{body}"

        unordered = re.match(r"^(\s*)[-+*]\s+(.*)$", preprocessed_line)
        if unordered:
            indent = unordered.group(1)
            level = max(0, len(indent.expandtabs(2)) // 2)
            bullet = "• " if level <= 1 else "◦ " if level == 2 else "▪ "
            body = _render_inline_markdown(unordered.group(2), tty=True)
            bullet_style = f"{ANSI_BOLD}{ANSI_CYAN}" if level <= 1 else f"{ANSI_BOLD}{ANSI_WHITE}"
            return f"{indent}{_style_span(bullet, bullet_style)}{body}"

        ordered_task = re.match(r"^(\s*)(\d+)\.\s+\[([ xX])\]\s+(.*)$", preprocessed_line)
        if ordered_task:
            indent = ordered_task.group(1)
            number = ordered_task.group(2)
            marker = "☑ " if ordered_task.group(3).lower() == "x" else "☐ "
            body = _render_inline_markdown(ordered_task.group(4), tty=True)
            return f"{indent}{_style_span(f'{number}. ', f'{ANSI_BOLD}{ANSI_CYAN}')}{_style_span(marker, f'{ANSI_BOLD}{ANSI_CYAN}')}{body}"

        ordered = re.match(r"^(\s*)(\d+)\.\s+(.*)$", preprocessed_line)
        if ordered:
            indent = ordered.group(1)
            number = ordered.group(2)
            body = _render_inline_markdown(ordered.group(3), tty=True)
            return f"{indent}{_style_span(f'{number}. ', f'{ANSI_BOLD}{ANSI_CYAN}')}{body}"

        return _render_inline_markdown(preprocessed_line, tty=True)

    def _render_block(self, text: str) -> str:
        if not text:
            return ""
        normalized = MARKDOWN_HTML_BREAK_RE.sub("\n", text)
        chunks = normalized.splitlines(keepends=True)
        lines = [chunk[:-1] if chunk.endswith("\n") else chunk for chunk in chunks]
        lines = self._extract_reference_blocks(lines)
        rendered_lines: list[str] = []
        index = 0
        while index < len(lines):
            line = lines[index]
            next_line = lines[index + 1] if index + 1 < len(lines) else None

            if (
                line.strip()
                and next_line is not None
                and MARKDOWN_SETEXT_H1_RE.fullmatch(next_line.strip())
            ):
                rendered_lines.append(
                    _render_inline_markdown(self._preprocess_inline_text(f"■ {line.strip()}"), base_style=f"{ANSI_BOLD}{ANSI_CYAN}", tty=True)
                )
                index += 2
                continue

            if (
                line.strip()
                and next_line is not None
                and MARKDOWN_SETEXT_H2_RE.fullmatch(next_line.strip())
                and "|" not in line
            ):
                rendered_lines.append(
                    _render_inline_markdown(self._preprocess_inline_text(f"▸ {line.strip()}"), base_style=f"{ANSI_BOLD}{ANSI_WHITE}", tty=True)
                )
                index += 2
                continue

            if (
                "|" in line
                and next_line is not None
                and self._is_table_separator(next_line)
            ):
                table_rows = [self._split_table_row(line)]
                index += 2
                while index < len(lines):
                    current = lines[index]
                    if not current.strip() or "|" not in current:
                        break
                    table_rows.append(self._split_table_row(current))
                    index += 1
                rendered_lines.extend(self._render_table_lines(table_rows))
                continue

            rendered_lines.append(self._render_line(line))
            index += 1

        visible_footnotes = [
            key
            for key in self.referenced_footnotes
            if key in self.footnotes and key not in self.emitted_footnotes
        ]
        if visible_footnotes:
            if rendered_lines and rendered_lines[-1] != "":
                rendered_lines.append("")
            rendered_lines.append(_style_span("Footnotes", f"{ANSI_BOLD}{ANSI_CYAN}"))
            for key in visible_footnotes:
                marker = _footnote_marker(key)
                body = self.footnotes[key].replace("\n", " / ")
                rendered_lines.append(
                    f"{_style_span(marker + ' ', f'{ANSI_BOLD}{ANSI_MAGENTA}')}"
                    f"{_render_inline_markdown(self._preprocess_inline_text(body), tty=True)}"
                )
                self.emitted_footnotes.add(key)

        return "\n".join(rendered_lines)

    def render(self, text: str) -> str:
        return self._render_block(_repair_markdown_list_boundaries(text))


def _find_safe_stream_split(text: str, *, force: bool) -> int:
    if not text:
        return 0
    if force:
        return len(text)

    safe_index = 0
    code_fence_open = False
    inline_code_open = False
    bold_open = False
    strike_open = False
    bracket_depth = 0
    paren_depth = 0
    index = 0
    length = len(text)

    while index < length:
        if text.startswith("```", index) or text.startswith("~~~", index):
            code_fence_open = not code_fence_open
            index += 3
            continue

        char = text[index]
        if char == "\\":
            index += 2
            continue

        if not code_fence_open and char == "`":
            inline_code_open = not inline_code_open
            index += 1
            continue

        if not code_fence_open and not inline_code_open and text.startswith("**", index):
            bold_open = not bold_open
            index += 2
            continue

        if not code_fence_open and not inline_code_open and text.startswith("~~", index):
            strike_open = not strike_open
            index += 2
            continue

        if not code_fence_open and not inline_code_open:
            if char == "[":
                bracket_depth += 1
            elif char == "]":
                bracket_depth = max(0, bracket_depth - 1)
            elif char == "(":
                paren_depth += 1
            elif char == ")":
                paren_depth = max(0, paren_depth - 1)

        if char == "\n":
            if not code_fence_open and not inline_code_open and not bold_open and not strike_open and bracket_depth == 0 and paren_depth == 0:
                safe_index = index + 1
        elif not code_fence_open and not inline_code_open and not bold_open and not strike_open and bracket_depth == 0 and paren_depth == 0:
            if char in STREAM_SENTENCE_ENDINGS:
                safe_index = index + 1
            elif char in "，、," and index + 1 >= 24:
                safe_index = index + 1
            elif char.isspace() and index + 1 >= 40:
                safe_index = index + 1
            elif index + 1 == length and length >= 64:
                safe_index = index + 1

        index += 1

    if safe_index > 0:
        return safe_index

    if (
        not code_fence_open
        and not inline_code_open
        and not bold_open
        and not strike_open
        and bracket_depth == 0
        and paren_depth == 0
        and length >= 80
    ):
        for index in range(length - 1, max(0, length - 32), -1):
            if text[index].isspace():
                return index + 1
        return max(1, length - 16)

    return 0


def _stream_has_block_markdown(text: str) -> bool:
    return bool(MARKDOWN_STREAM_BLOCK_RE.search(text or ""))


def _stream_fence_is_open(text: str) -> bool:
    marker: str | None = None
    for raw_line in str(text or "").splitlines():
        stripped = raw_line.strip()
        match = re.match(r"^(```+|~~~+)", stripped)
        if not match:
            continue
        current = match.group(1)[:3]
        if marker == current:
            marker = None
        elif marker is None:
            marker = current
    return marker is not None


def _markdown_stream_flush_index(text: str) -> int:
    if not text:
        return 0
    if not _stream_has_block_markdown(text):
        return len(text)

    best = 0
    search_from = 0
    while True:
        boundary = text.find("\n\n", search_from)
        if boundary < 0:
            break
        end = boundary + 2
        if not _stream_fence_is_open(text[:end]):
            best = end
        search_from = end
    return best


class StreamingTextSanitizer:
    def __init__(self) -> None:
        self.pending = ""
        self.leading_stage_checked = False
        self.emitted_visible = False

    def _strip_leading_stage_direction(self, *, force: bool) -> None:
        if self.leading_stage_checked or self.emitted_visible:
            return

        stripped = self.pending.lstrip()
        leading_ws = self.pending[: len(self.pending) - len(stripped)]
        if not stripped:
            self.pending = leading_ws
            return

        opener = stripped[0]
        if opener not in {"（", "("}:
            self.leading_stage_checked = True
            return

        closer = "）" if opener == "（" else ")"
        closing_index = stripped.find(closer)
        if closing_index < 0:
            if not force and len(stripped) <= 48 and "\n" not in stripped:
                return
            self.leading_stage_checked = True
            return

        candidate = stripped[1:closing_index]
        if 0 < len(candidate) <= 24 and "\n" not in candidate:
            remainder = stripped[closing_index + 1 :].lstrip("，,。.!！？、；;:： \t")
            self.pending = leading_ws + remainder
        self.leading_stage_checked = True

    def _normalize_pending(self, *, force: bool) -> None:
        self._strip_leading_stage_direction(force=force)
        self.pending = self.pending.replace("\r", "")
        self.pending = _collapse_blank_lines(self.pending)

    def push(self, text: str) -> str:
        if not text:
            return ""
        self.pending += text
        self._normalize_pending(force=False)

        split_index = _find_safe_stream_split(self.pending, force=False)
        if split_index <= 0:
            return ""

        ready = self.pending[:split_index]
        self.pending = self.pending[split_index:]
        if ready.strip():
            self.emitted_visible = True
        return ready

    def finish(self) -> str:
        self._normalize_pending(force=True)
        ready = self.pending
        self.pending = ""
        if ready.strip():
            self.emitted_visible = True
        return ready


def _prompt_line(prompt: str) -> str:
    try:
        return input(prompt)
    except (EOFError, KeyboardInterrupt):
        print("")
        return ""


def _render_assistant_block(
    text: str,
    role: LauncherRole | None = None,
    *,
    persona_bundle: PersonaBundle | None = None,
) -> str:
    if role is None:
        return f"\n{_style_heading('● PROJECT凌', 'deepseek')}\n{_indent_block(text)}\n"
    return f"\n{_format_role_heading(role, persona_bundle)}\n\n{_indent_block(text)}\n"


def _pick_model_interactive(current_model: str) -> str | None:
    print("")
    for index, (model_name, desc) in enumerate(MODEL_CHOICES, start=1):
        marker = "  当前" if model_name == current_model else ""
        print(f"{index}. {model_name}  {desc}{marker}")
    print("3. 自定义输入")
    picked = _prompt_line("选择模型 > ").strip()
    if not picked:
        return None
    if picked in {"1", "2"}:
        return MODEL_CHOICES[int(picked) - 1][0]
    if picked == "3":
        custom = _prompt_line("输入模型名 > ").strip()
        return custom or None
    print("无效输入，保持原样。")
    return None


def _save_config_value(config: ProjectLingConfig, updates: dict[str, str | None]) -> ProjectLingConfig:
    save_env_config(updates, path=config.env_file_path)
    return load_config()


def _collab_mode_input_value(raw: str | None) -> str | None:
    value = str(raw or "").strip().lower()
    if not value:
        return None
    value = COLLAB_MODE_ALIASES.get(value, value)
    if value in COLLAB_MODE_ORDER:
        return value
    return None


def _collab_mode_next_value(current: str | None) -> str:
    normalized = _collab_mode_value(current)
    try:
        current_index = COLLAB_MODE_ORDER.index(normalized)
    except ValueError:
        current_index = 0
    return COLLAB_MODE_ORDER[(current_index + 1) % len(COLLAB_MODE_ORDER)]


def _collab_mode_models(mode: str) -> tuple[str, str]:
    normalized = _collab_mode_value(mode)
    planner = "deepseek-chat" if normalized == "rapid" else "deepseek-reasoner"
    executor = "deepseek-reasoner" if normalized == "precise" else "deepseek-chat"
    return planner, executor


def _collab_mode_detail(mode: str) -> str:
    normalized = _collab_mode_value(mode)
    label = next((name for value, name, _desc in COLLAB_MODE_CHOICES if value == normalized), normalized)
    desc = next((desc for value, _name, desc in COLLAB_MODE_CHOICES if value == normalized), "")
    return f"{label} · {desc}".strip(" ·")


def _render_model_mode_menu(current: ProjectLingConfig) -> None:
    current_mode = _collab_mode_value(current.collab_mode)
    print("")
    print("协作模式")
    print(f"当前：{_collab_mode_detail(current_mode)}")
    for index, (mode, label, desc) in enumerate(COLLAB_MODE_CHOICES, start=1):
        marker = "  当前" if mode == current_mode else ""
        print(f"{index}. {label}  {desc}{marker}")
    print("0. 返回")


def _apply_collab_mode(config: ProjectLingConfig, raw_mode: str | None) -> int:
    raw_value = str(raw_mode or "").strip().lower()
    current_mode = _collab_mode_value(config.collab_mode)
    if raw_value in COLLAB_MODE_STATUS_ALIASES:
        print(f"当前协作模式：{_collab_mode_detail(current_mode)}")
        return 0
    if raw_value in COLLAB_MODE_CYCLE_ALIASES:
        mode = _collab_mode_next_value(current_mode)
    else:
        mode = _collab_mode_input_value(raw_mode)
    if mode is None:
        print("无效模式。可用：快速 / 标准 / 精确，输入 1 / 2 / 3，或输入 next 轮换。")
        return 1
    _save_config_value(config, {"PROJECTLING_COLLAB_MODE": mode})
    if mode == current_mode:
        print(f"协作模式保持：{_collab_mode_detail(mode)}")
    else:
        print(f"协作模式已更新：{_collab_mode_detail(mode)}")
    return 0


def _run_model_mode_ui(mode_arg: str = "") -> int:
    if str(mode_arg or "").strip():
        return _apply_collab_mode(load_config(), mode_arg)

    while True:
        current = load_config()
        _render_model_mode_menu(current)
        choice = _prompt_line("> ").strip()
        if choice == "0" or not choice:
            return 0
        if _apply_collab_mode(current, choice) == 0:
            return 0


def _prompt_optional_text(prompt: str) -> str | None:
    value = _prompt_line(prompt)
    if not value.strip():
        return None
    return value.strip()


def _prompt_float(prompt: str, *, min_value: float, max_value: float) -> float | None:
    raw = _prompt_line(prompt).strip()
    if not raw:
        return None
    try:
        value = float(raw)
    except ValueError:
        print("输入无效，保持原样。")
        return None
    if value < min_value or value > max_value:
        print(f"输入超出范围，需要在 {min_value:g} - {max_value:g} 之间。")
        return None
    return value


def _prompt_int(prompt: str, *, min_value: int, allow_empty_clear: bool = False) -> int | None | str:
    raw = _prompt_line(prompt)
    if not raw.strip():
        return "" if allow_empty_clear else None
    try:
        value = int(raw.strip())
    except ValueError:
        print("输入无效，保持原样。")
        return None
    if value < min_value:
        print(f"输入无效，需要大于等于 {min_value}。")
        return None
    return value


def _bool_label(value: bool) -> str:
    return "ON" if value else "OFF"


def _key_status(value: str | None) -> str:
    return "已设置" if value else "未设置"


def _tool_round_limit_label(value: int) -> str:
    rounds = max(0, int(value or 0))
    return "UNLIMITED" if rounds == 0 else str(rounds)


def _render_command_help() -> None:
    print("")
    print("PROJECT凌")
    print("  /mode      轮换协作模式；/mode 2 可直接切标准模式")
    print("  /model     打开协作模式菜单（兼容旧入口）")
    print("  /send      直接发送消息给辅导位")
    print("  /settings  打开设置")
    print("  /codexurl  打开 codexurl")
    print("  /help      显示帮助")


def _render_settings_root(current: ProjectLingConfig) -> None:
    print("")
    print("PROJECT凌设置")
    print("1. API")
    print("2. Persona")
    print("3. System")
    print("0. 完成设置")


def _render_api_settings(current: ProjectLingConfig) -> None:
    max_tokens_text = str(current.max_tokens) if current.max_tokens is not None else "自动"
    websearch_status = f"S:{_key_status(current.websearch_summary_key)} / W:{_key_status(current.websearch_web_key)}"
    print("")
    print("API 设置")
    print(f"1. API Key       [{_key_status(current.api_key)}]")
    print(f"2. Base URL      [{current.base_url}]")
    print(f"3. SSE 流式      [{_bool_label(current.enable_sse)}]")
    print(f"4. Max Tokens    [{max_tokens_text}]")
    print(f"5. Temperature   [{current.temperature:g}]")
    print(f"6. Timeout       [{current.timeout_seconds:g}s]")
    print(f"7. Retry         [{current.retry_count}]")
    print("8. API 测试")
    print(f"9. WebSearch     [{websearch_status}]")
    print("0. 返回上级")


def _render_system_settings(current: ProjectLingConfig) -> None:
    print("")
    print("系统设置")
    print(f"1. 角色停留时长  [{current.role_ttl_hours}h]")
    print(f"2. 协作模式      [{current.collab_mode}]")
    print("0. 返回上级")


def _render_websearch_settings(current: ProjectLingConfig) -> None:
    print("")
    print("WEBSEARCH API")
    print(f"1. Summary Key   [{_key_status(current.websearch_summary_key)}]")
    print(f"2. Web Key       [{_key_status(current.websearch_web_key)}]")
    print(f"3. Endpoint      [{current.websearch_endpoint}]")
    print("4. Test summary")
    print("5. Test web")
    print("0. 返回上级")


def _run_websearch_test(config: ProjectLingConfig, *, mode: str) -> None:
    print("")
    query = _prompt_line("输入测试搜索词，留空使用默认 > ").strip() or "AI 大模型 最新热点"
    registry = ToolRegistry(config)
    context = ToolContext(cwd=Path.cwd(), home=Path.home(), config=config)
    tool_call = {
        "id": f"settings-websearch-{mode}",
        "function": {
            "name": "web_search",
            "arguments": json.dumps({"query": query, "mode": mode, "count": 3}, ensure_ascii=False),
        },
    }
    payload = registry.execute_tool_call(tool_call, context)
    try:
        result = json.loads(str(payload.get("content") or "{}"))
    except json.JSONDecodeError:
        print("测试失败：工具返回不是合法 JSON。")
        return

    print(f"status={result.get('status')} mode={result.get('mode_used') or mode} count={result.get('result_count', 0)}")
    if result.get("message"):
        print(f"message={result.get('message')}")
    summary = str(result.get("summary") or "").strip()
    if summary:
        print(f"summary={summary[:220]}{'…' if len(summary) > 220 else ''}")
    for index, item in enumerate(result.get("results") or [], start=1):
        if index > 3:
            break
        print(f"{index}. {item.get('title') or ''}")
        if item.get("url"):
            print(f"   {item.get('url')}")


def _run_websearch_settings_ui() -> None:
    while True:
        current = load_config()
        _render_websearch_settings(current)
        choice = _prompt_line("> ").strip()

        if choice == "1":
            key = _prompt_line("输入 VOLC_WEBSEARCH_SUMMARY_KEY，留空保持原样 > ").strip()
            if key:
                _save_config_value(current, {"VOLC_WEBSEARCH_SUMMARY_KEY": key})
                print("Summary Key 已写入。")
            else:
                print("未输入内容，保持原样。")
            continue

        if choice == "2":
            key = _prompt_line("输入 VOLC_WEBSEARCH_WEB_KEY，留空保持原样 > ").strip()
            if key:
                _save_config_value(current, {"VOLC_WEBSEARCH_WEB_KEY": key})
                print("Web Key 已写入。")
            else:
                print("未输入内容，保持原样。")
            continue

        if choice == "3":
            endpoint = _prompt_optional_text("输入 WebSearch Endpoint，留空保持原样 > ")
            if endpoint is not None:
                _save_config_value(current, {"VOLC_WEBSEARCH_ENDPOINT": endpoint})
                print(f"Endpoint 已更新：{endpoint}")
            else:
                print("未输入内容，保持原样。")
            continue

        if choice == "4":
            _run_websearch_test(current, mode="summary")
            continue

        if choice == "5":
            _run_websearch_test(current, mode="web")
            continue

        if choice == "0" or not choice:
            return

        print("无效输入。")


def _run_persona_settings_ui(config: ProjectLingConfig | None = None) -> int:
    current = config or load_config()
    roster = load_roster(current)
    while True:
        active_role, _seed = resolve_current_role(current)
        bundle = resolve_persona_bundle(current, role=active_role)
        print("")
        print("PERSONA")
        print(f"当前主角色: {active_role.name_zh} / {active_role.name_en}")
        print(f"当前辅导位: {bundle.liaison.name_zh} / {bundle.liaison.name_en}" if bundle.liaison is not None else "当前辅导位: 未配置")
        print(f"联动名: {bundle.pair_label}")
        print("1. 选择主角色")
        print("2. 选择辅导位")
        print("3. 查看角色列表")
        print("0. 返回上级")
        choice = _prompt_line("> ").strip()

        if choice == "1":
            picked = _pick_role_from_roster(
                roster,
                header="选择主角色",
                current_role=active_role,
            )
            if picked is None:
                print("未选择角色。")
                continue
            select_current_role_by_name(picked.name_en, current)
            print(f"已选择主角色：{picked.name_zh} / {picked.name_en}")
            current = load_config()
            continue

        if choice == "2":
            picked = _pick_role_from_roster(
                roster,
                header="选择辅导位",
                current_role=bundle.liaison,
            )
            if picked is None:
                print("未选择角色。")
                continue
            if picked.name_en == active_role.name_en:
                print("辅导位不能与主角色相同。")
                continue
            select_liaison_role_by_name(picked.name_en, current)
            print(f"已选择辅导位：{picked.name_zh} / {picked.name_en}")
            current = load_config()
            continue

        if choice == "3":
            print("")
            for index, role in enumerate(roster, start=1):
                print(f"{index:02d}. [{role.rarity}] {role.name_zh} / {role.name_en}")
            continue

        if choice == "0" or not choice:
            return 0

        print("无效输入。")


def _resolve_role_from_input(roster: list[LauncherRole], raw: str) -> LauncherRole | None:
    text = str(raw or "").strip()
    if not text:
        return None
    if text.isdigit():
        index = int(text)
        if 1 <= index <= len(roster):
            return roster[index - 1]
    lowered = text.lower()
    for role in roster:
        if role.name_en.lower() == lowered or role.name_zh.lower() == lowered:
            return role
    return None


def _pick_role_from_roster(
    roster: list[LauncherRole],
    *,
    prompt: str = "输入角色序号或名称 > ",
    header: str = "PERSONA",
    current_role: LauncherRole | None = None,
) -> LauncherRole | None:
    print("")
    print(header)
    for index, role in enumerate(roster, start=1):
        marker = "  当前" if current_role is not None and role.name_en == current_role.name_en else ""
        print(f"{index:02d}. [{role.rarity}] {role.name_zh} / {role.name_en}{marker}")
    picked = _prompt_line(prompt).strip()
    return _resolve_role_from_input(roster, picked)


def _run_role_chat(
    config: ProjectLingConfig,
    role: LauncherRole,
    message: str,
    *,
    cwd: str | Path,
    allow_tools: bool,
    stream: bool,
    as_json: bool,
    persona_bundle: PersonaBundle | None = None,
) -> int:
    engine = ProjectLingEngine(config)
    selected_bundle = persona_bundle or PersonaBundle(main=role, source="direct")
    current_cwd = Path(cwd).expanduser()
    route = engine.preview_route(message, allow_tools=allow_tools, dispatch_mode="chat")
    use_stream = bool(stream and not as_json)

    if use_stream:
        printer = ShellStreamPrinter(
            engine.prompt_bundle,
            role,
            persona_bundle=selected_bundle,
            context_budget=load_context_budget(config),
        )
        printer.begin("thinking" if bool(route.get("thinking_enabled")) else "responding")
        try:
            result = engine.chat(
                message,
                cwd=current_cwd,
                mode="chat",
                allow_tools=allow_tools,
                stream=True,
                on_stream_delta=printer.on_delta,
                on_stream_event=printer.on_event,
                role_override=role,
                persona_bundle_override=selected_bundle,
            )
        except KeyboardInterrupt:
            printer.emit_message("已中断。")
            printer.finish("")
            return 130
        except Exception as exc:  # pragma: no cover - CLI safety net
            printer.emit_message(f"运行失败：{exc}")
            printer.finish("")
            return 1
        if not result.text and not result.tool_traces:
            if result.finish_reason == "stream_limit":
                printer.finish("本轮输出已达到上限。")
            else:
                printer.finish("我没有得到有效回复。")
        else:
            printer.finish(result.text or "")
        return 0

    result = engine.chat(
        message,
        cwd=current_cwd,
        mode="chat",
        allow_tools=allow_tools,
        role_override=role,
        persona_bundle_override=selected_bundle,
    )
    if as_json:
        display_bundle = result.persona_bundle or selected_bundle
        print(
            json.dumps(
                {
                    "text": result.text,
                    "reasoning_text": result.reasoning_text,
                    "rounds": result.rounds,
                    "used_tools": result.used_tools,
                    "thinking_traces": list(result.thinking_traces),
                    "tool_traces": list(result.tool_traces),
                    "finish_reason": result.finish_reason,
                    "routing": result.routing,
                    "persona": {
                        "display_zh": display_bundle.main.name_zh,
                        "display_en": display_bundle.main.name_en,
                        "liaison_display_zh": display_bundle.liaison.name_zh if display_bundle.liaison else "",
                        "liaison_display_en": display_bundle.liaison.name_en if display_bundle.liaison else "",
                        "liaison": display_bundle.liaison_label,
                        "source": display_bundle.source,
                    },
                    "role": {
                        "rarity": result.role.rarity,
                        "name_zh": result.role.name_zh,
                        "name_en": result.role.name_en,
                    },
                    "raw_response": result.raw_response,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    receipts = _render_tool_receipts(result.tool_traces)
    display_bundle = result.persona_bundle or selected_bundle
    if receipts and result.text:
        print(f"{receipts}{_render_assistant_block(result.text, role=result.role, persona_bundle=display_bundle)}")
    elif receipts:
        print(receipts)
    else:
        print(_render_assistant_block(result.text, role=result.role, persona_bundle=display_bundle))
    return 0

def _run_api_test(config: ProjectLingConfig) -> None:
    print("")
    print("API TEST")
    if not config.api_key:
        print("未设置 API Key。先执行 1 写入 API Key。")
        return

    client = DeepSeekClient(config)
    _planner_model, executor_model = _collab_mode_models(config.collab_mode)
    messages = [
        {"role": "system", "content": "你是连通性测试助手。只回复 pong。"},
        {"role": "user", "content": "ping"},
    ]

    try:
        if config.enable_sse:
            preview = ""
            for chunk in client.chat_completions_stream(
                messages=messages,
                tools=None,
                tool_choice="none",
                model=executor_model,
            ):
                choice = ((chunk.get("choices") or [{}])[0] or {})
                delta = choice.get("delta") or {}
                preview += str(delta.get("content") or "")
                finish_reason = str(choice.get("finish_reason") or "").strip().lower()
                if len(preview.strip()) >= 12 or finish_reason == "stop":
                    break
            preview = preview.strip() or "(收到流式事件，内容为空)"
            print(f"SSE 连通成功。预览响应：{preview[:80]}")
            print(f"超时规则：配置值 {config.timeout_seconds:g}s，SSE 读超时已自动放宽。")
            return

        data = client.chat_completions(messages=messages, tools=None, tool_choice="none", model=executor_model)
        choice = ((data.get("choices") or [{}])[0] or {})
        message = choice.get("message") or {}
        preview = str(message.get("content") or "").strip() or "(响应为空)"
        print(f"普通请求连通成功。预览响应：{preview[:80]}")
        print(f"超时规则：当前使用 {config.timeout_seconds:g}s。")
    except Exception as exc:
        print(f"连通失败：{exc}")


def _toggle_config_value(config: ProjectLingConfig, key: str, current: bool, label: str) -> ProjectLingConfig:
    updated = _save_config_value(config, {key: "0" if current else "1"})
    print(f"{label} 已切换为 {_bool_label(not current)}。")
    return updated


def _bootstrap_missing_key(config: ProjectLingConfig) -> ProjectLingConfig | None:
    role, _seed = resolve_current_role(config)
    print(_render_assistant_block("尚未配置 API Key。", role=role))
    key = _prompt_line("  输入 API Key，直接回车跳过 > ").strip()
    if not key:
        print("  已跳过配置。输入 /settings 继续设置。\n")
        return None

    updated = _save_config_value(config, {"DEEPSEEK_API_KEY": key})
    print("基础设置已写入。\n")
    return updated


class ShellStreamPrinter:
    def __init__(
        self,
        prompt_bundle: PromptBundle,
        role: LauncherRole,
        *,
        persona_bundle: PersonaBundle | None = None,
        show_role_heading: bool = True,
        context_budget: dict[str, Any] | None = None,
    ) -> None:
        self.prompt_bundle = prompt_bundle
        self.role = role
        self.persona_bundle = persona_bundle or PersonaBundle(main=role)
        self.typing = prompt_bundle.typing
        self.sanitizer = StreamingTextSanitizer()
        self.heading_printed = not show_role_heading
        self.line_open = False
        self.status_visible = False
        self.status_kind = ""
        self.status_label = ""
        self.status_frame = 0
        self.status_lock = threading.RLock()
        self.working_active = False
        self.working_thread: threading.Thread | None = None
        self.working_stop_event = threading.Event()
        self.saw_content = False
        self.reasoning_buffer: list[str] = []
        self.reasoning_started_at: float | None = None
        self.reasoning_live_lines = 0
        self.last_reasoning_render_at = 0.0
        self.tool_active = False
        self.tool_payload: dict[str, Any] | None = None
        self.tool_block_rendered = False
        self.tool_group_family: str | None = None
        self.tool_running_rendered = False
        self.tool_running_lines = 0
        self.tool_running_started_at: float | None = None
        self.tool_saw_output = False
        self.tool_stream_seen = {"stdout": 0, "stderr": 0}
        self.pending_tool_receipts: list[dict[str, Any]] = []
        self.markdown_pending = ""
        self.assistant_content_seen = False
        self.can_control = _supports_tty_control()
        self.typewriter_enabled = bool(self.typing.get("enabled", True) and sys.stdout.isatty())
        self.renderer = MarkdownAnsiRenderer(tty=self.can_control)
        self.thinking_label = _normalize_status_label(prompt_bundle.status.get("thinking"), "Thinking")
        self.responding_label = _normalize_status_label(
            prompt_bundle.status.get("responding"),
            "Responding",
        )
        self.context_budget = dict(context_budget or load_context_budget(load_config()))

    def _update_context_budget(self, payload: dict[str, Any] | None) -> None:
        if not isinstance(payload, dict):
            return
        if "context_budget_percent" not in payload and str(payload.get("tool") or "") != "context":
            return
        self.context_budget = dict(payload)

    def _apply_persona_handoff_payload(self, payload: dict[str, Any]) -> bool:
        resolved = _persona_from_handoff_payload(payload)
        if resolved is None:
            return False
        role, persona_bundle = resolved
        current_liaison = self.persona_bundle.liaison.name_en if self.persona_bundle.liaison else ""
        next_liaison = persona_bundle.liaison.name_en if persona_bundle.liaison else ""
        same_heading = (
            self.role.name_en == role.name_en
            and self.persona_bundle.main.name_en == persona_bundle.main.name_en
            and current_liaison == next_liaison
        )
        self.role = role
        self.persona_bundle = persona_bundle
        if same_heading or not self.heading_printed:
            return True
        if self.assistant_content_seen:
            return True
        self.clear_status()
        if self.line_open:
            self._write("\n")
            self.line_open = False
        heading = _format_role_heading(self.role, self.persona_bundle)
        context_line = _context_budget_line(self.context_budget)
        if context_line:
            self._write(f"\n{heading}  {context_line}\n\n")
        else:
            self._write(f"\n{heading}\n\n")
        return True

    def _apply_executor_handoff_payload(self, payload: dict[str, Any]) -> bool:
        tool_name = str(payload.get("tool") or "")
        action_name = str(payload.get("action") or "").strip().lower()
        target = str(payload.get("target") or "").strip().lower()
        if tool_name != "link" or action_name != "continue" or target not in {"executor", "liaison"}:
            return False
        actor_kind = str(payload.get("actor_kind") or "").strip().lower()
        executor_keys = ["executor_name", "liaison_name"]
        planner_keys = ["planner_name", "main_name", "main_role"]
        if actor_kind == "executor":
            executor_keys.append("actor_name")
        elif actor_kind == "planner":
            planner_keys.append("actor_name")
        executor = _role_from_roster_payload_priority(payload, *executor_keys)
        planner = _role_from_roster_payload_priority(payload, *planner_keys)
        if executor is None:
            return False
        current_liaison = self.persona_bundle.liaison.name_en if self.persona_bundle.liaison else ""
        same_heading = (
            self.role.name_en == executor.name_en
            and self.persona_bundle.source == "executor_handoff"
            and current_liaison == (planner.name_en if planner else "")
        )
        self.role = executor
        self.persona_bundle = PersonaBundle(main=executor, liaison=planner, source="executor_handoff")
        if same_heading:
            return True
        self.clear_status()
        if self.line_open:
            self._write("\n")
            self.line_open = False
        heading = _format_role_heading(self.role, self.persona_bundle)
        context_line = _context_budget_line(self.context_budget)
        if context_line:
            self._write(f"\n{heading}  {context_line}\n\n")
        else:
            self._write(f"\n{heading}\n\n")
        return True

    def _write(self, text: str, *, flush: bool = True) -> None:
        sys.stdout.write(text)
        if flush:
            sys.stdout.flush()

    def _render_markdown_stream_text(self, text: str) -> str:
        if not text:
            return ""
        if not text.strip():
            return text
        rendered = self.renderer.render(text)
        if rendered or "\n" not in text:
            return rendered
        return text

    def _flush_markdown_pending(self, *, force: bool = False) -> None:
        while self.markdown_pending:
            if force:
                chunk = self.markdown_pending
                self.markdown_pending = ""
            else:
                split_index = _markdown_stream_flush_index(self.markdown_pending)
                if split_index <= 0:
                    return
                chunk = self.markdown_pending[:split_index]
                self.markdown_pending = self.markdown_pending[split_index:]

            rendered = self._render_markdown_stream_text(chunk)
            if rendered:
                self._write_indented(rendered)

            if not force and _markdown_stream_flush_index(self.markdown_pending) <= 0:
                return

    def _queue_markdown_stream_text(self, text: str) -> None:
        if not text:
            return
        if text.strip():
            self.saw_content = True
            self.assistant_content_seen = True
        self.markdown_pending += text
        self._flush_markdown_pending(force=False)

    def _status_text(self, kind: str, label: str | None = None) -> str:
        base = _normalize_status_label(label or (self.thinking_label if kind == "thinking" else self.responding_label), "Working")
        trimmed = base.rstrip(".")
        dots = "." * ((self.status_frame % 3) + 1)
        prefix = "◔" if kind == "thinking" else "●"
        text = f"{prefix} {trimmed}{dots}"
        return _style_status(text, kind)

    def start(self) -> None:
        if self.heading_printed:
            return
        heading = _format_role_heading(self.role, self.persona_bundle)
        context_line = _context_budget_line(self.context_budget)
        if context_line:
            self._write(f"\n{heading}  {context_line}\n\n")
        else:
            self._write(f"\n{heading}\n\n")
        self.heading_printed = True

    def show_status(self, kind: str) -> None:
        with self.status_lock:
            if not self.can_control:
                self.start()
                if self.line_open:
                    self._write("\n")
                    self.line_open = False
                if not self.status_visible:
                    self.status_kind = kind
                    self.status_label = self.thinking_label if kind == "thinking" else self.responding_label
                    self._write(f"{self._status_text(kind, self.status_label)}\n")
                    self.status_visible = True
                    self.start_working_animation()
                return
            self.start()
            if self.line_open:
                self._write("\n")
                self.line_open = False
            if self.status_visible:
                self._write("\033[A\r\033[2K")
            self.status_kind = kind
            self.status_label = self.thinking_label if kind == "thinking" else self.responding_label
            self._write(f"{self._status_text(kind, self.status_label)}\n")
            self.status_visible = True
            self.start_working_animation()

    def refresh_status(self) -> None:
        with self.status_lock:
            if not self.status_visible or not self.can_control:
                return
            self.status_frame += 1
            self._write(f"\033[A\r\033[2K{self._status_text(self.status_kind or 'thinking', self.status_label)}\n")

    def clear_status(self) -> None:
        with self.status_lock:
            if not self.status_visible or not self.can_control:
                self.status_visible = False
                self.status_kind = ""
                self.status_label = ""
                return
            self._write("\033[A\r\033[2K")
            self.status_visible = False
            self.status_kind = ""
            self.status_label = ""

    def _working_loop(self) -> None:
        while not self.working_stop_event.wait(WORKING_ANIMATION_INTERVAL_SECONDS):
            if not self.working_active:
                break
            try:
                self.refresh_status()
                self._refresh_tool_running_block()
            except Exception:
                break
        self.working_active = False

    def start_working_animation(self) -> None:
        if self.working_active or not self.can_control:
            return
        self.working_active = True
        self.working_stop_event.clear()
        worker = threading.Thread(
            target=self._working_loop,
            name="projectling-working",
            daemon=True,
        )
        self.working_thread = worker
        worker.start()

    def stop_working_animation(self) -> None:
        self.working_active = False
        self.working_stop_event.set()
        worker = self.working_thread
        if worker is not None and worker is not threading.current_thread():
            try:
                worker.join(timeout=0.4)
            except RuntimeError:
                pass
        self.working_thread = None

    def begin(self, kind: str) -> None:
        self.show_status(kind)

    def _current_speaker_label(self) -> str:
        if self.persona_bundle.source in {"speaker_handoff", "executor_handoff", "persona_link_mission", "liaison_tool"}:
            return "辅导位"
        return "主角色"

    def _thinking_body_lines(self, text: str) -> list[str]:
        cleaned = (text or "").strip()
        if not cleaned:
            return []
        rendered = _strip_ansi(self.renderer.render(cleaned).rstrip("\n"))
        wrap_width = max(12, _terminal_render_width() - 4)
        wrapped_lines: list[str] = []
        raw_lines = rendered.splitlines() if rendered else ["..."]
        for raw_line in raw_lines:
            if raw_line:
                wrapped_lines.extend(_wrap_ansi_display(raw_line, wrap_width))
            else:
                wrapped_lines.append("")
        body_lines = wrapped_lines or ["..."]
        if len(body_lines) > THINKING_PREVIEW_MAX_LINES:
            body_lines = body_lines[-THINKING_PREVIEW_MAX_LINES:]
            head = _strip_ansi(body_lines[0]).lstrip()
            body_lines[0] = _truncate_display_ellipsis(f"… {head}", wrap_width)
        return body_lines

    def _clear_live_reasoning(self) -> None:
        if not self.can_control or self.reasoning_live_lines <= 0:
            self.reasoning_live_lines = 0
            return
        for _ in range(self.reasoning_live_lines):
            self._write("\033[A\r\033[2K")
        self.reasoning_live_lines = 0

    def _render_live_reasoning(self) -> None:
        if not self.can_control:
            return
        body_lines = self._thinking_body_lines("".join(self.reasoning_buffer))
        if not body_lines:
            return
        now = time.monotonic()
        last_text = self.reasoning_buffer[-1] if self.reasoning_buffer else ""
        should_force = any(mark in last_text for mark in ("\n", "。", "！", "？", ".", "!", "?", "；", ";", "：", ":"))
        if not should_force and self.last_reasoning_render_at and (now - self.last_reasoning_render_at) < THINKING_RENDER_INTERVAL_SECONDS:
            return
        self.start()
        self.clear_status()
        if self.line_open:
            self._write("\n")
            self.line_open = False

        self._clear_live_reasoning()
        block_lines: list[str] = []
        block_lines.append(_style_status(f"◔ Thinking · {self._current_speaker_label()}", "thinking"))
        for line in body_lines:
            body_line = _style_thought_text(line) if line else ""
            block_lines.append(f"  {body_line}" if line else "")
        self._write("\n".join(block_lines) + "\n")
        self.reasoning_live_lines = len(block_lines)
        self.last_reasoning_render_at = now

    def _fold_reasoning_summary(self, elapsed_seconds: float | None = None) -> None:
        text = "".join(self.reasoning_buffer).strip()
        if not text:
            self.reasoning_buffer = []
            self.reasoning_started_at = None
            self.reasoning_live_lines = 0
            self.last_reasoning_render_at = 0.0
            return

        summary_text = _style_thought_text(_format_thought_summary(elapsed_seconds))
        self.start()
        self.clear_status()
        if self.line_open:
            self._write("\n")
            self.line_open = False
        self._clear_live_reasoning()
        self._write(f"{summary_text}\n\n")
        self.reasoning_buffer = []
        self.reasoning_started_at = None
        self.reasoning_live_lines = 0
        self.last_reasoning_render_at = 0.0

    def _print_transient_block(self, header: str, text: str, *, elapsed_seconds: float | None = None) -> None:
        body_lines = self._thinking_body_lines(text)
        if not body_lines:
            return
        self.start()
        self.clear_status()
        if self.line_open:
            self._write("\n")
            self.line_open = False

        block_lines: list[str] = []
        block_lines.append(header)
        for line in body_lines:
            body_line = _style_thought_text(line) if line else ""
            block_lines.append(f"  {body_line}" if line else "")

        block_text = "\n".join(block_lines)
        self._write(f"{block_text}\n")
        summary_text = _style_thought_text(_format_thought_summary(elapsed_seconds))
        if not self.can_control:
            self._write(f"{summary_text}\n\n")
            return

        time.sleep(THINKING_FOLD_DELAY_SECONDS)
        for _ in range(len(block_lines)):
            self._write("\033[A\r\033[2K")
        self._write(f"{summary_text}\n\n")

    def flush_reasoning_trace(self) -> None:
        text = "".join(self.reasoning_buffer).strip()
        elapsed_seconds = None
        if self.reasoning_started_at is not None:
            elapsed_seconds = max(0.0, time.monotonic() - self.reasoning_started_at)
        if not text:
            return
        self._fold_reasoning_summary(elapsed_seconds)

    def show_thinking_trace(self, text: str, *, elapsed_seconds: float | None = None, role_label: str = "") -> None:
        label = role_label.strip() or self._current_speaker_label()
        self._print_transient_block(_style_heading(f"◔ Thinking · {label}", "thinking"), text, elapsed_seconds=elapsed_seconds)

    def _reset_tool_state(self) -> None:
        self.tool_active = False
        self.tool_payload = None
        self.tool_block_rendered = False
        self.tool_group_family = None
        self.tool_running_rendered = False
        self.tool_running_lines = 0
        self.tool_running_started_at = None
        self.tool_saw_output = False
        self.tool_stream_seen = {"stdout": 0, "stderr": 0}

    def _emit_plain_block_direct(self, text: str, *, trailing_blank: bool = True) -> None:
        block = (text or "").rstrip()
        if not block:
            return
        self._flush_markdown_pending(force=True)
        self.start()
        self.clear_status()
        if self.line_open:
            self._write("\n")
            self.line_open = False
        self._write(f"{block}\n")
        if trailing_blank:
            self._write("\n")
        self.saw_content = True

    def _flush_pending_tool_receipts(self) -> None:
        if not self.pending_tool_receipts:
            return
        blocks: list[str] = []
        grouped_payloads: list[dict[str, Any]] = []
        grouped_family: str | None = None

        def flush_group() -> None:
            nonlocal grouped_payloads, grouped_family
            if not grouped_payloads:
                return
            if (len(grouped_payloads) == 1 and grouped_family != "explore") or grouped_family is None:
                blocks.append(_render_tool_receipt_payload(grouped_payloads[0]))
            else:
                blocks.append(_render_grouped_tool_receipt(grouped_payloads, grouped_family))
            grouped_payloads = []
            grouped_family = None

        for payload in self.pending_tool_receipts:
            if _should_suppress_tool_receipt(payload):
                continue
            family = _tool_group_family(payload)
            if family:
                if grouped_payloads and family != grouped_family:
                    flush_group()
                grouped_payloads.append(payload)
                grouped_family = family
                continue
            flush_group()
            blocks.append(_render_tool_receipt_payload(payload))
        flush_group()
        self.pending_tool_receipts = []
        if blocks:
            self._emit_plain_block_direct("\n\n".join(blocks), trailing_blank=True)

    def _queue_pending_tool_receipt(self, payload: dict[str, Any]) -> None:
        family = _tool_group_family(payload)
        if self.pending_tool_receipts and family and _tool_group_family(self.pending_tool_receipts[-1]) != family:
            self._flush_pending_tool_receipts()
        self.pending_tool_receipts.append(dict(payload))

    def _render_tool_running_block(self, payload: dict[str, Any]) -> None:
        if self.tool_running_rendered:
            return
        self.start()
        self.clear_status()
        if self.line_open:
            self._write("\n")
            self.line_open = False
        block = _render_tool_running_receipt(payload)
        self._write(f"{block}\n")
        self.tool_running_rendered = True
        self.tool_running_lines = max(1, len(block.splitlines()))
        self.tool_running_started_at = time.monotonic()
        self.saw_content = True
        self.start_working_animation()

    def _tool_running_output_line(self) -> str:
        dots = "." * ((self.status_frame % 3) + 1)
        parts: list[str] = [f"Running{dots}"]
        if self.tool_running_started_at is not None:
            elapsed = max(0.0, time.monotonic() - self.tool_running_started_at)
            if elapsed >= 1.0:
                parts.append(f"{elapsed:.0f}s")
        stdout_seen = int(self.tool_stream_seen.get("stdout") or 0)
        stderr_seen = int(self.tool_stream_seen.get("stderr") or 0)
        if stdout_seen:
            parts.append(f"stdout {stdout_seen} chars")
        if stderr_seen:
            parts.append(f"stderr {stderr_seen} chars")
        return _tool_meta_line("OUTPUT", *parts, color=ANSI_SOFT_PINK)

    def _refresh_tool_running_block(self) -> None:
        with self.status_lock:
            if not self.can_control or not self.tool_running_rendered or self.tool_running_lines <= 0:
                return
            self.status_frame += 1
            self._write(f"\033[A\r\033[2K{self._tool_running_output_line()}\n")

    def _clear_tool_running_block(self) -> None:
        with self.status_lock:
            if not self.can_control or not self.tool_running_rendered or self.tool_running_lines <= 0:
                self.tool_running_rendered = False
                self.tool_running_lines = 0
                self.tool_running_started_at = None
                return
            lines = self.tool_running_lines
            self.tool_running_rendered = False
            self.tool_running_lines = 0
            self.tool_running_started_at = None
            for _ in range(lines):
                self._write("\033[A\r\033[2K")

    def _start_tool_block(self, payload: dict[str, Any]) -> None:
        self.start()
        self.clear_status()
        if self.line_open:
            self._write("\n")
            self.line_open = False
        self._reset_tool_state()
        self.tool_active = True
        self.tool_block_rendered = True
        self.saw_content = True
        self.tool_payload = dict(payload)
        heading = _tool_heading(payload)
        width = max(24, _terminal_render_width() - 8)
        self._write(f"\n{heading}\n")
        for index, line in enumerate(_tool_preview_lines(_shorten_tool_text(str(payload.get("command") or "")), width=width, max_lines=3)):
            prefix = _tool_prefix("CMD", first=index == 0)
            self._write(f"{prefix}{_style_tool_omission(line)}\n")

    def _finish_tool_block(self, payload: dict[str, Any]) -> None:
        self._clear_tool_running_block()
        self._emit_plain_block_direct(_render_tool_receipt_payload(payload), trailing_blank=True)
        self._reset_tool_state()

    def on_event(self, kind: str, payload: dict[str, Any]) -> None:
        if kind == "thinking_trace":
            text = str(payload.get("text") or "").strip()
            if text:
                trace_role = str(payload.get("role") or "").strip().lower()
                role_label = "主角色" if trace_role.startswith("planner") else ""
                self.show_thinking_trace(text, elapsed_seconds=payload.get("elapsed_seconds"), role_label=role_label)
                payload["_frontend_rendered"] = True
            return
        if kind == "stream_limit":
            if bool(payload.get("soft")):
                self.show_status("thinking")
                return
            self._clear_live_reasoning()
            self.reasoning_buffer = []
            self.reasoning_started_at = None
            self.last_reasoning_render_at = 0.0
            note = str(payload.get("message") or payload.get("reason") or "流式输出已达到上限。")
            self._emit_plain_block_direct(_style_tool_line(f"  {note}", ANSI_SOFT_RED, bold=True), trailing_blank=True)
            self.show_status("thinking")
            return
        if kind == "tool_start":
            tool_name = str(payload.get("tool") or "")
            if tool_name in {"persona_handoff", "persona_link", "liaison"}:
                self._flush_markdown_pending(force=True)
                if self.pending_tool_receipts:
                    self._flush_pending_tool_receipts()
                elapsed_seconds = None
                if self.reasoning_started_at is not None:
                    elapsed_seconds = max(0.0, time.monotonic() - self.reasoning_started_at)
                self._fold_reasoning_summary(elapsed_seconds)
                self._reset_tool_state()
                if tool_name == "persona_handoff":
                    self._apply_persona_handoff_payload(payload)
                return
            self._flush_markdown_pending(force=True)
            elapsed_seconds = None
            if self.reasoning_started_at is not None:
                elapsed_seconds = max(0.0, time.monotonic() - self.reasoning_started_at)
            self._fold_reasoning_summary(elapsed_seconds)
            family = _tool_group_family(payload)
            if family:
                if self.pending_tool_receipts and _tool_group_family(self.pending_tool_receipts[-1]) != family:
                    self._flush_pending_tool_receipts()
                self.start()
                self.clear_status()
                if self.line_open:
                    self._write("\n")
                    self.line_open = False
                self._reset_tool_state()
                self.tool_active = True
                self.tool_group_family = family
                self.saw_content = True
                self.tool_payload = dict(payload)
                self._render_tool_running_block(payload)
            else:
                if self.pending_tool_receipts:
                    self._flush_pending_tool_receipts()
                self.start()
                self.clear_status()
                if self.line_open:
                    self._write("\n")
                    self.line_open = False
                self._reset_tool_state()
                self.tool_active = True
                self.saw_content = True
                self.tool_payload = dict(payload)
                self._render_tool_running_block(payload)
            return
        if kind in {"tool_stdout", "tool_stderr"}:
            stream = "stdout" if kind == "tool_stdout" else "stderr"
            if not self.tool_active:
                self.start()
                self.clear_status()
                self._reset_tool_state()
                self.tool_active = True
                self.tool_payload = dict(payload)
                self._render_tool_running_block(payload)
            self.tool_stream_seen[stream] = self.tool_stream_seen.get(stream, 0) + len(str(payload.get("text") or ""))
            self.tool_saw_output = True
            return
        if kind == "tool_result":
            elapsed_seconds = None
            if self.reasoning_started_at is not None:
                elapsed_seconds = max(0.0, time.monotonic() - self.reasoning_started_at)
            self._fold_reasoning_summary(elapsed_seconds)
            self._update_context_budget(payload)
            tool_name = str(payload.get("tool") or "")
            action_name = str(payload.get("action") or payload.get("speaker_mode") or "").strip().lower()
            if (
                tool_name == "persona_handoff"
                or (tool_name == "persona_link" and action_name == "switch")
                or (tool_name == "link" and action_name == "switch")
            ):
                self._clear_tool_running_block()
                self._reset_tool_state()
                self._apply_persona_handoff_payload(payload)
                rendered = _render_tool_receipt_payload(payload)
                if rendered:
                    self._emit_plain_block_direct(rendered, trailing_blank=True)
                payload["_frontend_rendered"] = True
                return
            if self.tool_active:
                if self.tool_block_rendered:
                    self._finish_tool_block(payload)
                    if str(payload.get("tool") or "") in {"persona_link", "liaison"}:
                        payload["_frontend_rendered"] = True
                else:
                    self._clear_tool_running_block()
                    self._queue_pending_tool_receipt(payload)
                    self._reset_tool_state()
            else:
                if _tool_group_family(payload):
                    self._queue_pending_tool_receipt(payload)
                else:
                    self._flush_pending_tool_receipts()
                    rendered = _render_tool_receipt_payload(payload)
                    if rendered:
                        self._emit_plain_block_direct(rendered, trailing_blank=True)
                    if str(payload.get("tool") or "") in {"persona_link", "liaison"}:
                        payload["_frontend_rendered"] = True
            if tool_name == "link" and action_name == "continue" and str(payload.get("target") or "").strip().lower() in {"executor", "liaison"}:
                self._apply_executor_handoff_payload(payload)

    def _sleep_for_char(self, char: str, burst_count: int) -> int:
        if not self.typewriter_enabled:
            return burst_count
        punctuation_delay_ms = max(0, int(self.typing.get("punctuation_delay_ms", 10)))
        char_delay_ms = max(0, int(self.typing.get("char_delay_ms", 2)))
        burst_chars = max(1, int(self.typing.get("burst_chars", 3)))

        if char in "\n":
            return 0
        if char in "，。！？；：、,.!?;:" and punctuation_delay_ms > 0:
            time.sleep(punctuation_delay_ms / 1000.0)
            return 0
        if burst_count >= burst_chars and char_delay_ms > 0:
            time.sleep(char_delay_ms / 1000.0)
            return 0
        return burst_count

    def _write_indented(self, text: str) -> None:
        if not text:
            return
        self.start()
        self.clear_status()
        bulk_mode = not self.typewriter_enabled
        burst_count = 0
        for token in _tokenize_ansi(text):
            if not token:
                continue
            if ANSI_PATTERN.fullmatch(token):
                if not self.line_open:
                    self._write("  ", flush=not bulk_mode)
                    self.line_open = True
                self._write(token, flush=not bulk_mode)
                continue
            for char in token:
                if char == "\n":
                    self._write("\n", flush=not bulk_mode)
                    self.line_open = False
                    burst_count = 0
                    continue
                if not self.line_open:
                    self._write("  ", flush=not bulk_mode)
                    self.line_open = True
                self._write(char, flush=not bulk_mode)
                burst_count += 1
                burst_count = self._sleep_for_char(char, burst_count)
        if bulk_mode:
            sys.stdout.flush()

    def on_delta(self, kind: str, text: str) -> None:
        if kind == "reasoning":
            self._flush_pending_tool_receipts()
            if text:
                if self.reasoning_started_at is None:
                    self.reasoning_started_at = time.monotonic()
                self.reasoning_buffer.append(text)
                self._render_live_reasoning()
            elif not self.saw_content:
                self.show_status("thinking")
            return
        if kind != "content" or not text:
            return
        self.flush_reasoning_trace()
        self._flush_pending_tool_receipts()
        cleaned = self.sanitizer.push(text)
        if not cleaned:
            return
        self._queue_markdown_stream_text(cleaned)

    def emit_message(self, text: str) -> None:
        self._flush_markdown_pending(force=True)
        self._flush_pending_tool_receipts()
        cleaned = self.sanitizer.push(text or "")
        cleaned += self.sanitizer.finish()
        body = ((cleaned or "").strip() or "我没有得到有效回复。") + "\n"
        self.saw_content = True
        self.assistant_content_seen = True
        self._write_indented(self.renderer.render(body))

    def emit_plain_block(self, text: str, *, trailing_blank: bool = True) -> None:
        self._flush_markdown_pending(force=True)
        self._flush_pending_tool_receipts()
        self._emit_plain_block_direct(text, trailing_blank=trailing_blank)

    def finish(self, fallback_message: str | None = None) -> None:
        self.stop_working_animation()
        self.flush_reasoning_trace()
        self._flush_pending_tool_receipts()
        tail = self.sanitizer.finish()
        if tail:
            self._queue_markdown_stream_text(tail)
        self._flush_markdown_pending(force=True)
        fallback = str(fallback_message or "")
        if not self.assistant_content_seen:
            if fallback.strip() or not self.saw_content:
                self.emit_message(fallback or "我没有得到有效回复。")
            else:
                self.clear_status()
        else:
            self.clear_status()
        if self.line_open:
            self._write("\n")
            self.line_open = False
        self._write("\n")


class ChatCore(ProjectLingEngine):
    """Compatibility shim for the old in-package API."""

    def chat(
        self,
        user_message: str,
        *,
        cwd: str | Path | None = None,
        history: list[dict[str, Any]] | None = None,
        allow_tools: bool | None = None,
        system_prompt: str | None = None,
        stream: bool = False,
        on_stream_delta: Any = None,
        on_stream_event: Any = None,
        mode: str = "chat",
    ) -> ChatResult:
        del history, system_prompt
        return super().chat(
            user_message,
            cwd=cwd,
            mode=mode,
            allow_tools=allow_tools,
            stream=stream,
            on_stream_delta=on_stream_delta,
            on_stream_event=on_stream_event,
        )


def _tool_preview_lines(
    text: str,
    *,
    width: int,
    head_lines: int = TOOL_PREVIEW_HEAD_LINES,
    tail_lines: int = TOOL_PREVIEW_TAIL_LINES,
    max_lines: int | None = None,
) -> list[str]:
    normalized = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    source_lines = [line.expandtabs(2).rstrip() for line in normalized.splitlines() if line.strip()]
    if not source_lines:
        return ["—"]

    if max_lines is not None:
        head_lines = max(1, (max_lines - 1) // 2)
        tail_lines = max(1, max_lines - head_lines - 1)

    visible_limit = max(1, head_lines) + max(1, tail_lines)
    if len(source_lines) > visible_limit:
        omitted = len(source_lines) - visible_limit
        selected = source_lines[:head_lines]
        selected.append(f"...   +{omitted} lines")
        selected.extend(source_lines[-tail_lines:])
    else:
        selected = source_lines
    lines = [_truncate_display_ellipsis(line, width) for line in selected]
    return lines


def _tool_line_count(text: str) -> int:
    normalized = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    return len([line for line in normalized.splitlines() if line.strip()])


def _shorten_tool_text(text: str) -> str:
    value = str(text or "")
    home = str(Path.home())
    replacements = (
        ("/data/data/com.termux/files/home", "~"),
        (home, "~"),
        ("/data/data/com.termux/files/usr", "$PREFIX"),
    )
    for src, dst in replacements:
        if src and src in value:
            value = value.replace(src, dst)
    value = PATHLIKE_TOKEN_RE.sub(lambda match: _shorten_path_token(match.group(0)), value)
    return value


def _tool_heading_base(payload: dict[str, Any]) -> str:
    status = str(payload.get("status") or "")
    channel = str(payload.get("channel") or payload.get("tool") or "Tool")
    tool = str(payload.get("tool") or "")
    if tool == "apply_patch":
        return "● Edit File"
    if tool == "link":
        return "● X-Link"
    if tool == "update_plan":
        return "● Plan"
    if tool == "model_mode":
        return "● 协作模式"
    if tool == "diary_keeper":
        return "● Diary Keeper"
    if tool == "persona_link":
        action = str(payload.get("action") or "").strip().lower()
        if action == "switch":
            return "● 角色切换"
        if action == "mission":
            return "● Mission"
        if action in {"send", "contact", "liaison"}:
            return "● 辅导位"
        return "● Persona Link"
    if tool == "liaison":
        return "● Liaison"
    if tool == "memory_add":
        return "● Memory Add"
    if tool == "memory_check":
        return "● Memory Check"
    if tool == "memory_read":
        return "● Memory Read"
    if tool == "memory_status":
        return "● Memory Status"
    if tool == "web_search":
        return "● WebSearch"
    if tool == "context":
        return "● Context"
    if tool in {"context_manage", "contextmanage"}:
        return "● Context Manage"
    if tool == "tool_manage":
        return "● Tool Box"
    if tool == "aidebug":
        return "● Explored"
    if status == "pending_confirmation":
        return f"● Confirm {channel}"
    if status == "rejected":
        return f"● Canceled {channel}"
    if channel == "Bash":
        return "● Ran COMMAND"
    return f"● Ran {channel}"


def _tool_heading_color(payload: dict[str, Any]) -> str:
    channel = str(payload.get("channel") or payload.get("tool") or "Tool")
    tool = str(payload.get("tool") or "")
    if tool == "apply_patch":
        return ANSI_MAGENTA
    if tool == "link":
        return ANSI_VIOLET
    if tool == "update_plan":
        return ANSI_SOFT_BLUE
    if tool == "model_mode":
        return ANSI_SOFT_BLUE
    if tool == "diary_keeper":
        return ANSI_SOFT_BLUE
    if tool == "persona_link":
        action = str(payload.get("action") or "").strip().lower()
        if action == "switch":
            return ANSI_GOLD
        if action == "mission":
            return ANSI_VIOLET
        if action in {"send", "contact", "liaison"}:
            return ANSI_CYAN
        return ANSI_CYAN
    if tool == "liaison":
        return ANSI_CYAN
    if tool in {"memory_add", "memory_check", "memory_read", "memory_status"}:
        return ANSI_SOFT_BLUE
    if tool == "web_search":
        return ANSI_CYAN
    if tool == "context":
        return ANSI_CYAN
    if tool in {"context_manage", "contextmanage"}:
        return ANSI_MAGENTA
    if tool == "tool_manage":
        return ANSI_CYAN
    if tool == "aidebug":
        return ANSI_SOFT_BLUE
    if channel == "ADB":
        return ANSI_SOFT_PINK
    if channel == "Termux API":
        return ANSI_MAGENTA
    return ANSI_GOLD


def _tool_command_summary(command: str, *, width: int = 44) -> str:
    text = _shorten_tool_text(command).strip()
    if not text:
        return ""
    return _middle_truncate_display(text, width)


def _tool_actor_text(payload: dict[str, Any], *, width: int = 42) -> str:
    label = str(payload.get("actor_label") or "").strip()
    name = str(payload.get("actor_name") or "").strip()
    actor_kind = str(payload.get("actor_kind") or "").strip().lower()
    if actor_kind == "executor" and not label:
        label = "执行位"
    elif actor_kind == "planner" and not label:
        label = "主角色"
    if not label and not name:
        return ""
    text = f"{label} · {name}" if label and name else label or name
    return _middle_truncate_display(_shorten_tool_text(text), width)


def _tool_manage_name_list(value: Any) -> list[str]:
    if isinstance(value, str):
        items: list[Any] = [value]
    elif isinstance(value, dict):
        items = [value]
    elif isinstance(value, (list, tuple)):
        items = list(value)
    else:
        items = []

    names: list[str] = []
    for item in items:
        if isinstance(item, dict):
            name = str(item.get("name") or item.get("tool") or "").strip()
            if not name:
                name = str(item.get("summary") or "").strip()
            if not name:
                continue
            if "expanded" in item:
                state = "expanded" if item.get("expanded") else "collapsed"
                names.append(f"{name} ({state})")
            else:
                names.append(name)
            continue
        text = str(item or "").strip()
        if text:
            names.append(text)
    return names


def _tool_explore_target(command: str, *, width: int = 58) -> str:
    tokens = _split_shell_words(command)
    if not tokens:
        return ""
    candidates = [token for token in tokens[1:] if "/" in token or token.startswith(("~", "$PREFIX"))]
    target = candidates[-1] if candidates else " ".join(tokens[1:]) if len(tokens) > 1 else tokens[0]
    return _middle_truncate_display(_shorten_tool_text(target), width)


def _command_is_explore_readonly(tokens: list[str]) -> bool:
    if not tokens:
        return False
    first = tokens[0]
    if first == "sed":
        return not any(token == "-i" or token == "--in-place" or token.startswith("-i") for token in tokens[1:])
    if first == "find":
        return not any(token in FIND_MUTATING_TOKENS or token.startswith("-exec") or token.startswith("-ok") for token in tokens[1:])
    return True


def _tool_group_family(payload: dict[str, Any]) -> str | None:
    status = str(payload.get("status") or "").strip()
    if status and status != "ok":
        return None
    if str(payload.get("stderr") or "").strip():
        return None
    tool = str(payload.get("tool") or "")
    if tool == "aidebug":
        action = str(payload.get("action") or "").strip().lower()
        return "explore" if action in {"read", "status"} else None
    if tool != "command" or str(payload.get("channel") or "") != "Bash":
        return None
    tokens = _split_shell_words(str(payload.get("command") or ""))
    if not tokens:
        return None
    first = tokens[0]
    if first in EXPLORE_SEARCH_COMMANDS or first in EXPLORE_READ_COMMANDS or first in EXPLORE_LIST_COMMANDS:
        if not _command_is_explore_readonly(tokens):
            return None
        return "explore"
    return None


def _tool_explore_label(payload: dict[str, Any]) -> str:
    if str(payload.get("tool") or "") == "aidebug":
        action = str(payload.get("action") or "").strip().lower()
        if action == "status":
            return "Status"
        return "Read"
    tokens = _split_shell_words(str(payload.get("command") or ""))
    first = tokens[0] if tokens else ""
    if first in EXPLORE_SEARCH_COMMANDS:
        return "Search"
    if first in EXPLORE_LIST_COMMANDS:
        return "List"
    return "Read"


def _tool_group_entry(payload: dict[str, Any], family: str) -> str:
    if family == "explore":
        if str(payload.get("tool") or "") == "aidebug":
            target = _shorten_tool_text(
                str(payload.get("relative_path") or payload.get("path") or payload.get("log_path") or "aidebug")
            )
            mode = str(payload.get("mode") or "").strip().lower()
            suffix = f" · {mode}" if mode and mode != "tail" else ""
            return f"{_tool_explore_label(payload)}  {target or 'aidebug'}{suffix}"
        command = str(payload.get("command") or "")
        command_text = _tool_explore_target(command, width=58) or _tool_command_summary(command, width=58) or "command"
        label = _tool_explore_label(payload)
        if label == "Search":
            return f"Search in {command_text}"
        return f"{label}  {command_text}"
    return f"Brief {_tool_command_summary(str(payload.get('command') or ''), width=52) or _tool_brief(payload)}"


def _render_grouped_tool_receipt(payloads: list[dict[str, Any]], family: str) -> str:
    first = payloads[0]
    heading_plain = "● Explored" if family == "explore" else _tool_heading_base(first)
    actor_text = _tool_actor_text(first, width=40)
    heading = heading_plain
    if _supports_tty_control():
        heading = f"{ANSI_BOLD}{_tool_heading_color(first)}{heading_plain}{ANSI_RESET}"
        if actor_text:
            heading += f"{ANSI_DIM}{ANSI_WHITE} · {actor_text}{ANSI_RESET}"
    elif actor_text:
        heading = f"{heading_plain} · {actor_text}"
    entry_width = max(18, _terminal_render_width() - 4)
    entries = [_tool_group_entry(payload, family) for payload in payloads]
    lines = [heading]
    for entry in entries:
        lines.append(_style_tool_line(f"└ {_middle_truncate_display(entry, entry_width)}", ANSI_WHITE, dim=True))
    return "\n".join(lines)


def _tool_brief(payload: dict[str, Any]) -> str:
    tool = str(payload.get("tool") or "")
    explicit = str(payload.get("brief") or "").strip()
    if explicit:
        return explicit
    command = _shorten_tool_text(str(payload.get("command") or "").strip())
    status = str(payload.get("status") or "").strip()

    if tool == "web_search":
        query = str(payload.get("query") or "").strip()
        return f"搜索 {query or 'query'}"
    if tool == "apply_patch":
        changed = payload.get("changed_files") or []
        if isinstance(changed, list) and changed:
            return f"修改 {changed[0]}{' 等' if len(changed) > 1 else ''}"
        return "应用补丁"
    if tool == "link":
        action = str(payload.get("action") or "continue").strip().lower()
        target = str(payload.get("target") or "").strip().lower()
        message = str(payload.get("message") or payload.get("task") or "").strip()
        head = f"{action} {target}".strip()
        if message:
            return f"{head} · {_middle_truncate_display(_shorten_tool_text(message), 36)}" if head else _middle_truncate_display(_shorten_tool_text(message), 44)
        return head or "X-Link"
    if tool == "update_plan":
        action = str(payload.get("action") or "status").strip().lower()
        mode = str(payload.get("mode") or "todo").strip().lower()
        title = str(payload.get("title") or "").strip()
        head = f"{mode}/{action}"
        return f"{head} · {_middle_truncate_display(_shorten_tool_text(title), 36)}" if title else head
    if tool == "model_mode":
        mode = str(payload.get("mode") or "").strip()
        action = str(payload.get("action") or "status").strip()
        return f"{action} {mode}".strip()
    if tool == "diary_keeper":
        date = str(payload.get("date") or "").strip()
        return f"更新日记 {date}".strip()
    if tool == "persona_link":
        action = str(payload.get("action") or "").strip().lower()
        if action == "switch":
            target = str(payload.get("target") or payload.get("speaker_mode") or "liaison").strip().lower()
            return "切换辅导位" if target == "liaison" else "切回主角色"
        if action == "mission":
            task = str(payload.get("task") or payload.get("message") or "").strip()
            return _middle_truncate_display(_shorten_tool_text(task), 44) if task else "mission"
        if action in {"send", "contact", "liaison"}:
            message = str(payload.get("message") or payload.get("question") or payload.get("prompt") or "").strip()
            if message:
                return _middle_truncate_display(_shorten_tool_text(message), 44)
            return action
        return "role link"
    if tool == "terminal":
        action = str(payload.get("action") or "start").strip()
        session = str(payload.get("session_name") or "").strip()
        return f"{action} {session}".strip()
    if tool == "aidebug":
        action = str(payload.get("action") or "status").strip()
        return f"aidebug {action}"
    if tool == "context":
        percent = _payload_percent(payload)
        try:
            turns = int(payload.get("turns_remaining") or payload.get("turns") or 1)
        except (TypeError, ValueError):
            turns = 1
        if percent >= 100:
            return "100% full"
        suffix = f"{turns} turns" if turns > 1 else "next turn"
        return f"{percent}% {suffix}".strip()
    if tool in {"memory_add", "memory_check", "memory_read", "memory_status"}:
        action = str(payload.get("action") or "").strip().lower()
        if tool == "memory_add":
            date = str(payload.get("date") or "").strip()
            keywords = payload.get("keywords") or []
            kw_text = f"{len(keywords)} kw" if isinstance(keywords, list) else "kw"
            return f"{date} · {kw_text}".strip(" ·")
        if tool == "memory_check":
            keywords = payload.get("keywords") or []
            return f"{len(keywords)} kw" if isinstance(keywords, list) else "memory_check"
        if tool == "memory_read":
            requested = payload.get("requested") or payload.get("dates") or []
            return f"{len(requested)} dates" if isinstance(requested, list) else "memory_read"
        if action == "clear_datememory":
            return "clear datememory"
        return "status"
    if tool == "liaison":
        rounds = payload.get("rounds")
        label = str(payload.get("liaison_name") or payload.get("name") or "liaison")
        if isinstance(rounds, int) and rounds > 0:
            return f"{label} · {rounds} rounds"
        return label
    if tool in {"context_manage", "contextmanage"}:
        mode = str(payload.get("mode") or "compact").strip()
        target = str(payload.get("target") or "both").strip()
        return f"{mode} {target}".strip()
    if tool == "tool_manage":
        action = str(payload.get("action") or "list").strip().lower()
        if action == "list":
            expanded = payload.get("expanded_count")
            total = payload.get("total_count")
            if isinstance(expanded, int) and isinstance(total, int) and total > 0:
                return f"{expanded}/{total} tools"
            if isinstance(total, int) and total > 0:
                return f"{total} tools"
            return "list"
        if action == "inspect":
            names = _tool_manage_name_list(payload.get("tools"))
            return f"inspect {', '.join(names[:3])}" if names else "inspect tool"
        if action in {"expand", "collapse"}:
            names = _tool_manage_name_list(payload.get("requested") or payload.get("changed") or payload.get("tools") or payload.get("tool"))
            return f"{action} {', '.join(names[:3])}" if names else action
        if action in {"expand_all", "collapse_all", "reset"}:
            return action.replace("_", " ")
        return action or "toolbox"
    if command:
        return _tool_command_summary(command, width=42)
    return status or "tool event"


def _tool_heading(payload: dict[str, Any]) -> str:
    base = _tool_heading_base(payload)
    brief = _tool_brief(payload)
    actor = _tool_actor_text(payload)
    parts = [part for part in (actor, brief) if part]
    if not _supports_tty_control():
        return f"{base} · {' · '.join(parts)}".rstrip()
    color = _tool_heading_color(payload)
    suffix = f" · {' · '.join(parts)}" if parts else ""
    return f"{ANSI_BOLD}{color}{base}{ANSI_RESET}{ANSI_DIM}{ANSI_WHITE}{suffix}{ANSI_RESET}"


def _tool_status_text(payload: dict[str, Any]) -> str:
    status = str(payload.get("status") or "").strip()
    if status in STATUS_SUCCESS_TEXT:
        return STATUS_SUCCESS_TEXT[status]
    if status == "pending_confirmation":
        return "Pending"
    if status in {"error", "blocked", "timeout", "rejected"}:
        return "Failed"
    return status.capitalize() if status else "Done"


def _tool_count_text(payload: dict[str, Any]) -> str:
    parts: list[str] = []
    stdout = str(payload.get("stdout") or "")
    stderr = str(payload.get("stderr") or "")
    patch = str(payload.get("patch") or "")
    tool = str(payload.get("tool") or "")
    lines = 0
    chars = 0
    if tool == "tool_manage":
        total = payload.get("total_count")
        expanded = payload.get("expanded_count")
        changed = payload.get("changed")
        tools = payload.get("tools")
        if isinstance(total, int) and total >= 0:
            parts.append(f"↗{total} tools")
        elif isinstance(tools, list) and tools:
            parts.append(f"↗{len(tools)} tools")
        if isinstance(expanded, int) and expanded >= 0:
            parts.append(f"{expanded} visible")
        if isinstance(changed, list) and changed:
            parts.append(f"{len(changed)} changed")
        return " · ".join(parts) if parts else "↗0 tools"
    if tool == "link":
        steps = payload.get("steps") or []
        if isinstance(steps, list) and steps:
            return f"↗{len(steps)} steps"
        action = str(payload.get("action") or "").strip().lower()
        if action == "switch":
            return "↗1 switch"
        return "↗1 link"
    if tool == "update_plan":
        total = payload.get("total_count")
        completed = payload.get("completed_count")
        if isinstance(total, int) and isinstance(completed, int):
            return f"↗{completed}/{total} steps"
        items = payload.get("items") or []
        if isinstance(items, list):
            return f"↗{len(items)} steps"
        return "↗0 steps"
    if tool == "model_mode":
        return "↗1 mode"
    if tool == "diary_keeper":
        keywords = payload.get("keywords") or []
        if isinstance(keywords, list):
            return f"↗{len(keywords)} kw"
        return "↗1 diary"
    if tool == "persona_link":
        action = str(payload.get("action") or "").strip().lower()
        if action == "switch":
            return "↗1 switch"
        if action == "mission":
            return "↗1 task"
        rounds = payload.get("rounds")
        if isinstance(rounds, int) and rounds > 0:
            parts.append(f"↗{rounds} rounds")
        transcript = payload.get("transcript") or []
        if isinstance(transcript, list) and transcript:
            parts.append(f"↗{len(transcript)} turns")
        return " · ".join(parts) if parts else "↗0 rounds"
    if tool == "liaison":
        rounds = payload.get("rounds")
        if isinstance(rounds, int) and rounds > 0:
            parts.append(f"↗{rounds} rounds")
        transcript = payload.get("transcript") or []
        if isinstance(transcript, list) and transcript:
            parts.append(f"↗{len(transcript)} turns")
        return " · ".join(parts) if parts else "↗0 rounds"
    if tool == "memory_add":
        keywords = payload.get("keywords") or []
        if isinstance(keywords, list):
            return f"↗{len(keywords)} kw"
        return "↗1 diary"
    if tool == "memory_check":
        result_count = payload.get("result_count")
        if isinstance(result_count, int):
            return f"↗{result_count} hits"
        return "↗0 hits"
    if tool == "memory_read":
        found = payload.get("found")
        if isinstance(found, int):
            return f"↗{found} entries"
        return "↗0 entries"
    if tool == "memory_status":
        days = payload.get("datememory_days")
        diaries = payload.get("memory_db_diaries")
        parts = []
        if isinstance(days, int):
            parts.append(f"↗{days} days")
        if isinstance(diaries, int):
            parts.append(f"↗{diaries} diaries")
        return " · ".join(parts) if parts else "↗0"
    if tool == "terminal":
        try:
            lines = int(payload.get("log_lines") or 0)
        except (TypeError, ValueError):
            lines = 0
        try:
            chars = int(payload.get("log_size") or payload.get("log_bytes") or 0)
        except (TypeError, ValueError):
            chars = 0
    elif patch:
        lines = _tool_line_count(patch)
        chars = len(patch)
    else:
        lines = _tool_line_count(stdout) + _tool_line_count(stderr)
        chars = len(stdout) + len(stderr)
    if lines:
        parts.append(f"↗{lines} Lines")
    if chars:
        parts.append(f"↗{chars} chars")
    return " · ".join(parts) if parts else "↗0 Lines · ↗0 chars"


def _tool_input_kind(payload: dict[str, Any]) -> str:
    tool = str(payload.get("tool") or "")
    if tool == "link":
        action = str(payload.get("action") or "").strip().lower()
        return f"X-Link/{action or 'continue'}"
    if tool == "update_plan":
        action = str(payload.get("action") or "status").strip().lower()
        mode = str(payload.get("mode") or "todo").strip().lower()
        return f"Plan/{mode}/{action}"
    if tool == "model_mode":
        action = str(payload.get("action") or "status").strip().lower()
        return f"模式/{action}"
    if tool == "diary_keeper":
        return "Diary"
    if tool == "persona_link":
        action = str(payload.get("action") or "").strip().lower()
        return f"Role/{action or 'link'}"
    if tool == "web_search":
        mode = str(payload.get("mode_used") or payload.get("mode") or "auto").strip()
        return mode.capitalize()
    if tool == "terminal":
        action = str(payload.get("action") or "start").strip()
        return f"Terminal/{action}"
    if tool == "aidebug":
        action = str(payload.get("action") or "status").strip()
        return f"Explore/{action}"
    if tool == "context":
        percent = _payload_percent(payload)
        return f"Context/{percent}%"
    if tool == "liaison":
        return "Liaison"
    if tool in {"memory_add", "memory_check", "memory_read", "memory_status", "diary_keeper"}:
        return "Memory"
    if tool in {"context_manage", "contextmanage"}:
        mode = str(payload.get("mode") or "compact").strip()
        target = str(payload.get("target") or "both").strip()
        return f"Context/{mode}/{target}"
    if tool == "tool_manage":
        action = str(payload.get("action") or "list").strip().lower()
        return f"Tool/{action}"
    channel = str(payload.get("channel") or "")
    if channel:
        return "Command" if channel == "Bash" else channel
    return tool or "Tool"


def _tool_input_text(payload: dict[str, Any]) -> str:
    tool = str(payload.get("tool") or "")
    if tool == "link":
        action = str(payload.get("action") or "").strip().lower()
        if action == "switch":
            target = str(payload.get("target") or payload.get("speaker_mode") or "liaison").strip()
            return f"target {target}"
        steps = payload.get("steps") or []
        message = str(payload.get("message") or payload.get("task") or "").strip()
        if isinstance(steps, list) and steps:
            step_text = "; ".join(str(item).strip() for item in steps[:3] if str(item).strip())
            return f"{message} · {step_text}" if message else step_text
        return message or str(payload.get("brief") or "X-Link")
    if tool == "update_plan":
        title = str(payload.get("title") or "").strip()
        active = payload.get("active_item") or {}
        if isinstance(active, dict):
            active_title = str(active.get("title") or "").strip()
            if active_title:
                return f"{title} · {active_title}" if title else active_title
        return title or str(payload.get("message") or "plan")
    if tool == "model_mode":
        mode = str(payload.get("mode") or "").strip()
        planner = str(payload.get("planner_model") or "").strip()
        executor = str(payload.get("executor_model") or "").strip()
        pair = f"{planner} -> {executor}".strip(" ->")
        return f"{mode} · {pair}".strip(" ·") or "status"
    if tool == "diary_keeper":
        return str(payload.get("date") or payload.get("message") or "diary")
    if tool == "persona_link":
        action = str(payload.get("action") or "").strip().lower()
        if action == "switch":
            target = str(payload.get("target") or payload.get("speaker_mode") or "liaison").strip()
            return f"target {target}"
        if action == "mission":
            task = str(payload.get("task") or payload.get("message") or "").strip()
            objective = str(payload.get("objective") or "").strip()
            return f"{task} · {objective}" if objective else task or "mission"
        if action in {"send", "contact", "liaison"}:
            return str(payload.get("message") or payload.get("question") or payload.get("prompt") or "—")
        return str(payload.get("message") or "—")
    if tool == "web_search":
        return str(payload.get("query") or "—")
    if tool == "apply_patch":
        changed = payload.get("changed_files") or []
        if isinstance(changed, list) and changed:
            return ", ".join(_shorten_tool_text(str(item)) for item in changed[:3])
        return str(payload.get("message") or "patch")
    if tool == "terminal":
        text = str(payload.get("command") or payload.get("session_name") or payload.get("message") or "—")
        return text
    if tool == "aidebug":
        return str(payload.get("relative_path") or payload.get("path") or payload.get("log_path") or payload.get("action") or "aidebug")
    if tool == "context":
        percent = _payload_percent(payload)
        try:
            turns = int(payload.get("turns_remaining") or payload.get("turns") or 1)
        except (TypeError, ValueError):
            turns = 1
        if percent >= 100:
            return f"ctx {percent}% · full"
        turns_text = f"{turns} turns" if turns > 1 else "next"
        return f"ctx {percent}% · {turns_text}"
    if tool == "liaison":
        return str(payload.get("question") or payload.get("prompt") or payload.get("message") or "—")
    if tool == "memory_add":
        return str(payload.get("date") or "—")
    if tool == "memory_check":
        keywords = payload.get("keywords") or []
        if isinstance(keywords, list):
            return ", ".join(_shorten_tool_text(str(item)) for item in keywords[:5])
        return str(payload.get("message") or "—")
    if tool == "memory_read":
        dates = payload.get("requested") or payload.get("dates") or []
        if isinstance(dates, list):
            return ", ".join(_shorten_tool_text(str(item)) for item in dates[:5])
        return str(payload.get("message") or "—")
    if tool == "memory_status":
        return str(payload.get("memory_dir") or payload.get("datememory_path") or "—")
    if tool in {"context_manage", "contextmanage"}:
        saved = str(payload.get("saved_chars") or "0")
        target = str(payload.get("target") or "both")
        recommendation = str(payload.get("recommendation") or "").strip()
        if recommendation:
            return f"{target} · saved {saved} chars · {recommendation}"
        return f"{target} · saved {saved} chars"
    if tool == "tool_manage":
        action = str(payload.get("action") or "list").strip().lower()
        if action == "list":
            total = payload.get("total_count")
            expanded = payload.get("expanded_count")
            if isinstance(expanded, int) and isinstance(total, int) and total > 0:
                return f"{expanded}/{total} visible"
            if isinstance(total, int) and total > 0:
                return f"{total} tools"
            return "toolbox"
        if action == "inspect":
            names = _tool_manage_name_list(payload.get("tools"))
            return ", ".join(names[:4]) if names else "tool"
        names = _tool_manage_name_list(payload.get("requested") or payload.get("changed") or payload.get("tools") or payload.get("tool"))
        return ", ".join(names[:4]) if names else action
    return str(payload.get("command") or payload.get("message") or "—")


def _tool_meta_line(label: str, *parts: str, color: str = ANSI_WHITE) -> str:
    body = " · ".join(str(part).strip() for part in parts if str(part).strip())
    text = f"✲{label} · {body}" if body else f"✲{label}"
    return _style_tool_line(text, color, bold=True)


def _tool_context_status_text(payload: dict[str, Any]) -> str:
    percent = _payload_percent(payload)
    try:
        turns = int(payload.get("turns_remaining") or payload.get("turns") or 1)
    except (TypeError, ValueError):
        turns = 1
    parts = [f"ctx {percent}%"]
    if percent >= 100:
        parts.append("full")
        return " · ".join(parts)
    turns_text = f"{turns} turns" if turns > 1 else "next"
    parts.append(turns_text)
    return " · ".join(parts)


def _should_suppress_tool_receipt(payload: dict[str, Any]) -> bool:
    if bool(payload.get("_frontend_rendered")):
        return True
    return str(payload.get("tool") or "") == "persona_handoff" and str(payload.get("status") or "") == "ok"


def _render_persona_chat_card(
    title: str,
    name: str,
    body: str,
    *,
    header_color: str,
    body_color: str,
    width: int,
) -> list[str]:
    header = f"╭─ {title} · {name}".rstrip(" ·")
    if _supports_tty_control():
        header = f"{ANSI_BOLD}{header_color}{header}{ANSI_RESET}"
    lines = [header]
    content = _shorten_tool_text(str(body or "").strip()) or "—"
    wrapped: list[str] = []
    for raw_line in content.splitlines() or ["—"]:
        wrapped.extend(_wrap_ansi_display(raw_line, max(16, width - 4)))
    if not wrapped:
        wrapped = ["—"]
    for line in wrapped:
        lines.append(_style_tool_line(f"│ {line}", body_color, dim=True))
    if _supports_tty_control():
        lines.append(f"{ANSI_DIM}{body_color}╰─{ANSI_RESET}")
    else:
        lines.append("╰─")
    return lines


def _render_liaison_receipt(payload: dict[str, Any]) -> str:
    width = max(24, _terminal_render_width() - 2)
    tool = str(payload.get("tool") or "")
    action = str(payload.get("action") or "").strip().lower()
    status = str(payload.get("status") or "").strip().lower()
    legacy_switch = tool == "persona_handoff"
    name = str(
        payload.get("liaison_name")
        or payload.get("speaker_name")
        or payload.get("name")
        or "辅导位"
    ).strip()
    if (tool == "persona_link" and action == "switch") or legacy_switch:
        target = str(payload.get("target") or payload.get("speaker_mode") or "liaison").strip().lower()
        speaker = str(payload.get("speaker_name") or name).strip()
        standby_name = str(
            payload.get("main_name") if target == "liaison" else payload.get("liaison_name") or "辅导位"
        ).strip()
        standby_role = "主角色" if target == "liaison" else "辅导位"
        speaker_role = "辅导位" if target == "liaison" else "主角色"
        note = str(payload.get("message") or "已切换当前说话者。").strip()
        context_percent = payload.get("context_percent")
        if context_percent not in {None, ""}:
            try:
                note += f"\nctx {max(0, min(100, int(context_percent)))}%"
            except (TypeError, ValueError):
                note += f"\nctx {context_percent}"
        speaker_block = _render_persona_chat_card(
            speaker_role,
            speaker,
            note,
            header_color=ANSI_GOLD,
            body_color=ANSI_GOLD,
            width=width,
        )
        standby_block = _render_persona_chat_card(
            standby_role,
            standby_name,
            "standby",
            header_color=ANSI_SOFT_BLUE,
            body_color=ANSI_SOFT_BLUE,
            width=width,
        )
        return "\n".join(speaker_block + [""] + standby_block)
    if tool == "persona_link" and action == "mission":
        mission_task = str(payload.get("task") or payload.get("message") or "").strip()
        objective = str(payload.get("objective") or "").strip()
        status_text = str(payload.get("mission_status") or status or "queued").strip() or "queued"
        main_name = str(payload.get("main_role") or payload.get("main_name") or "主角色").strip()
        liaison_name = str(payload.get("liaison_name") or "辅导位").strip()
        transcript = payload.get("transcript") or []
        lines: list[str] = []
        if isinstance(transcript, list) and transcript:
            ordered = sorted(
                [item for item in transcript if isinstance(item, dict)],
                key=lambda item: int(item.get("round") or 0),
                reverse=True,
            )
            for item in ordered:
                speaker = str(item.get("role") or "").strip()
                content = str(item.get("content") or "").strip()
                if not content:
                    continue
                round_label = str(item.get("round") or "").strip()
                title = "辅导位"
                color = ANSI_VIOLET
                if speaker == main_name:
                    title = "主角色"
                    color = ANSI_GOLD
                if round_label.isdigit():
                    title = f"{title} · 第{round_label}轮"
                lines.extend(
                    _render_persona_chat_card(
                        title,
                        speaker or (liaison_name if title.startswith("辅导位") else main_name),
                        content,
                        header_color=color,
                        body_color=color,
                        width=width,
                    )
                )
                lines.append("")
            if lines:
                return "\n".join(lines[:-1])
        liaison_body = f"{str(payload.get('message') or '任务已入队')}\n状态：{status_text}"
        if str(payload.get("mission_path") or "").strip():
            liaison_body += f"\n记录：{_shorten_tool_text(str(payload.get('mission_path') or ''))}"
        liaison_block = _render_persona_chat_card(
            "辅导位",
            liaison_name,
            liaison_body,
            header_color=ANSI_VIOLET,
            body_color=ANSI_VIOLET,
            width=width,
        )
        main_body = mission_task
        if objective:
            main_body = f"{main_body}\n目标：{objective}" if main_body else f"目标：{objective}"
        main_block = _render_persona_chat_card(
            "主角色",
            main_name,
            main_body or "—",
            header_color=ANSI_GOLD,
            body_color=ANSI_GOLD,
            width=width,
        )
        return "\n".join(liaison_block + [""] + main_block)
    if (tool == "persona_link" and action in {"send", "contact", "liaison"}) or tool == "liaison":
        label = "辅导位" if action == "liaison" else ("联系辅导位" if action == "contact" else "发送消息")
        main_name = str(payload.get("main_role") or payload.get("main_name") or "主角色").strip()
        liaison_name = str(payload.get("liaison_name") or name or "辅导位").strip()
        if tool == "liaison":
            request_text = str(payload.get("original_message") or "").strip()
        else:
            request_text = str(payload.get("original_message") or payload.get("message") or "").strip()
        reply_text = str(payload.get("reply") or "").strip()
        transcript = payload.get("transcript") or []
        lines: list[str] = []
        if transcript and isinstance(transcript, list):
            for item in transcript:
                if not isinstance(item, dict):
                    continue
                speaker = str(item.get("role") or liaison_name).strip() or liaison_name
                content = str(item.get("content") or "").strip()
                if not content:
                    continue
                round_label = str(item.get("round") or "").strip()
                color = ANSI_VIOLET
                title = "辅导位"
                if speaker == main_name:
                    title = "主角色"
                    color = ANSI_GOLD
                if round_label.isdigit():
                    title = f"{title} · 第{round_label}轮"
                lines.extend(
                    _render_persona_chat_card(
                        title,
                        speaker,
                        content,
                        header_color=color,
                        body_color=color,
                        width=width,
                    )
                )
                lines.append("")
            if request_text:
                lines.extend(
                    _render_persona_chat_card(
                        "主角色",
                        main_name,
                        request_text,
                        header_color=ANSI_GOLD,
                        body_color=ANSI_GOLD,
                        width=width,
                    )
                )
                lines.append("")
        else:
            if reply_text:
                lines.extend(
                    _render_persona_chat_card(
                        "辅导位",
                        liaison_name,
                        reply_text,
                        header_color=ANSI_VIOLET,
                        body_color=ANSI_VIOLET,
                        width=width,
                    )
                )
                lines.append("")
        if not lines and request_text:
            lines.extend(
                _render_persona_chat_card(
                    "主角色",
                    main_name,
                    request_text,
                    header_color=ANSI_GOLD,
                    body_color=ANSI_GOLD,
                    width=width,
                )
            )
        if not lines:
            lines.extend(
                _render_persona_chat_card(
                    label,
                    liaison_name,
                    str(payload.get("brief") or "已完成消息交换").strip(),
                    header_color=ANSI_VIOLET,
                    body_color=ANSI_VIOLET,
                    width=width,
                )
            )
        return "\n".join(lines)
    if tool == "persona_link":
        main_name = str(payload.get("main_role") or payload.get("main_name") or "主角色").strip()
        liaison_name = str(payload.get("liaison_name") or name or "辅导位").strip()
        status_text = "完成" if status in {"ok", "empty", "queued"} else _tool_status_text(payload)
        brief = str(payload.get("brief") or "已完成角色联动").strip()
        main_block = _render_persona_chat_card(
            "主角色",
            main_name,
            brief,
            header_color=ANSI_GOLD,
            body_color=ANSI_GOLD,
            width=width,
        )
        liaison_block = _render_persona_chat_card(
            "辅导位",
            liaison_name,
            f"状态：{status_text}",
            header_color=ANSI_VIOLET,
            body_color=ANSI_VIOLET,
            width=width,
        )
        return "\n".join(liaison_block + [""] + main_block)
    heading = f"● 辅导位 · {name}"
    if _supports_tty_control():
        heading = f"{ANSI_BOLD}{ANSI_CYAN}{heading}{ANSI_RESET}"
    status_text = "完成" if status in {"ok", "empty"} else _tool_status_text(payload)
    brief = str(payload.get("brief") or "已完成辅助判断").strip()
    lines = [heading, _tool_meta_line("结果", status_text, _middle_truncate_display(_shorten_tool_text(brief), max(16, width - 18)), color=ANSI_SOFT_PINK)]
    if status not in {"ok", "empty"}:
        message = str(payload.get("message") or "").strip()
        if message:
            lines.append(_tool_meta_line("提示", _middle_truncate_display(_shorten_tool_text(message), max(16, width - 12)), color=ANSI_SOFT_RED))
    return "\n".join(lines)


def _render_link_receipt(payload: dict[str, Any]) -> str:
    width = max(24, _terminal_render_width() - 2)
    action = str(payload.get("action") or "continue").strip().lower()
    heading = "● X-Link"
    if _supports_tty_control():
        heading = f"{ANSI_BOLD}{ANSI_VIOLET}{heading}{ANSI_RESET}"
    if action in {"switch", "liaison", "mission", "send", "contact"}:
        legacy_payload = dict(payload)
        legacy_payload["tool"] = "persona_link"
        rendered = _render_liaison_receipt(legacy_payload)
        return f"{heading}\n\n{rendered}" if rendered else heading

    def steps_text() -> str:
        raw_steps = payload.get("steps") or []
        if not isinstance(raw_steps, list):
            return ""
        steps = [str(item).strip() for item in raw_steps if str(item).strip()]
        if not steps:
            return ""
        return "\n".join(f"{index}. {step}" for index, step in enumerate(steps, start=1))

    def append_lines(*parts: str) -> str:
        return "\n".join(str(part).strip() for part in parts if str(part).strip())

    message = str(payload.get("message") or "").strip()
    task = str(payload.get("task") or "").strip()
    objective = str(payload.get("objective") or "").strip()
    context_percent = payload.get("context_percent")
    context_line = ""
    if context_percent not in {None, ""}:
        try:
            percent = max(0, min(100, int(context_percent)))
            context_line = f"ctx {percent}%"
        except (TypeError, ValueError):
            context_line = f"ctx {context_percent}"

    step_block = steps_text()
    main_name = str(payload.get("main_role") or payload.get("main_name") or "").strip()
    liaison_name = str(payload.get("liaison_name") or payload.get("name") or "").strip()
    top_title = "辅导位"
    top_name = liaison_name
    bottom_title = "主角色"
    bottom_name = main_name

    action_label = {
        "continue": "接续",
        "done": "完成",
        "blocked": "阻塞",
        "review": "审查",
        "ask": "询问",
        "handoff": "交还",
    }.get(action, action or "link")

    if action in {"done", "blocked", "review"}:
        top_body = append_lines(
            f"状态：{action_label}",
            message,
            f"任务：{task}" if task else "",
            f"目标：{objective}" if objective else "",
            "步骤：\n" + step_block if step_block else "",
            context_line,
        )
        bottom_hint = "等待主角色审查" if action == "done" else "需要主角色判断" if action == "blocked" else "审查记录已收到"
        bottom_body = append_lines(bottom_hint, f"目标：{objective}" if objective and not top_body else "")
    else:
        status_line = f"状态：{action_label}"
        if context_line:
            status_line = f"{status_line} · {context_line}"
        top_body = append_lines(
            status_line,
            f"目标：{objective}" if objective else "",
        )
        bottom_body = append_lines(
            message,
            f"任务：{task}" if task else "",
            f"目标：{objective}" if objective else "",
            "步骤：\n" + step_block if step_block else "",
        )

    top_block = _render_persona_chat_card(
        top_title,
        top_name,
        top_body or "已记录 X-Link。",
        header_color=ANSI_VIOLET,
        body_color=ANSI_VIOLET,
        width=width,
    )
    bottom_block = _render_persona_chat_card(
        bottom_title,
        bottom_name,
        bottom_body or "standby",
        header_color=ANSI_GOLD,
        body_color=ANSI_GOLD,
        width=width,
    )
    return "\n".join([heading, "", *top_block, "", *bottom_block])


def _render_memory_receipt(payload: dict[str, Any]) -> str:
    width = max(24, _terminal_render_width() - 2)
    tool = str(payload.get("tool") or "")
    action = str(payload.get("action") or "").strip().lower()
    if tool == "memory_add":
        heading = f"● Memory Add · {str(payload.get('date') or '—')}"
    elif tool == "diary_keeper":
        heading = f"● Diary Keeper · {str(payload.get('date') or '—')}"
    elif tool == "memory_check":
        heading = "● Memory Check"
    elif tool == "memory_read":
        heading = "● Memory Read"
    else:
        heading = "● Memory Status"
    if _supports_tty_control():
        heading = f"{ANSI_BOLD}{ANSI_SOFT_BLUE}{heading}{ANSI_RESET}"
    status_text = _tool_status_text(payload)
    brief = str(payload.get("brief") or "")
    if not brief:
        if tool == "memory_status":
            brief = "检查长期记忆状态"
        elif tool == "memory_add":
            brief = "写入长期记忆"
        elif tool == "memory_check":
            brief = "检索长期记忆"
        elif tool == "memory_read":
            brief = "按日期读取长期记忆"
    lines = [heading, _tool_meta_line("结果", status_text, _middle_truncate_display(_shorten_tool_text(brief), max(16, width - 18)), color=ANSI_SOFT_PINK)]
    if tool in {"memory_add", "diary_keeper"}:
        keywords = payload.get("keywords") or []
        if isinstance(keywords, list) and keywords:
            lines.append(_tool_meta_line("关键词", _middle_truncate_display(", ".join(_shorten_tool_text(str(item)) for item in keywords[:5]), max(16, width - 14)), color=ANSI_SOFT_BLUE))
        db_path = str(payload.get("db_path") or "").strip()
        if db_path:
            lines.append(_tool_meta_line("数据库", _middle_truncate_display(_shorten_tool_text(db_path), max(16, width - 14)), color=ANSI_SOFT_BLUE))
        if tool == "diary_keeper":
            lines.append(_tool_meta_line("来源", "datememory", "auto", color=ANSI_SOFT_BLUE))
    elif tool == "memory_check":
        dates = payload.get("dates") or []
        if isinstance(dates, list) and dates:
            lines.append(_tool_meta_line("日期", _middle_truncate_display(", ".join(_shorten_tool_text(str(item)) for item in dates[:5]), max(16, width - 14)), color=ANSI_SOFT_BLUE))
        best = payload.get("best_detail") or payload.get("best") or {}
        if isinstance(best, dict) and best:
            lines.append(_tool_meta_line("命中", _middle_truncate_display(_shorten_tool_text(str(best.get("date") or "")), max(16, width - 14)), color=ANSI_SOFT_BLUE))
    elif tool == "memory_read":
        requested = payload.get("requested") or []
        if isinstance(requested, list) and requested:
            lines.append(_tool_meta_line("读取", _middle_truncate_display(", ".join(_shorten_tool_text(str(item)) for item in requested[:5]), max(16, width - 14)), color=ANSI_SOFT_BLUE))
    else:
        lines.append(_tool_meta_line("日记", _middle_truncate_display(_shorten_tool_text(str(payload.get("datememory_bytes") or "")), max(16, width - 14)), color=ANSI_SOFT_BLUE))
        lines.append(_tool_meta_line("状态", _middle_truncate_display(_shorten_tool_text(f"{payload.get('datememory_days', 0)} days / {payload.get('memory_db_diaries', 0)} diaries"), max(16, width - 14)), color=ANSI_SOFT_BLUE))
    message = str(payload.get("message") or "").strip()
    if message and status_text not in {"完成", "Done", "Succeeded"}:
        lines.append(_tool_meta_line("提示", _middle_truncate_display(_shorten_tool_text(message), max(16, width - 14)), color=ANSI_SOFT_RED))
    return "\n".join(lines)


def _render_model_mode_receipt(payload: dict[str, Any]) -> str:
    heading = _tool_heading(payload)
    mode = _collab_mode_value(str(payload.get("mode") or "standard").strip())
    previous = _collab_mode_value(str(payload.get("previous_mode") or "").strip()) if str(payload.get("previous_mode") or "").strip() else ""
    planner = str(payload.get("planner_model") or "").strip()
    executor = str(payload.get("executor_model") or "").strip()
    action = str(payload.get("action") or "status").strip().lower()
    status_text = "已切换" if action == "set" and str(payload.get("status") or "") in STATUS_SUCCESS_TEXT else "当前"
    body = f"{status_text}：{_collab_mode_detail(mode)}"
    if previous and previous != mode:
        body += f"  ({_collab_mode_detail(previous)} -> {_collab_mode_detail(mode)})"
    lines = [heading, _style_tool_line(f"  {body}", ANSI_SOFT_BLUE, dim=True)]
    if planner or executor:
        lines.append(_style_tool_line(f"  规划位 {planner or '?'}  /  执行位 {executor or '?'}", ANSI_SOFT_BLUE, dim=True))
    reason = str(payload.get("reason") or "").strip()
    if reason:
        lines.append(_style_tool_line(f"  {reason}", ANSI_WHITE, dim=True))
    return "\n".join(lines)


def _plan_item_symbol(status: str) -> str:
    return {
        "done": "●",
        "in_progress": "●",
        "blocked": "!",
        "pending": "○",
    }.get(str(status or "").strip().lower(), "○")


def _plan_item_color(status: str) -> tuple[str, bool, bool]:
    normalized = str(status or "").strip().lower()
    if normalized == "done":
        return ANSI_SOFT_GREEN, True, False
    if normalized == "in_progress":
        return ANSI_SOFT_BLUE, True, False
    if normalized == "blocked":
        return ANSI_SOFT_RED, True, False
    return ANSI_MUTED_TEXT, False, True


def _render_update_plan_receipt(payload: dict[str, Any]) -> str:
    heading = _tool_heading(payload)
    width = max(24, _terminal_render_width() - 2)
    mode = str(payload.get("mode") or "todo").strip().lower()
    action = str(payload.get("action") or "status").strip().lower()
    title = _shorten_tool_text(str(payload.get("title") or "").strip())
    plan_status = str(payload.get("plan_status") or "").strip().lower()
    try:
        completed = int(payload.get("completed_count") or 0)
        total = int(payload.get("total_count") or 0)
    except (TypeError, ValueError):
        completed = 0
        total = 0
    mode_label = "TODO" if mode == "todo" else "PLAN"
    status_label = {
        "empty": "empty",
        "pending": "pending",
        "in_progress": "active",
        "blocked": "blocked",
        "done": "done",
    }.get(plan_status, plan_status or action)

    lines = [heading]
    meta_parts = [
        _style_badge(mode_label, color=ANSI_CTX_FG),
        _style_badge(status_label, color=ANSI_MUTED_TEXT),
    ]
    if total:
        meta_parts.append(_style_badge(f"{completed}/{total}", color=ANSI_SOFT_GREEN if completed == total else ANSI_SOFT_BLUE))
    meta_line = "  " + " ".join(meta_parts)
    if title:
        meta_line += f"  {_style_tool_line(_middle_truncate_display(title, max(12, width - 34)), ANSI_WHITE, bold=True)}"
    lines.append(meta_line.rstrip())

    items = payload.get("items") or []
    if not isinstance(items, list):
        items = []
    visible_items = [item for item in items if isinstance(item, dict)][:8]
    if not visible_items:
        message = str(payload.get("message") or "当前没有计划步骤。").strip()
        lines.append(_style_tool_line(f"  {message}", ANSI_MUTED_TEXT, dim=True))
    for item in visible_items:
        status = str(item.get("status") or "pending").strip().lower()
        symbol = _plan_item_symbol(status)
        color, bold, dim = _plan_item_color(status)
        item_id = str(item.get("id") or "").strip()
        phase = str(item.get("phase") or "").strip()
        title_text = _shorten_tool_text(str(item.get("title") or item.get("note") or "—").strip())
        lead = phase or item_id
        prefix = f"{symbol} {lead} · " if lead else f"{symbol} "
        available = max(10, width - _display_width(prefix) - 3)
        lines.append(_style_tool_line(f"  {prefix}{_middle_truncate_display(title_text, available)}", color, bold=bold, dim=dim))
        note = _shorten_tool_text(str(item.get("note") or "").strip())
        if note and status in {"in_progress", "blocked"}:
            lines.append(_style_tool_line(f"    {_middle_truncate_display(note, max(10, width - 6))}", color, dim=True))
    if len(items) > len(visible_items):
        lines.append(_style_tool_line(f"  ... +{len(items) - len(visible_items)} steps", ANSI_MUTED_TEXT, dim=True))
    next_text = _shorten_tool_text(str(payload.get("next") or "").strip())
    if next_text:
        lines.append(_style_tool_line(f"  next · {_middle_truncate_display(next_text, max(10, width - 10))}", ANSI_MUTED_BLUE, dim=True))
    message = str(payload.get("message") or "").strip()
    if message and visible_items:
        lines.append(_style_tool_line(f"  {_middle_truncate_display(_shorten_tool_text(message), max(10, width - 4))}", ANSI_MUTED_TEXT, dim=True))
    return "\n".join(lines)


def _render_compact_tool_receipt(payload: dict[str, Any]) -> str:
    if _should_suppress_tool_receipt(payload):
        return ""
    tool = str(payload.get("tool") or "")
    if tool == "link":
        return _render_link_receipt(payload)
    if tool in {"persona_link", "liaison"}:
        return _render_liaison_receipt(payload)
    if tool == "model_mode":
        return _render_model_mode_receipt(payload)
    if tool == "update_plan":
        return _render_update_plan_receipt(payload)
    if tool in {"memory_add", "memory_check", "memory_read", "memory_status", "diary_keeper"}:
        return _render_memory_receipt(payload)
    heading_text = _tool_heading(payload)
    width = max(24, _terminal_render_width() - 2)
    input_text = _middle_truncate_display(_shorten_tool_text(_tool_input_text(payload)), max(16, width - 24))
    output_parts = [_tool_status_text(payload), _tool_count_text(payload)]
    message = str(payload.get("message") or "").strip()
    if message and str(payload.get("status") or "") not in {"ok", "empty"}:
        output_parts.append(_middle_truncate_display(_shorten_tool_text(message), max(16, width - 30)))
    lines = [heading_text, ""]
    if str(payload.get("tool") or "") == "context":
        lines.append(_tool_meta_line("CTX", _middle_truncate_display(_shorten_tool_text(_tool_context_status_text(payload)), max(16, width - 12)), color=ANSI_CYAN))
        lines.append("")
    lines.extend(
        [
            _tool_meta_line("INPUT", _tool_input_kind(payload), input_text, color=ANSI_MUTED_BLUE),
            _tool_meta_line("OUTPUT", *output_parts, color=ANSI_SOFT_PINK),
        ]
    )
    if str(payload.get("status") or "") == "pending_confirmation":
        confirm_token = str(payload.get("confirm_command") or "y").strip() or "y"
        deny_token = str(payload.get("deny_command") or "n").strip() or "n"
        lines.append("")
        lines.append(_tool_meta_line("CONFIRM", f"type {confirm_token} to run", f"{deny_token} to cancel", color=ANSI_SOFT_RED))
    warnings = payload.get("warnings") or []
    if isinstance(warnings, list) and warnings:
        lines.append("")
        for warning in warnings[:4]:
            lines.append(_tool_meta_line("WARN", _middle_truncate_display(_shorten_tool_text(str(warning)), max(16, width - 14)), color=ANSI_SOFT_RED))
    recovery_hint = payload.get("recovery_hint") or []
    if isinstance(recovery_hint, list) and recovery_hint:
        lines.append("")
        for hint in recovery_hint[:3]:
            lines.append(_tool_meta_line("HINT", _middle_truncate_display(_shorten_tool_text(str(hint)), max(16, width - 14)), color=ANSI_MUTED_BLUE))
    return "\n".join(lines)


def _render_tool_running_receipt(payload: dict[str, Any]) -> str:
    running_payload = dict(payload)
    running_payload["status"] = "running"
    heading_text = _tool_heading(running_payload)
    return "\n".join([heading_text, _tool_meta_line("OUTPUT", "Running", color=ANSI_SOFT_PINK)])


def _tool_body_preview(payload: dict[str, Any]) -> tuple[str, str]:
    status = str(payload.get("status") or "")
    if status == "pending_confirmation":
        return "brief", str(payload.get("reason") or "该命令需要确认后执行。")
    if status == "blocked":
        return "brief", str(payload.get("reason") or payload.get("message") or "该命令已被安全策略阻止。")
    if status == "rejected":
        return "brief", str(payload.get("message") or "已取消执行。")

    if str(payload.get("tool") or "") == "terminal":
        parts: list[str] = []
        summary = str(payload.get("summary") or "").strip()
        if summary:
            parts.append(summary)
        message = str(payload.get("message") or "").strip()
        if message:
            parts.append(message)
        setup_warning = str(payload.get("setup_warning") or "").strip()
        if setup_warning:
            parts.append(setup_warning)
        session_name = str(payload.get("session_name") or "").strip()
        if session_name:
            parts.append(f"session {session_name}")
        log_path = str(payload.get("log_path") or "").strip()
        if log_path:
            parts.append(
                f"log {log_path} ({payload.get('log_lines', 0)} lines, {payload.get('log_size') or payload.get('log_bytes', 0)})"
            )
            parts.append(f"head {payload.get('read_head_command')}")
            parts.append(f"tail {payload.get('read_tail_command')}")
            parts.append(f"slice {payload.get('read_slice_command')}")
        preview = str(payload.get("log_preview") or "").strip()
        if preview:
            parts.append(f"[log preview]\n{preview}")
        if not parts:
            parts.append("terminal completed with no log output.")
        return "out", _shorten_tool_text("\n".join(parts))

    if str(payload.get("tool") or "") == "aidebug":
        parts: list[str] = []
        summary = str(payload.get("summary") or "").strip()
        if summary:
            parts.append(summary)
        action = str(payload.get("action") or "").strip()
        if action:
            parts.append(f"action {action}")
        aidebug_dir = str(payload.get("aidebug_dir") or "").strip()
        if aidebug_dir:
            parts.append(f"dir {aidebug_dir}")
        relative_path = str(payload.get("relative_path") or payload.get("log_path") or "").strip()
        if relative_path:
            parts.append(f"path {relative_path}")
        stdout = str(payload.get("stdout") or "").strip()
        if stdout:
            parts.append(stdout)
        message = str(payload.get("message") or "").strip()
        if message:
            parts.append(message)
        if not parts:
            parts.append("aidebug completed with no output.")
        return "dbg", _shorten_tool_text("\n".join(parts))

    if str(payload.get("tool") or "") == "tool_manage":
        action = str(payload.get("action") or "list").strip().lower()
        parts: list[str] = []
        summary = str(payload.get("summary") or "").strip()
        if summary:
            parts.append(summary)
        total = payload.get("total_count")
        expanded = payload.get("expanded_count")
        changed = payload.get("changed")
        if action == "list":
            if isinstance(expanded, int) and isinstance(total, int) and total > 0:
                parts.append(f"{expanded}/{total} visible")
            elif isinstance(total, int) and total > 0:
                parts.append(f"{total} tools")
        elif action == "inspect":
            rows = payload.get("tools") or []
            if isinstance(rows, list):
                preview: list[str] = []
                for item in rows[:4]:
                    if isinstance(item, dict):
                        name = str(item.get("name") or "").strip()
                        if not name:
                            continue
                        state = "expanded" if item.get("expanded") else "collapsed"
                        preview.append(f"{name} ({state})")
                    else:
                        text = str(item or "").strip()
                        if text:
                            preview.append(text)
                if preview:
                    parts.append(", ".join(preview))
        else:
            names = _tool_manage_name_list(changed or payload.get("requested") or payload.get("tools") or payload.get("tool"))
            if names:
                parts.append(", ".join(names[:4]))
        message = str(payload.get("message") or "").strip()
        if message:
            parts.append(message)
        if not parts:
            parts.append("toolbox completed with no output.")
        return "box", _shorten_tool_text("\n".join(parts))

    if str(payload.get("tool") or "") == "liaison":
        parts: list[str] = []
        summary = str(payload.get("summary") or "").strip()
        if summary:
            parts.append(summary)
        reply = str(payload.get("reply") or payload.get("message") or "").strip()
        if reply:
            parts.append(reply)
        transcript = payload.get("transcript") or []
        if isinstance(transcript, list) and transcript:
            preview: list[str] = []
            for item in transcript[:3]:
                if not isinstance(item, dict):
                    continue
                content = str(item.get("content") or "").strip()
                if content:
                    preview.append(content)
            if preview:
                parts.append("\n".join(preview))
        if not parts:
            parts.append("liaison completed with no output.")
        return "liaison", _shorten_tool_text("\n".join(parts))

    stdout = str(payload.get("stdout") or "").strip()
    stderr = str(payload.get("stderr") or "").strip()
    parts: list[str] = []
    summary = str(payload.get("summary") or "").strip()
    if summary:
        parts.append(summary)
    if status == "timeout":
        parts.append(f"timeout after {payload.get('timeout_seconds', '?')}s")
    elif status == "error" and payload.get("returncode") is not None:
        parts.append(f"returncode {payload.get('returncode')}")

    if stdout:
        parts.append(stdout)
    if stderr:
        parts.append(f"[stderr]\n{stderr}")
    if not parts:
        parts.append("completed with no output.")
    return "out", _shorten_tool_text("\n".join(parts))


def _tool_body_counter(payload: dict[str, Any], body_label: str, body_text: str) -> str:
    tool = str(payload.get("tool") or "")
    if tool == "terminal":
        try:
            count = int(payload.get("log_lines") or 0)
        except (TypeError, ValueError):
            count = 0
        return f"TOTAL {count} lines" if count > 0 else ""
    if tool == "aidebug":
        stdout = str(payload.get("stdout") or "").strip()
        count = _tool_line_count(stdout) or _tool_line_count(body_text)
        return f"TOTAL {count} lines" if count > 0 else ""
    if body_label == "out":
        stdout = str(payload.get("stdout") or "").strip()
        stderr = str(payload.get("stderr") or "").strip()
        count = _tool_line_count(stdout) + _tool_line_count(stderr)
        if count <= 0:
            count = _tool_line_count(body_text)
        return f"TOTAL {count} lines" if count > 0 else ""
    count = _tool_line_count(body_text)
    return f"TOTAL {count} lines" if count > 0 else ""


def _tool_prefix(label: str, *, first: bool, counter: str = "") -> str:
    if not first:
        return "        \t"
    suffix = f" {counter}" if counter else ""
    symbol = "✲" if label == "CMD" else "○"
    plain = f"  {symbol} {label}{suffix}\t"
    if not _supports_tty_control():
        return plain
    color = ANSI_WHITE
    if label == "CMD":
        color = ANSI_MUTED_BLUE
    elif label == "OUT":
        color = ANSI_SOFT_PINK
    elif label == "DBG":
        color = ANSI_SOFT_BLUE
    elif label == "BOX":
        color = ANSI_CYAN
    elif label == "BRIEF":
        color = f"{ANSI_DIM}{ANSI_WHITE}"
        return f"  {color}{symbol} {label}{suffix}{ANSI_RESET}\t"
    return f"  {ANSI_BOLD}{color}{symbol} {label}{suffix}{ANSI_RESET}\t"


def _render_patch_diff_lines(patch_text: str, *, width: int) -> list[str]:
    lines: list[str] = []
    old_line: int | None = None
    new_line: int | None = None
    for raw_line in str(patch_text or "").splitlines():
        line = raw_line.rstrip("\n")
        if line.startswith(("diff --git ", "index ", "--- ", "+++ ")):
            continue
        if line.startswith("@@"):
            match = re.search(r"-(\d+)(?:,\d+)? \+(\d+)", line)
            if match:
                old_line = int(match.group(1))
                new_line = int(match.group(2))
            continue
        if not line:
            continue
        marker = line[:1]
        body = line[1:] if marker in {" ", "+", "-"} else line
        if marker == "+" and not line.startswith("+++"):
            number = new_line or 0
            new_line = number + 1 if new_line is not None else None
            rendered = f"{number:>5} + {_middle_truncate_display(body, max(8, width - 9))}"
            lines.append(_style_tool_line(rendered, ANSI_SOFT_PINK, bold=True))
        elif marker == "-" and not line.startswith("---"):
            number = old_line or 0
            old_line = number + 1 if old_line is not None else None
            rendered = f"{number:>5} - {_middle_truncate_display(body, max(8, width - 9))}"
            lines.append(_style_tool_line(rendered, ANSI_SOFT_RED, bold=True))
        elif marker == " ":
            number = new_line if new_line is not None else old_line or 0
            if old_line is not None:
                old_line += 1
            if new_line is not None:
                new_line += 1
            rendered = f"{number:>5}   {_middle_truncate_display(body, max(8, width - 9))}"
            lines.append(_style_tool_line(rendered, ANSI_WHITE, dim=True))
        else:
            lines.append(_style_tool_line(_middle_truncate_display(line, width), ANSI_WHITE, dim=True))
        if len(lines) >= 80:
            lines.append(_style_tool_line("... patch preview truncated ...", ANSI_SOFT_RED, bold=True))
            break
    return lines or [_style_tool_line("  patch preview unavailable", ANSI_WHITE, dim=True)]


def _render_apply_patch_receipt(payload: dict[str, Any]) -> str:
    heading_text = _tool_heading(payload)
    width = max(24, _terminal_render_width() - 2)
    changed = payload.get("changed_files") or []
    changed_paths = [str(item) for item in changed] if isinstance(changed, list) else []
    lines = [heading_text, ""]
    if changed_paths:
        for path in changed_paths[:8]:
            lines.append(_tool_meta_line("Edited", _middle_truncate_display(_shorten_tool_text(path), max(16, width - 14)), color=ANSI_MAGENTA))
    else:
        lines.append(_tool_meta_line("Edited", "patch", color=ANSI_MAGENTA))
    lines.append(_tool_meta_line("OUTPUT", _tool_status_text(payload), _tool_count_text(payload), color=ANSI_SOFT_PINK))
    patch_text = str(payload.get("patch") or "")
    if patch_text:
        lines.append("")
        lines.extend(_render_patch_diff_lines(patch_text, width=width))
    else:
        stdout = str(payload.get("stdout") or payload.get("message") or "").strip()
        if stdout:
            lines.append("")
            lines.extend(_tool_preview_lines(_shorten_tool_text(stdout), width=width))
    warnings = payload.get("warnings") or []
    if isinstance(warnings, list) and warnings:
        lines.append("")
        for warning in warnings[:4]:
            lines.append(_tool_meta_line("WARN", _middle_truncate_display(_shorten_tool_text(str(warning)), max(16, width - 14)), color=ANSI_SOFT_RED))
    recovery_hint = payload.get("recovery_hint") or []
    if isinstance(recovery_hint, list) and recovery_hint:
        lines.append("")
        for hint in recovery_hint[:3]:
            lines.append(_tool_meta_line("HINT", _middle_truncate_display(_shorten_tool_text(str(hint)), max(16, width - 14)), color=ANSI_MUTED_BLUE))
    return "\n".join(lines)


def _render_tool_receipt_payload(payload: dict[str, Any]) -> str:
    status = str(payload.get("status") or "")
    tool = str(payload.get("tool") or "")
    if _should_suppress_tool_receipt(payload):
        return ""
    if tool == "apply_patch":
        return _render_apply_patch_receipt(payload)
    if tool in {"web_search", "command", "terminal", "aidebug", "context", "context_manage", "contextmanage", "tool_manage", "link", "liaison", "persona_link", "memory_add", "memory_check", "memory_read", "memory_status", "diary_keeper", "model_mode", "update_plan"} and _tool_group_family(payload) is None:
        return _render_compact_tool_receipt(payload)
    width = max(24, _terminal_render_width() - 8)
    heading_text = _tool_heading(payload)
    command_lines = _tool_preview_lines(_shorten_tool_text(str(payload.get("command") or "")), width=width, max_lines=3)
    body_label, body_text = _tool_body_preview(payload)
    body_lines = _tool_preview_lines(body_text, width=width)
    body_counter = _tool_body_counter(payload, body_label, body_text)

    lines = [heading_text]
    for index, line in enumerate(command_lines):
        prefix = _tool_prefix("CMD", first=index == 0)
        lines.append(f"{prefix}{_style_tool_omission(line)}")
    for index, line in enumerate(body_lines):
        label = body_label.upper()
        prefix = _tool_prefix(label, first=index == 0, counter=body_counter if index == 0 else "")
        lines.append(f"{prefix}{_style_tool_omission(line)}")

    if status == "pending_confirmation":
        confirm_token = str(payload.get("confirm_command") or "y").strip() or "y"
        lines.append(f"  type {confirm_token} = run  ·  n = cancel")

    return "\n".join(lines)


def _render_tool_receipts(tool_traces: tuple[dict[str, Any], ...]) -> str:
    payloads: list[dict[str, Any]] = []
    for trace in tool_traces:
        payload = trace.get("result") if isinstance(trace, dict) else None
        if isinstance(payload, dict):
            payloads.append(payload)
    blocks: list[str] = []
    grouped_payloads: list[dict[str, Any]] = []
    grouped_family: str | None = None

    def flush_group() -> None:
        nonlocal grouped_payloads, grouped_family
        if not grouped_payloads:
            return
        if (len(grouped_payloads) == 1 and grouped_family != "explore") or grouped_family is None:
            blocks.append(_render_tool_receipt_payload(grouped_payloads[0]))
        else:
            blocks.append(_render_grouped_tool_receipt(grouped_payloads, grouped_family))
        grouped_payloads = []
        grouped_family = None

    for payload in payloads:
        if _should_suppress_tool_receipt(payload):
            continue
        family = _tool_group_family(payload)
        if family:
            if grouped_payloads and family != grouped_family:
                flush_group()
            grouped_payloads.append(payload)
            grouped_family = family
            continue
        flush_group()
        blocks.append(_render_tool_receipt_payload(payload))
    flush_group()
    return "\n\n".join(blocks).strip()


def dispatch_shell_input(
    raw_input: str,
    *,
    mode: str = "command_not_found",
    cwd: str | Path | None = None,
    config: ProjectLingConfig | None = None,
    dry_run: bool = False,
) -> int:
    config = config or load_config()
    _cleanup_legacy_runtime(config)

    text = raw_input.strip()
    if not text:
        return 0

    normalized_mode = mode.strip().lower()
    if normalized_mode not in SHELL_DISPATCH_MODES:
        normalized_mode = "command_not_found"
    if normalized_mode == "command_not_found" and re.fullmatch(r"\d{1,3}", text):
        return 0

    engine = ProjectLingEngine(config)
    role, _role_seed, persona_bundle = engine.persona_for_dispatch_mode(normalized_mode)
    current_cwd = Path(cwd or Path.cwd()).expanduser()
    allow_tools = bool(config.allow_tools)
    use_stream = bool(config.enable_sse)
    route = engine.preview_route(text, allow_tools=allow_tools, dispatch_mode=normalized_mode)
    if bool(route.get("speaker_handoff_request")) and allow_tools:
        target = str(route.get("speaker_handoff_target") or "").strip().lower()
        if target in {"liaison", "main"}:
            role, _role_seed, persona_bundle = engine.persona_for_handoff_target(target)

    if dry_run:
        payload = {
            "raw": text,
            "mode": normalized_mode,
            "cwd": str(current_cwd),
            "role": {
                "display_zh": persona_bundle.main.name_zh,
                "display_en": persona_bundle.main.name_en,
            },
            "liaison": {
                "display_zh": persona_bundle.liaison.name_zh if persona_bundle.liaison else "",
                "display_en": persona_bundle.liaison.name_en if persona_bundle.liaison else "",
            },
            "route": route,
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    if not config.api_key:
        config = _bootstrap_missing_key(config) or config
        if not config.api_key:
            return 0
        engine = ProjectLingEngine(config)
        role, _role_seed, persona_bundle = engine.persona_for_dispatch_mode(normalized_mode)
        allow_tools = bool(config.allow_tools)
        use_stream = bool(config.enable_sse)
        route = engine.preview_route(text, allow_tools=allow_tools, dispatch_mode=normalized_mode)
        if bool(route.get("speaker_handoff_request")) and allow_tools:
            target = str(route.get("speaker_handoff_target") or "").strip().lower()
            if target in {"liaison", "main"}:
                role, _role_seed, persona_bundle = engine.persona_for_handoff_target(target)

    printer = ShellStreamPrinter(
        engine.prompt_bundle,
        role,
        persona_bundle=persona_bundle,
        context_budget=load_context_budget(config),
    )
    initial_status = "thinking" if bool(route.get("thinking_enabled")) else "responding"
    printer.begin(initial_status)

    try:
        result = engine.chat(
            text,
            cwd=current_cwd,
            mode=normalized_mode,
            allow_tools=allow_tools,
            stream=use_stream,
            on_stream_delta=printer.on_delta if use_stream else None,
            on_stream_event=printer.on_event,
        )
    except KeyboardInterrupt:
        printer.emit_message("已中断。")
        printer.finish("")
        return 130
    except DeepSeekAPIError as exc:
        printer.emit_message(f"请求失败：{exc}")
        printer.finish("")
        return 1
    except Exception as exc:  # pragma: no cover - shell safety net
        printer.emit_message(f"运行失败：{exc}")
        printer.finish("")
        return 1

    streamed_response = bool(
        isinstance(result.raw_response, dict)
        and result.raw_response.get("_projectling_streamed")
    )
    for trace in result.thinking_traces:
        if not isinstance(trace, dict):
            continue
        if bool(trace.get("_frontend_rendered")):
            continue
        trace_text = str(trace.get("text") or "").strip()
        if not trace_text:
            continue
        if streamed_response and trace_text == str(result.reasoning_text or "").strip():
            continue
        printer.show_thinking_trace(
            trace_text,
            elapsed_seconds=trace.get("elapsed_seconds"),
        )
    if not use_stream:
        frontend_receipts = _render_tool_receipts(result.tool_traces)
        if frontend_receipts:
            printer.emit_plain_block(frontend_receipts, trailing_blank=bool(result.text))
        if result.text:
            printer.emit_message(result.text)
    if result.finish_reason == "stream_limit" and not result.text and not result.tool_traces:
        printer.finish("本轮输出已达到上限。")
    else:
        printer.finish(result.text or "我没有得到有效回复。")
    return 0


def _run_api_settings_ui() -> int:
    while True:
        current = load_config()
        _render_api_settings(current)
        choice = _prompt_line("> ").strip()

        if choice == "1":
            print("")
            key = _prompt_line("输入 API Key，留空保持原样 > ").strip()
            if key:
                _save_config_value(current, {"DEEPSEEK_API_KEY": key})
                print("API key 已写入并立即生效。")
            else:
                print("未输入内容，保持原样。")
            continue

        if choice == "2":
            base_url = _prompt_optional_text("输入 Base URL，留空保持原样 > ")
            if base_url is not None:
                _save_config_value(current, {"DEEPSEEK_BASE_URL": base_url})
                print(f"Base URL 已更新：{base_url}")
            else:
                print("未输入内容，保持原样。")
            continue

        if choice == "3":
            print("")
            _toggle_config_value(
                current,
                "DEEPSEEK_ENABLE_SSE",
                current.enable_sse,
                "SSE",
            )
            continue

        if choice == "4":
            print("")
            max_tokens = _prompt_int("输入 Max Tokens > ", min_value=1, allow_empty_clear=True)
            if max_tokens == "":
                _save_config_value(current, {"DEEPSEEK_MAX_TOKENS": None})
                print("Max Tokens 已恢复自动。")
            elif isinstance(max_tokens, int):
                _save_config_value(current, {"DEEPSEEK_MAX_TOKENS": str(max_tokens)})
                print(f"Max Tokens 已更新：{max_tokens}")
            continue

        if choice == "5":
            print("")
            temperature = _prompt_float("输入 Temperature (0.0 - 2.0) > ", min_value=0.0, max_value=2.0)
            if temperature is not None:
                _save_config_value(current, {"DEEPSEEK_TEMPERATURE": f"{temperature:g}"})
                print(f"Temperature 已更新：{temperature:g}")
            continue

        if choice == "6":
            print("")
            print("这是 API 超时时间，默认 180s。SSE 会在此基础上自动放宽读超时。")
            timeout_seconds = _prompt_float("输入 Timeout 秒数 > ", min_value=5.0, max_value=86400.0)
            if timeout_seconds is not None:
                _save_config_value(current, {"DEEPSEEK_TIMEOUT_SECONDS": f"{timeout_seconds:g}"})
                print(f"Timeout 已更新：{timeout_seconds:g}s")
            continue

        if choice == "7":
            print("")
            print("超时或连接建立失败时会用同一份上下文重试，不会把失败请求追加进历史。最大 10 次。")
            retries = _prompt_int("输入 Retry 次数 > ", min_value=0)
            if isinstance(retries, int):
                if retries > 10:
                    print("输入无效，需要小于等于 10。")
                else:
                    _save_config_value(current, {"DEEPSEEK_RETRY_COUNT": str(retries)})
                    print(f"Retry 已更新：{retries}")
            continue

        if choice == "8":
            _run_api_test(current)
            continue

        if choice == "9":
            _run_websearch_settings_ui()
            continue

        if choice == "0" or not choice:
            return 0

        print("无效输入。")


def _run_system_settings_ui() -> int:
    while True:
        current = load_config()
        _render_system_settings(current)
        choice = _prompt_line("> ").strip()

        if choice == "1":
            print("")
            print("角色停留时间支持 1 - 48 小时。")
            role_hours = _prompt_int("输入角色停留小时数 > ", min_value=1)
            if isinstance(role_hours, int):
                if role_hours > 48:
                    print("输入无效，需要小于等于 48。")
                else:
                    _save_config_value(current, {"PROJECTLING_ROLE_TTL_HOURS": str(role_hours)})
                    print(f"角色停留时间已更新：{role_hours}h")
            continue

        if choice == "2":
            print("")
            _run_model_mode_ui()
            continue

        if choice == "0" or not choice:
            return 0

        print("无效输入。")


def run_settings_ui(config: ProjectLingConfig | None = None, *, tab: str = "root") -> int:
    config = config or load_config()
    _cleanup_legacy_runtime(config)
    normalized_tab = (tab or "root").strip().lower()
    if normalized_tab in {"api", "deepseek"}:
        return _run_api_settings_ui()
    if normalized_tab in {"persona"}:
        return _run_persona_settings_ui(config)
    if normalized_tab in {"system", "settings"}:
        return _run_system_settings_ui()

    while True:
        current = load_config()
        _render_settings_root(current)
        choice = _prompt_line("> ").strip()

        if choice == "1":
            _run_api_settings_ui()
            continue

        if choice == "2":
            _run_persona_settings_ui(current)
            continue

        if choice == "3":
            _run_system_settings_ui()
            continue

        if choice == "0" or not choice:
            print("设置完成。")
            return 0

        print("无效输入。")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="projectling")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("doctor", help="print config and runtime status")
    selftest = sub.add_parser("selftest", help="run offline release smoke tests")
    selftest.add_argument("--json", action="store_true", help="print structured selftest result")

    chat = sub.add_parser("chat", help="send one message to DeepSeek")
    chat.add_argument("--message", required=True, help="user message")
    chat.add_argument("--cwd", default=".", help="shell working directory")
    chat.add_argument("--mode", default="chat", choices=sorted(SHELL_DISPATCH_MODES))
    chat.add_argument("--no-tools", action="store_true", help="disable local tool calls")
    chat.add_argument("--stream", action="store_true", help="stream response to stdout")
    chat.add_argument("--json", action="store_true", help="print structured result")

    model = sub.add_parser("model", help="switch collaboration mode")
    model.add_argument("mode", nargs="*", default=[], help="rapid / standard / precise, or 1 / 2 / 3")
    mode = sub.add_parser("mode", help="switch collaboration mode")
    mode.add_argument("mode", nargs="*", default=[], help="rapid / standard / precise, or 1 / 2 / 3")

    sub.add_parser("help", help="show the compact command list")
    sub.add_parser("codexurl", help="open the codexurl proxy menu")

    card = sub.add_parser("render-motd-card", help="render the launcher card text")
    card.add_argument("--width", type=int, default=80, help="terminal width")
    card.add_argument("--seed", type=int, default=None, help="fixed seed for deterministic output")
    card.add_argument("--max-lines", type=int, default=None, help="limit card output height")
    card.add_argument("--settings-label", default="输入 0 进入设置", help="settings hint text")
    card.add_argument("--reroll", action="store_true", help="pick a new launcher role before rendering")

    anim = sub.add_parser("animate-motd-card", help="render card animation frames separated by form-feed")
    anim.add_argument("--width", type=int, default=80, help="terminal width")
    anim.add_argument("--seed", type=int, default=None, help="fixed seed for deterministic output")
    anim.add_argument("--frames", type=int, default=8, help="frame count")
    anim.add_argument("--reroll", action="store_true", help="pick a new launcher role before animating")
    anim.add_argument("--final-card", action="store_true", help="append the final launcher card after animation")
    anim.add_argument("--max-lines", type=int, default=None, help="limit final card output height")
    anim.add_argument("--settings-label", default="输入 0 进入设置", help="settings hint text for final card")

    roster = sub.add_parser("show-roster", help="print roster entries")
    roster.add_argument("--json", action="store_true", help="print as json")

    tools = sub.add_parser("show-tools", help="print available tool-call schemas")
    tools.add_argument("--json", action="store_true", help="print raw api tool schema")

    pending = sub.add_parser("show-pending-command", help="show current pending command approval request")
    pending.add_argument("--json", action="store_true", help="print as json")

    confirm = sub.add_parser("confirm-command", help="execute current pending command after typing y or yes")
    confirm.add_argument("answer", nargs="?", default="", help="confirmation text, usually y or yes")
    confirm.add_argument("--json", action="store_true", help="print as json")

    deny = sub.add_parser("deny-command", help="reject current pending command")
    deny.add_argument("--json", action="store_true", help="print as json")

    sub.add_parser("has-pending-command", help="exit 0 when a pending command approval exists")

    reroll = sub.add_parser("reroll-role", help="force pick a new launcher role")
    reroll.add_argument("--json", action="store_true", help="print as json")

    shell_settings = sub.add_parser("shell-settings", help="interactive shell settings menu")
    shell_settings.add_argument(
        "--tab",
        default="root",
        choices=("root", "api", "deepseek", "persona", "system", "settings"),
        help="open a settings section directly",
    )

    shell_dispatch = sub.add_parser("shell-dispatch", help="dispatch one zsh input to DeepSeek")
    shell_dispatch.add_argument("--raw", required=True, help="raw input text")
    shell_dispatch.add_argument("--cwd", default=".", help="shell working directory")
    shell_dispatch.add_argument(
        "--mode",
        default="command_not_found",
        choices=sorted(SHELL_DISPATCH_MODES),
        help="dispatch context",
    )
    shell_dispatch.add_argument(
        "--dry-run",
        action="store_true",
        help="print shell-dispatch routing without calling the API",
    )
    return parser


def _cmd_doctor() -> int:
    config = load_config()
    _cleanup_legacy_runtime(config)
    scrub_volatile_memory_entries(config)
    ensure_memory_layout(config)
    engine = ProjectLingEngine(config)
    prompt_bundle = engine.prompt_bundle
    active_role, _role_seed = resolve_current_role(config)
    persona_bundle = resolve_persona_bundle(config, role=active_role, seed=_role_seed)
    roster = load_roster(config)
    active_persona_path = persona_path_for_role(config, active_role)
    liaison_persona_path = persona_path_for_role(config, persona_bundle.liaison) if persona_bundle.liaison is not None else None
    shared_entries_path = context_entries_status(config).get("entries_path")
    external_context = load_external_context(config, role=active_role)
    active_context = load_role_context(config, role=active_role)
    liaison_context = load_role_context(config, role=persona_bundle.liaison) if persona_bundle.liaison is not None else ""

    def file_text_chars(path: Path | None) -> int:
        if path is None or not path.is_file():
            return 0
        try:
            return len(path.read_text(encoding="utf-8"))
        except OSError:
            return 0
    planner_model, executor_model = _collab_mode_models(config.collab_mode)

    payload = {
        "root_dir": str(config.root_dir),
        "config_dir": str(config.config_dir),
        "context_dir": str(config.context_dir),
        "runtime_dir": str(config.runtime_dir),
        "api_key_configured": bool(config.api_key),
        "base_url": config.base_url,
        "collab_mode": config.collab_mode,
        "planner_model": planner_model,
        "executor_model": executor_model,
        "temperature": config.temperature,
        "max_tokens": config.max_tokens,
        "enable_sse": config.enable_sse,
        "thinking_control": "collab_mode",
        "retry_count": config.retry_count,
        "full_context_mode": config.full_context_mode,
        "context_mode": config.context_mode,
        "websearch_summary_key_configured": bool(config.websearch_summary_key),
        "websearch_web_key_configured": bool(config.websearch_web_key),
        "websearch_endpoint": config.websearch_endpoint,
        "allow_tools": config.allow_tools,
        "timeout_seconds": config.timeout_seconds,
        "role_ttl_hours": config.role_ttl_hours,
        "max_tool_rounds": config.max_tool_rounds,
        "context_max_chars": config.context_max_chars,
        "context_compact_target_chars": config.context_compact_target_chars,
        "contextmanage_context_max_chars": config.advisorling_context_max_chars,
        "contextmanage_context_max_tokens": config.advisorling_context_max_tokens,
        "contextmanage_compact_target_chars": config.advisorling_compact_target_chars,
        "prompt_path": str(prompt_bundle.path),
        "shared_entries_path": str(shared_entries_path or ""),
        "shared_entries_chars": len(external_context),
        "active_context_chars": len(active_context),
        "legacy_external_context_path": str(active_persona_path),
        "legacy_external_context_chars": file_text_chars(active_persona_path),
        "liaison_context_path": str(liaison_persona_path or ""),
        "liaison_context_chars": len(liaison_context),
        "liaison_legacy_context_chars": file_text_chars(liaison_persona_path),
        "role_context_chars": len(active_context),
        "context_entries": context_entries_status(config),
        "persona_display_zh": persona_bundle.main.name_zh,
        "persona_display_en": persona_bundle.main.name_en,
        "persona_liaison_display_zh": persona_bundle.liaison.name_zh if persona_bundle.liaison else "",
        "persona_liaison_display_en": persona_bundle.liaison.name_en if persona_bundle.liaison else "",
        "persona_liaison": persona_bundle.liaison_label,
        "persona_source": persona_bundle.source,
        "memory": memory_status(config),
        "roster_path": str(config.roster_path),
        "roster_entries": len(roster),
        "active_role": f"{active_role.name_zh} / {active_role.name_en}",
        "legacy_runtime_files": [
            name for name in LEGACY_RUNTIME_FILES if (config.runtime_dir / name).exists()
        ],
        "legacy_root_runtime_files": [
            name for name in LEGACY_ROOT_RUNTIME_FILES if (config.root_dir / name).exists()
        ],
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def _selftest_record(results: list[dict[str, Any]], name: str, ok: bool, detail: str = "", *, skipped: bool = False) -> None:
    results.append(
        {
            "name": name,
            "status": "skip" if skipped else "ok" if ok else "fail",
            "detail": detail,
        }
    )


def _selftest_run_command(
    results: list[dict[str, Any]],
    name: str,
    command: list[str],
    *,
    input_text: str | None = None,
    env: dict[str, str] | None = None,
    timeout: int = 20,
) -> None:
    try:
        completed = subprocess.run(
            command,
            cwd=str(PROJECTLING_DIR),
            input=input_text,
            text=True,
            capture_output=True,
            timeout=timeout,
            env=env,
            check=False,
        )
    except FileNotFoundError:
        _selftest_record(results, name, False, f"command not found: {command[0]}")
        return
    except subprocess.TimeoutExpired:
        _selftest_record(results, name, False, f"timeout after {timeout}s")
        return
    output = (completed.stderr or completed.stdout or "").strip().splitlines()
    detail = output[0][:240] if output else f"rc={completed.returncode}"
    _selftest_record(results, name, completed.returncode == 0, detail)


def _cmd_selftest(args: argparse.Namespace) -> int:
    results: list[dict[str, Any]] = []

    python_files = ["core.py", "projectling.py", "tooling.py", "__init__.py"]
    _selftest_run_command(results, "python syntax", [sys.executable, "-m", "py_compile", *python_files])
    _selftest_run_command(results, "run.sh syntax", ["bash", "-n", "run.sh"])
    _selftest_run_command(results, "projectling.zsh syntax", ["zsh", "-n", "projectling.zsh"])

    optional_shell_files = [
        PROJECTLING_DIR.parent / "Quickinstall" / "deploy" / "termux" / "motd.sh",
        PROJECTLING_DIR.parent / "Quickinstall" / "deploy" / "aitermux" / "bootstrap.sh",
        PROJECTLING_DIR.parent / "Quickinstall" / "deploy" / "aitermux" / "zshrc.autostart.zsh",
    ]
    for path in optional_shell_files:
        if not path.is_file():
            _selftest_record(results, f"{path.name} syntax", True, "file not present", skipped=True)
            continue
        shell = "zsh" if path.suffix == ".zsh" else "bash"
        _selftest_run_command(results, f"{path.name} syntax", [shell, "-n", str(path)])

    try:
        config = load_config()
        engine = ProjectLingEngine(config)
        roster = load_roster(config)
        _selftest_record(results, "config load", bool(config.root_dir and config.config_dir), str(config.root_dir))
        _selftest_record(results, "roster load", len(roster) > 0, f"{len(roster)} roles")
        planner_model, executor_model = _collab_mode_models(config.collab_mode)
        _selftest_record(
            results,
            "mode mapping",
            planner_model in {"deepseek-chat", "deepseek-reasoner"} and executor_model in {"deepseek-chat", "deepseek-reasoner"},
            f"{config.collab_mode}: {planner_model}+{executor_model}",
        )
        mode_ok = (
            _collab_mode_value("1") == "rapid"
            and _collab_mode_value("2") == "standard"
            and _collab_mode_value("3") == "precise"
        )
        _selftest_record(results, "mode aliases", mode_ok, "1/2/3")

        tools = engine.registry.schemas()
        names = [str((item.get("function") or {}).get("name") or "") for item in tools]
        required_tools = {"link", "update_plan", "model_mode", "contextmanage", "apply_patch", "command"}
        _selftest_record(results, "tool schemas", required_tools.issubset(set(names)), ", ".join(names[:8]))
        apply_schema = next((item for item in tools if str((item.get("function") or {}).get("name") or "") == "apply_patch"), None)
        apply_params = ((apply_schema or {}).get("function") or {}).get("parameters") or {}
        apply_props = apply_params.get("properties") or {}
        apply_anyof = apply_params.get("anyOf") or []
        apply_ok = "operation" in apply_props and "edits" in apply_props and {"required": ["operation"]} in apply_anyof
        _selftest_record(results, "apply_patch structured schema", apply_ok, "operation/edits")

        casual = engine.preview_route("你好", dispatch_mode="chat")
        task = engine.preview_route("请帮我写一个网页版贪吃蛇，单文件 index.html", dispatch_mode="chat")
        route_ok = (
            casual.get("model") == engine._planner_model_for_mode(config.collab_mode)
            and bool(casual.get("thinking_enabled")) == ("reasoner" in str(casual.get("model")))
            and bool(task.get("tools_enabled"))
            and task.get("tool_scope") == "plan_gate"
            and bool(task.get("plan_required"))
        )
        _selftest_record(
            results,
            "routing policy",
            route_ok,
            f"casual={casual.get('model')} task={task.get('tool_scope')}/{task.get('task_complexity')}",
        )
    except Exception as exc:
        _selftest_record(results, "config/schema/routing", False, str(exc))

    try:
        with tempfile.TemporaryDirectory(prefix="projectling-selftest-") as tmp:
            root = Path(tmp)
            cfg = SimpleNamespace(
                root_dir=root,
                config_dir=root / "config",
                context_dir=root / "context",
                context_entries_path=root / "context" / "entries.jsonl",
                runtime_dir=root / "config",
            )
            context = ToolContext(cwd=root, home=Path.home(), config=cfg)
            write_result = _execute_apply_patch_tool(
                {"operation": "write", "target_file": "app/index.html", "content": "<html>A</html>", "brief": "selftest write"},
                context,
            )
            replace_result = _execute_apply_patch_tool(
                {"operation": "replace", "target_file": "app/index.html", "find": "A", "replace": "B", "brief": "selftest replace"},
                context,
            )
            edits_result = _execute_apply_patch_tool(
                {
                    "target_file": "app/index.html",
                    "edits": [
                        {"operation": "insert_after", "find": "B", "content": "C"},
                        {"operation": "append", "content": "<!-- tail -->"},
                    ],
                    "brief": "selftest edits",
                },
                context,
            )
            escape_result = _execute_apply_patch_tool(
                {"operation": "write", "target_file": "../escape.txt", "content": "x", "brief": "selftest escape"},
                context,
            )
            app_text = (root / "app" / "index.html").read_text(encoding="utf-8")
            apply_ok = (
                write_result.get("status") == "ok"
                and replace_result.get("status") == "ok"
                and edits_result.get("status") == "ok"
                and "BC" in app_text
                and "<!-- tail -->" in app_text
                and escape_result.get("status") == "blocked"
            )
            _selftest_record(results, "apply_patch execution", apply_ok, f"mode={edits_result.get('mode_used')}")

            plan_start = _execute_update_plan_tool(
                {
                    "action": "start",
                    "mode": "todo",
                    "title": "selftest",
                    "items": [{"id": "T1", "title": "step", "status": "in_progress"}],
                },
                context,
            )
            plan_done = _execute_update_plan_tool({"action": "complete", "step_id": "T1"}, context)
            _selftest_record(
                results,
                "update_plan execution",
                plan_start.get("status") == "ok" and plan_done.get("status") == "ok",
                str(plan_done.get("message") or ""),
            )
            context_status = _execute_contextmanage_tool({"mode": "status"}, context)
            _selftest_record(results, "contextmanage execution", context_status.get("status") == "ok", str(context_status.get("message") or ""))
    except Exception as exc:
        _selftest_record(results, "tool execution", False, str(exc))

    try:
        with tempfile.TemporaryDirectory(prefix="projectling-logtest-") as tmp:
            root = Path(tmp)
            aidebug = root / "aidebug"
            logs = aidebug / "logs"
            logs.mkdir(parents=True, exist_ok=True)
            (logs / "startup.log").write_text("S" * 9000, encoding="utf-8")
            (logs / "projectling.log").write_text("P" * 9000, encoding="utf-8")
            old_tmp = aidebug / "tmp" / "old.tmp"
            recent_tmp = aidebug / "tmp" / "recent.tmp"
            old_terminal = aidebug / "projectling" / "terminal output" / "old.log"
            notes_keep = aidebug / "notes" / "keep.md"
            for path, text in ((old_tmp, "old"), (recent_tmp, "recent"), (old_terminal, "old"), (notes_keep, "keep")):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text, encoding="utf-8")
            old_time = time.time() - 3 * 86400
            os.utime(old_tmp, (old_time, old_time))
            os.utime(old_terminal, (old_time, old_time))
            env = dict(os.environ)
            env.update(
                {
                    "AITERMUX_AIDEBUG_DIR": str(aidebug),
                    "AITERMUX_LOG_CLEAN_INTERVAL_SECONDS": "0",
                    "AITERMUX_STARTUP_LOG_MAX_KB": "4",
                    "AITERMUX_STARTUP_LOG_KEEP_KB": "2",
                    "AITERMUX_PROJECTLING_LOG_MAX_KB": "4",
                    "AITERMUX_PROJECTLING_LOG_KEEP_KB": "2",
                    "AITERMUX_TMP_LOG_KEEP_DAYS": "1",
                    "AITERMUX_TERMINAL_LOG_KEEP_DAYS": "1",
                }
            )
            completed = subprocess.run(
                ["bash", str(PROJECTLING_DIR / "run.sh"), "doctor"],
                cwd=str(PROJECTLING_DIR),
                env=env,
                text=True,
                capture_output=True,
                timeout=20,
                check=False,
            )
            log_ok = (
                completed.returncode == 0
                and (logs / "startup.log").stat().st_size < 4096
                and (logs / "projectling.log").stat().st_size < 4096
                and not old_tmp.exists()
                and recent_tmp.exists()
                and not old_terminal.exists()
                and notes_keep.exists()
            )
            _selftest_record(results, "log housekeeping", log_ok, f"rc={completed.returncode}")
    except Exception as exc:
        _selftest_record(results, "log housekeeping", False, str(exc))

    _selftest_run_command(results, "cleanup command", ["bash", "run.sh", "cleanup"], timeout=10)
    _selftest_run_command(results, "settings root exits", [sys.executable, "core.py", "shell-settings"], input_text="0\n", timeout=10)
    _selftest_run_command(results, "settings system exits", [sys.executable, "core.py", "shell-settings", "--tab", "system"], input_text="0\n", timeout=10)
    _selftest_run_command(results, "motd render", [sys.executable, "core.py", "render-motd-card", "--width", "80", "--max-lines", "12"], timeout=10)

    total = len(results)
    failed = [item for item in results if item["status"] == "fail"]
    skipped = [item for item in results if item["status"] == "skip"]
    passed = total - len(failed) - len(skipped)
    score = int(round((passed + len(skipped) * 0.5) * 100 / max(1, total)))
    payload = {
        "status": "ok" if not failed else "fail",
        "score": score,
        "passed": passed,
        "failed": len(failed),
        "skipped": len(skipped),
        "total": total,
        "results": results,
    }
    if getattr(args, "json", False):
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(f"ProjectLing selftest: {payload['status']} · score {score}% · {passed}/{total} passed")
        for item in results:
            marker = "✓" if item["status"] == "ok" else "-" if item["status"] == "skip" else "✗"
            detail = f" · {item['detail']}" if item.get("detail") else ""
            print(f"{marker} {item['name']}{detail}")
    return 0 if not failed else 1


def _cmd_chat(args: argparse.Namespace) -> int:
    config = load_config()
    _cleanup_legacy_runtime(config)
    engine = ProjectLingEngine(config)
    role, sequence_seed, persona_bundle = engine.persona_for_dispatch_mode(args.mode)
    current_cwd = Path(args.cwd).expanduser()
    allow_tools = not args.no_tools
    route = engine.preview_route(args.message, allow_tools=allow_tools, dispatch_mode=args.mode)
    if bool(route.get("speaker_handoff_request")) and allow_tools:
        target = str(route.get("speaker_handoff_target") or "").strip().lower()
        if target in {"liaison", "main"}:
            target_role, _target_seed, target_bundle = engine.persona_for_handoff_target(target)
            role = target_role
            persona_bundle = target_bundle

    if args.stream and not args.json:
        printer = ShellStreamPrinter(
            engine.prompt_bundle,
            role,
            persona_bundle=persona_bundle,
            context_budget=load_context_budget(config),
        )
        printer.begin("thinking" if bool(route.get("thinking_enabled")) else "responding")
        try:
            result = engine.chat(
                args.message,
                cwd=current_cwd,
                mode=args.mode,
                allow_tools=allow_tools,
                stream=True,
                on_stream_delta=printer.on_delta,
                on_stream_event=printer.on_event,
            )
        except KeyboardInterrupt:
            printer.emit_message("已中断。")
            printer.finish("")
            return 130
        except Exception as exc:  # pragma: no cover - CLI safety net
            printer.emit_message(f"运行失败：{exc}")
            printer.finish("")
            return 1
        if not result.text and not result.tool_traces:
            if result.finish_reason == "stream_limit":
                printer.finish("本轮输出已达到上限。")
            else:
                printer.finish("我没有得到有效回复。")
        else:
            printer.finish(result.text or "")
        return 0

    result = engine.chat(
        args.message,
        cwd=current_cwd,
        mode=args.mode,
        allow_tools=allow_tools,
    )
    if args.json:
        print(
            json.dumps(
                {
                    "text": result.text,
                    "reasoning_text": result.reasoning_text,
                    "rounds": result.rounds,
                    "used_tools": result.used_tools,
                    "thinking_traces": list(result.thinking_traces),
                    "tool_traces": list(result.tool_traces),
                    "finish_reason": result.finish_reason,
                    "routing": result.routing,
                    "persona": {
                        "display_zh": (result.persona_bundle or persona_bundle).main.name_zh,
                        "display_en": (result.persona_bundle or persona_bundle).main.name_en,
                        "liaison_display_zh": (result.persona_bundle or persona_bundle).liaison.name_zh if (result.persona_bundle or persona_bundle).liaison else "",
                        "liaison_display_en": (result.persona_bundle or persona_bundle).liaison.name_en if (result.persona_bundle or persona_bundle).liaison else "",
                        "liaison": (result.persona_bundle or persona_bundle).liaison_label,
                        "source": (result.persona_bundle or persona_bundle).source,
                    },
                    "role": {
                        "rarity": result.role.rarity,
                        "name_zh": result.role.name_zh,
                        "name_en": result.role.name_en,
                    },
                    "raw_response": result.raw_response,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    receipts = _render_tool_receipts(result.tool_traces)
    if receipts and result.text:
        print(f"{receipts}\n\n{result.text}")
    elif receipts:
        print(receipts)
    else:
        print(result.text)
    return 0


def _cmd_model(args: argparse.Namespace) -> int:
    config = load_config()
    _cleanup_legacy_runtime(config)
    raw_mode = getattr(args, "mode", "")
    if isinstance(raw_mode, list):
        raw_mode = raw_mode[0] if raw_mode else ""
    return _run_model_mode_ui(str(raw_mode or ""))


def _cmd_render_motd_card(args: argparse.Namespace) -> int:
    config = load_config()
    _cleanup_legacy_runtime(config)
    persona_bundle = resolve_persona_bundle(config)
    if args.reroll:
        role, sequence_seed = reroll_active_role(config)
        remaining_text = _format_remaining_text(_remaining_seconds_for_role(config, role))
        persona_bundle = resolve_persona_bundle(config, role=role, seed=sequence_seed)
    elif args.seed is None:
        role, sequence_seed = resolve_current_role(config)
        remaining_text = _format_remaining_text(_remaining_seconds_for_role(config, role))
        persona_bundle = resolve_persona_bundle(config, role=role, seed=sequence_seed)
    else:
        role, sequence_seed = resolve_active_role(config, seed=args.seed)
        remaining_text = None
        persona_bundle = resolve_persona_bundle(config, role=role, seed=sequence_seed)

    for line in render_motd_card(
        args.width,
        role,
        seed=sequence_seed,
        remaining_text=remaining_text,
        settings_label=args.settings_label,
        max_lines=args.max_lines,
        persona_bundle=persona_bundle,
    ):
        print(line)
    return 0


def _cmd_animate_motd_card(args: argparse.Namespace) -> int:
    config = load_config()
    _cleanup_legacy_runtime(config)
    if args.reroll:
        final_role, sequence_seed = reroll_active_role(config)
        persona_bundle = resolve_persona_bundle(config, role=final_role, seed=sequence_seed)
        sequence, final_role, sequence_seed = build_roll_sequence(
            config,
            frames=args.frames,
            final_role=final_role,
            sequence_seed=sequence_seed,
        )
    else:
        sequence, final_role, sequence_seed = build_roll_sequence(config, seed=args.seed, frames=args.frames)
        persona_bundle = resolve_persona_bundle(config, role=final_role, seed=sequence_seed)
    total_frames = max(1, len(sequence))
    animation_sequence = sequence[:-1] if args.final_card and len(sequence) > 1 else sequence
    for index, role in enumerate(animation_sequence):
        is_final_animation_frame = role.name_en == final_role.name_en and index == len(animation_sequence) - 1
        frame_bundle = (
            persona_bundle
            if is_final_animation_frame
            else resolve_persona_bundle(config, role=role, seed=sequence_seed + index)
        )
        for line in render_animation_frame(
            args.width,
            role,
            frame_index=index,
            total_frames=total_frames,
            persona_bundle=frame_bundle,
        ):
            print(line)
        if index != len(animation_sequence) - 1 or args.final_card:
            print("\f", flush=True)
    if args.final_card:
        remaining_text = _format_remaining_text(_remaining_seconds_for_role(config, final_role))
        for line in render_motd_card(
            args.width,
            final_role,
            seed=sequence_seed,
            remaining_text=remaining_text,
            settings_label=args.settings_label,
            max_lines=args.max_lines,
            persona_bundle=persona_bundle,
        ):
            print(line)
    sys.stdout.flush()
    return 0


def _cmd_show_roster(args: argparse.Namespace) -> int:
    config = load_config()
    _cleanup_legacy_runtime(config)
    roster = load_roster(config)
    if args.json:
        print(
            json.dumps(
                [
                    {
                        "rarity": role.rarity,
                        "name_zh": role.name_zh,
                        "name_en": role.name_en,
                        "quote": role.quote,
                        "profile": role.profile,
                    }
                    for role in roster
                ],
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    for index, role in enumerate(roster, start=1):
        print(f"{index:02d}. [{role.rarity}] {role.name_zh} / {role.name_en} :: {role.profile}")
    return 0


def _cmd_show_tools(args: argparse.Namespace) -> int:
    config = load_config()
    _cleanup_legacy_runtime(config)
    registry = ProjectLingEngine(config).registry
    schemas = registry.schemas()

    if args.json:
        print(json.dumps(schemas, ensure_ascii=False, indent=2))
        return 0

    for index, tool in enumerate(schemas, start=1):
        fn = tool.get("function") or {}
        print(f"{index:02d}. {fn.get('name', 'unknown')} :: {fn.get('description', '')}")
    return 0


def _cmd_help() -> int:
    _render_command_help()
    return 0


def _cmd_codexurl() -> int:
    runner = shutil.which("codexurl")
    if runner is None:
        print("未找到 codexurl 命令。")
        return 0
    try:
        completed = subprocess.run([runner], check=False)
    except KeyboardInterrupt:
        return 130
    return int(completed.returncode)


def _print_command_control_payload(payload: dict[str, Any], *, as_json: bool) -> int:
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    status = str(payload.get("status") or "unknown")
    if status == "empty":
        print(str(payload.get("message") or "当前没有待确认命令。"))
        return 0

    if status in {"pending_confirmation", "rejected", "ok", "error", "timeout", "blocked"}:
        print(_render_tool_receipt_payload(payload))
        return 0

    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def _cmd_show_pending_command(args: argparse.Namespace) -> int:
    config = load_config()
    _cleanup_legacy_runtime(config)
    payload = show_pending_command(config)
    return _print_command_control_payload(payload, as_json=args.json)


def _cmd_confirm_command(args: argparse.Namespace) -> int:
    config = load_config()
    _cleanup_legacy_runtime(config)
    if args.json:
        payload = confirm_pending_command(config, answer=args.answer)
        return _print_command_control_payload(payload, as_json=True)

    engine = ProjectLingEngine(config)
    persona_bundle = engine.current_persona()
    role = persona_bundle.main
    printer = ShellStreamPrinter(
        engine.prompt_bundle,
        role,
        persona_bundle=persona_bundle,
        show_role_heading=False,
        context_budget=load_context_budget(config),
    )
    payload = confirm_pending_command(config, answer=args.answer, event_callback=printer.on_event)
    status = str(payload.get("status") or "")
    if status in {"ok", "error", "timeout"}:
        if printer.line_open:
            printer._write("\n")
            printer.line_open = False
        return 0
    return _print_command_control_payload(payload, as_json=args.json)


def _cmd_deny_command(args: argparse.Namespace) -> int:
    config = load_config()
    _cleanup_legacy_runtime(config)
    payload = reject_pending_command(config)
    return _print_command_control_payload(payload, as_json=args.json)


def _cmd_has_pending_command() -> int:
    config = load_config()
    _cleanup_legacy_runtime(config)
    payload = show_pending_command(config)
    return 0 if str(payload.get("status") or "") == "pending_confirmation" else 1


def _cmd_reroll_role(args: argparse.Namespace) -> int:
    config = load_config()
    _cleanup_legacy_runtime(config)
    role, sequence_seed = reroll_active_role(config)
    payload = {
        "name_zh": role.name_zh,
        "name_en": role.name_en,
        "rarity": role.rarity,
        "sequence_seed": sequence_seed,
    }
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    print(f"{role.name_zh} / {role.name_en}")
    return 0


def _cmd_shell_settings(args: argparse.Namespace) -> int:
    return run_settings_ui(tab=args.tab)


def _cmd_shell_dispatch(args: argparse.Namespace) -> int:
    return dispatch_shell_input(
        args.raw,
        mode=args.mode,
        cwd=Path(args.cwd).expanduser(),
        dry_run=bool(getattr(args, "dry_run", False)),
    )


def main(argv: list[str] | None = None) -> int:
    try:
        signal.signal(signal.SIGPIPE, signal.SIG_DFL)
    except (AttributeError, ValueError):
        pass
    parser = _build_parser()
    args = parser.parse_args(argv)

    try:
        if args.command == "doctor":
            return _cmd_doctor()
        if args.command == "selftest":
            return _cmd_selftest(args)
        if args.command == "chat":
            return _cmd_chat(args)
        if args.command in {"model", "mode"}:
            return _cmd_model(args)
        if args.command == "help":
            return _cmd_help()
        if args.command == "codexurl":
            return _cmd_codexurl()
        if args.command == "render-motd-card":
            return _cmd_render_motd_card(args)
        if args.command == "animate-motd-card":
            return _cmd_animate_motd_card(args)
        if args.command == "show-roster":
            return _cmd_show_roster(args)
        if args.command == "show-tools":
            return _cmd_show_tools(args)
        if args.command == "show-pending-command":
            return _cmd_show_pending_command(args)
        if args.command == "confirm-command":
            return _cmd_confirm_command(args)
        if args.command == "deny-command":
            return _cmd_deny_command(args)
        if args.command == "has-pending-command":
            return _cmd_has_pending_command()
        if args.command == "reroll-role":
            return _cmd_reroll_role(args)
        if args.command == "shell-settings":
            return _cmd_shell_settings(args)
        if args.command == "shell-dispatch":
            return _cmd_shell_dispatch(args)
    except KeyboardInterrupt:
        return 130
    except Exception as exc:  # pragma: no cover - CLI safety net
        print(f"[projectling] {exc}", file=sys.stderr)
        return 1

    parser.print_help()
    return 1


__all__ = [
    "ChatCore",
    "ChatResult",
    "MODEL_CHOICES",
    "ProjectLingConfig",
    "ProjectLingEngine",
    "dispatch_shell_input",
    "main",
    "run_settings_ui",
]


if __name__ == "__main__":
    raise SystemExit(main())
