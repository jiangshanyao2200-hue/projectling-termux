from __future__ import annotations

import difflib
import codecs
from contextlib import contextmanager
from dataclasses import dataclass
import html as html_lib
import ipaddress
import json
import sqlite3
import os
from pathlib import Path
import queue
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any, Callable, Iterable
import urllib.error
import urllib.parse
import urllib.request

try:
    import fcntl
except ImportError:  # pragma: no cover - projectling normally runs on Linux/Termux.
    fcntl = None


# --- Storage: Context Entries ----------------------------------------------
ENTRIES_FILE_NAME = "entries.jsonl"
MAX_ENTRY_CONTENT_CHARS = 24000
MAX_RENDER_ENTRY_CHARS = 2400


def _root_dir(config: Any) -> Path:
    root = Path(getattr(config, "root_dir", Path.cwd())).expanduser()
    return root.resolve()


def context_dir_for_config(config: Any) -> Path:
    configured = getattr(config, "context_dir", None)
    if configured:
        path = Path(str(configured)).expanduser()
        if not path.is_absolute():
            path = (_root_dir(config) / path).resolve()
        return path
    return (_root_dir(config) / "context").resolve()


def context_entries_path_for_config(config: Any) -> Path:
    configured = getattr(config, "context_entries_path", None)
    if configured:
        path = Path(str(configured)).expanduser()
        if not path.is_absolute():
            path = (_root_dir(config) / path).resolve()
        return path
    return context_dir_for_config(config) / ENTRIES_FILE_NAME


def _context_entries_lock_path(config: Any) -> Path:
    return context_entries_path_for_config(config).with_suffix(".jsonl.lock")


@contextmanager
def _context_entries_lock(config: Any):
    lock_path = _context_entries_lock_path(config)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("w", encoding="utf-8") as handle:
        if fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _entry_num(entry_id: str) -> int:
    match = re.search(r"(\d+)$", str(entry_id or ""))
    return int(match.group(1)) if match else 0


def _entry_id(value: int) -> str:
    return f"E{max(1, int(value)):06d}"


def _coerce_text(value: Any, *, limit: int = MAX_ENTRY_CONTENT_CHARS) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    marker = "\n...[entry truncated]...\n"
    keep = max(0, limit - len(marker))
    head = keep // 2
    tail = keep - head
    return f"{text[:head].rstrip()}{marker}{text[-tail:].lstrip()}"


def _normalize_entry(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    entry_id = str(raw.get("id") or "").strip()
    if not entry_id:
        return None
    content = str(raw.get("content") or "").strip()
    if not content:
        return None
    meta = raw.get("meta") if isinstance(raw.get("meta"), dict) else {}
    source_ids = raw.get("source_ids") if isinstance(raw.get("source_ids"), list) else []
    return {
        "id": entry_id,
        "ts": str(raw.get("ts") or "").strip() or _utc_now(),
        "kind": str(raw.get("kind") or "note").strip()[:40] or "note",
        "speaker": str(raw.get("speaker") or "").strip()[:160],
        "scope": str(raw.get("scope") or "shared").strip()[:40] or "shared",
        "content": content,
        "source_ids": [str(item).strip() for item in source_ids if str(item).strip()],
        "meta": dict(meta),
        "visible": bool(raw.get("visible", True)),
    }


def load_context_entries(config: Any) -> list[dict[str, Any]]:
    path = context_entries_path_for_config(config)
    if not path.is_file():
        return []
    entries: list[dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    normalized = _normalize_entry(json.loads(line))
                except Exception:
                    normalized = None
                if normalized is not None:
                    entries.append(normalized)
    except OSError:
        return []
    entries.sort(key=lambda item: _entry_num(str(item.get("id") or "")))
    return entries


def _write_context_entries(config: Any, entries: Iterable[dict[str, Any]]) -> Path:
    path = context_entries_path_for_config(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            delete=False,
            dir=str(path.parent),
            prefix=f".{path.name}.",
            suffix=".tmp",
        ) as handle:
            temp_path = Path(handle.name)
            for raw in entries:
                normalized = _normalize_entry(raw)
                if normalized is None:
                    continue
                handle.write(json.dumps(normalized, ensure_ascii=False, separators=(",", ":")) + "\n")
        if temp_path is not None:
            temp_path.replace(path)
    finally:
        if temp_path is not None and temp_path.exists():
            try:
                temp_path.unlink()
            except OSError:
                pass
    return path


def _next_entry_id_from_entries(entries: Iterable[dict[str, Any]]) -> str:
    max_seen = 0
    for entry in entries:
        max_seen = max(max_seen, _entry_num(str(entry.get("id") or "")))
    return _entry_id(max_seen + 1)


def _next_entry_id(config: Any) -> str:
    return _next_entry_id_from_entries(load_context_entries(config))


def append_context_entry(
    config: Any,
    *,
    kind: str,
    speaker: str,
    content: str,
    scope: str = "shared",
    source_ids: Iterable[Any] | None = None,
    meta: dict[str, Any] | None = None,
    visible: bool = True,
) -> dict[str, Any]:
    path = context_entries_path_for_config(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    with _context_entries_lock(config):
        entry = {
            "id": _next_entry_id_from_entries(load_context_entries(config)),
            "ts": _utc_now(),
            "kind": str(kind or "note").strip()[:40] or "note",
            "speaker": str(speaker or "").strip()[:160],
            "scope": str(scope or "shared").strip()[:40] or "shared",
            "content": _coerce_text(content),
            "source_ids": [str(item).strip() for item in (source_ids or []) if str(item).strip()],
            "meta": dict(meta or {}),
            "visible": bool(visible),
        }
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False, separators=(",", ":")) + "\n")
    return entry


def clear_context_entries(config: Any) -> Path:
    with _context_entries_lock(config):
        return _write_context_entries(config, [])


def _entry_visible(entry: dict[str, Any]) -> bool:
    if not bool(entry.get("visible", True)):
        return False
    meta = entry.get("meta") if isinstance(entry.get("meta"), dict) else {}
    return not bool(meta.get("replaced_by"))


def _trim_for_render(text: str, *, limit: int = MAX_RENDER_ENTRY_CHARS) -> str:
    text = str(text or "").strip()
    if len(text) <= limit:
        return text
    keep = max(0, limit - 48)
    head = keep // 2
    tail = keep - head
    return f"{text[:head].rstrip()}\n...[entry omitted]...\n{text[-tail:].lstrip()}"


def render_context_entries_text(
    config: Any,
    *,
    max_chars: int,
    include_hidden: bool = False,
) -> str:
    max_chars = max(0, int(max_chars or 0))
    if max_chars <= 0:
        return ""
    entries = [
        entry
        for entry in load_context_entries(config)
        if include_hidden or _entry_visible(entry)
    ]
    if not entries:
        return ""
    rendered: list[str] = []
    total = 0
    for entry in reversed(entries):
        prefix = (
            f"{entry.get('id')} · {entry.get('kind')} · "
            f"{entry.get('speaker') or 'system'} · {entry.get('ts')}"
        )
        source_ids = entry.get("source_ids") or []
        if source_ids:
            prefix += f" · source={','.join(str(item) for item in source_ids[:4])}"
        block = f"{prefix}\n{_trim_for_render(str(entry.get('content') or ''))}"
        block_len = len(block) + 2
        if rendered and total + block_len > max_chars:
            break
        if not rendered and block_len > max_chars:
            block = block[:max_chars].rstrip()
            block_len = len(block)
        rendered.append(block)
        total += block_len
    rendered.reverse()
    return "shared_context.entries:\n" + "\n\n".join(rendered)


def context_entries_status(config: Any) -> dict[str, Any]:
    path = context_entries_path_for_config(config)
    entries = load_context_entries(config)
    visible = [entry for entry in entries if _entry_visible(entry)]
    hidden = len(entries) - len(visible)
    byte_count = path.stat().st_size if path.exists() else 0
    return {
        "entries_path": str(path),
        "entries_total": len(entries),
        "entries_visible": len(visible),
        "entries_hidden": hidden,
        "entries_bytes": byte_count,
        "first_id": str(entries[0].get("id") or "") if entries else "",
        "last_id": str(entries[-1].get("id") or "") if entries else "",
    }


def list_context_entry_summaries(config: Any, *, limit: int = 40, include_hidden: bool = False) -> list[dict[str, Any]]:
    entries = [
        entry
        for entry in load_context_entries(config)
        if include_hidden or _entry_visible(entry)
    ]
    limit = max(1, min(200, int(limit or 40)))
    selected = entries[-limit:]
    rows: list[dict[str, Any]] = []
    for entry in selected:
        rows.append(
            {
                "id": entry.get("id"),
                "ts": entry.get("ts"),
                "kind": entry.get("kind"),
                "speaker": entry.get("speaker"),
                "chars": len(str(entry.get("content") or "")),
                "source_ids": entry.get("source_ids") or [],
                "preview": _trim_for_render(str(entry.get("content") or ""), limit=180),
            }
        )
    return rows


def parse_entry_range(*, entry_id: str = "", start_id: str = "", end_id: str = "", id_range: str = "") -> tuple[str, str]:
    raw_range = str(id_range or "").strip()
    if raw_range:
        parts = re.split(r"\s*(?:~|\.{2,}|-|to)\s*", raw_range, maxsplit=1, flags=re.IGNORECASE)
        if len(parts) == 1:
            start_id = parts[0]
            end_id = parts[0]
        else:
            start_id, end_id = parts[0], parts[1]
    if entry_id and not start_id:
        start_id = entry_id
    if entry_id and not end_id:
        end_id = entry_id
    start_id = str(start_id or "").strip().upper()
    end_id = str(end_id or start_id).strip().upper()
    if not start_id:
        raise ValueError("缺少 entry id。")
    if _entry_num(start_id) <= 0 or _entry_num(end_id) <= 0:
        raise ValueError("entry id 必须类似 E000001。")
    if _entry_num(start_id) > _entry_num(end_id):
        start_id, end_id = end_id, start_id
    return _entry_id(_entry_num(start_id)), _entry_id(_entry_num(end_id))


def replace_context_entries(
    config: Any,
    *,
    start_id: str,
    end_id: str,
    summary: str,
    speaker: str = "contextmanage",
    reason: str = "",
) -> dict[str, Any]:
    summary = _coerce_text(summary)
    if not summary:
        raise ValueError("summary 为空，无法 replace。")
    with _context_entries_lock(config):
        entries = load_context_entries(config)
        start_num = _entry_num(start_id)
        end_num = _entry_num(end_id)
        selected = [
            entry
            for entry in entries
            if start_num <= _entry_num(str(entry.get("id") or "")) <= end_num and _entry_visible(entry)
        ]
        if not selected:
            raise ValueError(f"没有找到可替换的 entry 区间：{start_id}~{end_id}。")
        new_id = _entry_id(max((_entry_num(str(entry.get("id") or "")) for entry in entries), default=0) + 1)
        selected_ids = [str(entry.get("id") or "") for entry in selected]
        source_ids = selected_ids if len(selected_ids) == 1 else [selected_ids[0], selected_ids[-1]]
        for entry in entries:
            if str(entry.get("id") or "") in selected_ids:
                meta = entry.get("meta") if isinstance(entry.get("meta"), dict) else {}
                meta = dict(meta)
                meta["replaced_by"] = new_id
                meta["replace_reason"] = reason
                entry["meta"] = meta
                entry["visible"] = False
        summary_entry = {
            "id": new_id,
            "ts": _utc_now(),
            "kind": "summary",
            "speaker": speaker,
            "scope": "shared",
            "content": summary,
            "source_ids": source_ids,
            "meta": {
                "replace_kind": "range",
                "source_count": len(selected_ids),
                "reason": reason,
            },
            "visible": True,
        }
        entries.append(summary_entry)
        _write_context_entries(config, entries)
    return {
        "summary_id": new_id,
        "source_ids": source_ids,
        "source_count": len(selected_ids),
        "summary_chars": len(summary),
    }


def fold_context_tool_entries(config: Any, *, keep_last: int = 6, reason: str = "") -> dict[str, Any]:
    with _context_entries_lock(config):
        entries = load_context_entries(config)
        visible_tools = [entry for entry in entries if _entry_visible(entry) and str(entry.get("kind") or "") == "tool"]
        keep_last = max(0, min(50, int(keep_last)))
        foldable = visible_tools[: max(0, len(visible_tools) - keep_last)]
        if not foldable:
            return {"folded": 0, "message": "没有可折叠的旧工具 entries。"}
        snippets = []
        for entry in foldable:
            snippets.append(f"{entry.get('id')} · {entry.get('speaker')}: {_trim_for_render(str(entry.get('content') or ''), limit=220)}")
        summary = "旧工具回执已折叠，只保留关键索引：\n" + "\n".join(snippets)
        new_id = _entry_id(max((_entry_num(str(entry.get("id") or "")) for entry in entries), default=0) + 1)
        selected_ids = [str(entry.get("id") or "") for entry in foldable]
        source_ids = selected_ids if len(selected_ids) == 1 else [selected_ids[0], selected_ids[-1]]
        for entry in entries:
            if str(entry.get("id") or "") in selected_ids:
                meta = entry.get("meta") if isinstance(entry.get("meta"), dict) else {}
                meta = dict(meta)
                meta["replaced_by"] = new_id
                meta["replace_reason"] = reason or "fold old tool entries"
                entry["meta"] = meta
                entry["visible"] = False
        entries.append(
            {
                "id": new_id,
                "ts": _utc_now(),
                "kind": "summary",
                "speaker": "contextmanage",
                "scope": "tool_trace",
                "content": summary,
                "source_ids": source_ids,
                "meta": {
                    "replace_kind": "fold_tools",
                    "source_count": len(selected_ids),
                    "reason": reason or "fold old tool entries",
                },
                "visible": True,
            }
        )
        _write_context_entries(config, entries)
    return {
        "summary_id": new_id,
        "source_ids": source_ids,
        "source_count": len(selected_ids),
        "summary_chars": len(summary),
        "folded": len(foldable),
    }


# --- Storage: Date Memory --------------------------------------------------
MEMORY_DIR_NAME = "memory"
DATEMEMORY_FILE_NAME = "datememory.json"
MEMORY_DB_FILE_NAME = "memory.db"
DEFAULT_MEMORY_MAX_BYTES = 200 * 1024
MAX_MEMORY_DIARY_CHARS = 16000
MAX_MEMORY_CHECK_RESULTS = 8
MAX_MEMORY_READ_CHARS = 24000


def memory_dir_for_config(config: Any) -> Path:
    configured = getattr(config, "memory_dir", None)
    if configured:
        path = Path(str(configured)).expanduser()
        if not path.is_absolute():
            path = (_root_dir(config) / path).resolve()
        return path
    return (_root_dir(config) / MEMORY_DIR_NAME).resolve()


def datememory_path_for_config(config: Any) -> Path:
    configured = getattr(config, "datememory_path", None)
    if configured:
        path = Path(str(configured)).expanduser()
        if not path.is_absolute():
            path = (_root_dir(config) / path).resolve()
        return path
    return memory_dir_for_config(config) / DATEMEMORY_FILE_NAME


def _datememory_lock_path(config: Any) -> Path:
    return datememory_path_for_config(config).with_suffix(".json.lock")


@contextmanager
def _datememory_lock(config: Any):
    lock_path = _datememory_lock_path(config)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("w", encoding="utf-8") as handle:
        if fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def memory_db_path_for_config(config: Any) -> Path:
    configured = getattr(config, "memory_db_path", None)
    if configured:
        path = Path(str(configured)).expanduser()
        if not path.is_absolute():
            path = (_root_dir(config) / path).resolve()
        return path
    return memory_dir_for_config(config) / MEMORY_DB_FILE_NAME


def memory_max_bytes_for_config(config: Any) -> int:
    raw = getattr(config, "memory_max_bytes", None)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = DEFAULT_MEMORY_MAX_BYTES
    return max(1, value)


def _now_local() -> time.struct_time:
    return time.localtime()


def _format_date(now: time.struct_time | None = None) -> str:
    return time.strftime("%Y-%m-%d", now or _now_local())


def _format_time(now: time.struct_time | None = None) -> str:
    return time.strftime("%H:%M", now or _now_local())


def _normalize_text(value: str) -> str:
    return " ".join(str(value or "").split()).strip()


def _normalize_persona(persona: str) -> str:
    return _normalize_text(persona)[:120]


def _default_datememory() -> dict[str, Any]:
    return {"v": 1, "days": []}


def _normalize_turn(turn: Any) -> list[str] | None:
    if isinstance(turn, (list, tuple)) and len(turn) >= 4:
        clock, role, persona, text = turn[:4]
        return [
            str(clock).strip()[:5],
            _normalize_text(role)[:16] or "assistant",
            _normalize_persona(persona),
            str(text).strip(),
        ]
    if isinstance(turn, dict):
        clock = str(turn.get("t") or turn.get("time") or "").strip()[:5]
        role = _normalize_text(turn.get("r") or turn.get("role") or "assistant")[:16] or "assistant"
        persona = _normalize_persona(turn.get("p") or turn.get("persona") or "")
        text = str(turn.get("x") or turn.get("text") or "").strip()
        if clock and text:
            return [clock, role, persona, text]
    return None


def _normalize_day(day: Any) -> dict[str, Any] | None:
    if not isinstance(day, dict):
        return None
    date = str(day.get("d") or day.get("date") or "").strip()
    if not date:
        return None
    raw_turns = day.get("t") or day.get("turns") or []
    turns = [turn for turn in (_normalize_turn(item) for item in raw_turns) if turn]
    return {"d": date, "t": turns}


def load_datememory_payload(config: Any) -> dict[str, Any]:
    path = datememory_path_for_config(config)
    if not path.is_file():
        return _default_datememory()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return _default_datememory()
    if not isinstance(raw, dict):
        return _default_datememory()
    days_raw = raw.get("days") or raw.get("d") or []
    days = [day for day in (_normalize_day(item) for item in days_raw) if day]
    return {"v": int(raw.get("v") or raw.get("version") or 1), "days": days}


def _write_datememory_payload(config: Any, payload: dict[str, Any]) -> Path:
    path = datememory_path_for_config(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            delete=False,
            dir=str(path.parent),
            prefix=f".{path.name}.",
            suffix=".tmp",
        ) as handle:
            temp_path = Path(handle.name)
            handle.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")
        if temp_path is not None:
            temp_path.replace(path)
    finally:
        if temp_path is not None and temp_path.exists():
            try:
                temp_path.unlink()
            except OSError:
                pass
    return path


def save_datememory_payload(config: Any, payload: dict[str, Any]) -> Path:
    with _datememory_lock(config):
        return _write_datememory_payload(config, payload)


def clear_datememory_payload(config: Any) -> Path:
    return save_datememory_payload(config, _default_datememory())


def ensure_memory_layout(config: Any) -> dict[str, Path]:
    memory_dir = memory_dir_for_config(config)
    datememory_path = datememory_path_for_config(config)
    memory_db_path = memory_db_path_for_config(config)
    memory_dir.mkdir(parents=True, exist_ok=True)
    if not datememory_path.exists():
        save_datememory_payload(config, _default_datememory())
    ensure_memory_db(config)
    return {
        "memory_dir": memory_dir,
        "datememory_path": datememory_path,
        "memory_db_path": memory_db_path,
    }


def ensure_memory_db(config: Any) -> Path:
    path = memory_db_path_for_config(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(str(path), timeout=10) as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA foreign_keys=ON")
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS diaries (
              date TEXT PRIMARY KEY,
              diary TEXT NOT NULL,
              keywords TEXT NOT NULL,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            )
            """
        )
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS memory_events (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              event_type TEXT NOT NULL,
              date TEXT,
              detail TEXT NOT NULL,
              created_at TEXT NOT NULL
            )
            """
        )
        db.commit()
    return path


def _memory_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%MZ", time.gmtime())


def _log_memory_event(config: Any, event_type: str, *, date: str | None, detail: str) -> None:
    try:
        path = ensure_memory_db(config)
        with sqlite3.connect(str(path), timeout=10) as db:
            db.execute(
                "INSERT INTO memory_events(event_type, date, detail, created_at) VALUES(?,?,?,?)",
                (event_type, date, detail, _memory_now()),
            )
            db.commit()
    except Exception:
        return


def _load_diary_row(db: sqlite3.Connection, date: str) -> sqlite3.Row | tuple[Any, ...] | None:
    db.row_factory = sqlite3.Row
    row = db.execute("SELECT date, diary, keywords, created_at, updated_at FROM diaries WHERE date = ?", (date,)).fetchone()
    return row


def _coerce_keywords(keywords: Iterable[Any]) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()
    for raw in keywords:
        text = _normalize_text(raw)
        key = text.lower()
        if not text or key in seen:
            continue
        seen.add(key)
        normalized.append(text)
    return normalized


def _truncate_memory_text(text: str, limit: int = MAX_MEMORY_DIARY_CHARS) -> tuple[str, bool]:
    text = str(text or "").strip()
    if len(text) <= limit:
        return text, False
    marker = "\n...[省略]...\n"
    keep = max(0, limit - len(marker))
    head = keep // 2
    tail = keep - head
    return f"{text[:head].rstrip()}{marker}{text[-tail:].lstrip()}", True


def append_chat_turns(
    config: Any,
    *,
    persona: str,
    turns: Iterable[tuple[str, str]],
    when: time.struct_time | None = None,
) -> dict[str, Any]:
    ensure_memory_layout(config)
    date = _format_date(when)
    clock = _format_time(when)
    persona_text = _normalize_persona(persona)
    added = 0
    with _datememory_lock(config):
        payload = load_datememory_payload(config)
        day = None
        for item in payload["days"]:
            if str(item.get("d") or "") == date:
                day = item
                break
        if day is None:
            day = {"d": date, "t": []}
            payload["days"].append(day)

        for role, text in turns:
            normalized = str(text or "").strip()
            if not normalized:
                continue
            day["t"].append([clock, _normalize_text(role)[:16] or "assistant", persona_text, normalized])
            added += 1

        if added:
            _write_datememory_payload(config, payload)
    return {
        "date": date,
        "time": clock,
        "turns_added": added,
        "days": len(payload["days"]),
        "path": str(datememory_path_for_config(config)),
        "bytes": datememory_size_bytes(config),
    }


def datememory_size_bytes(config: Any) -> int:
    path = datememory_path_for_config(config)
    try:
        return path.stat().st_size
    except OSError:
        return 0


def datememory_day_count(config: Any) -> int:
    return len(load_datememory_payload(config).get("days") or [])


def datememory_last_date(config: Any) -> str:
    days = load_datememory_payload(config).get("days") or []
    if not days:
        return ""
    return str(days[-1].get("d") or "")


def render_datememory_text(config: Any) -> str:
    payload = load_datememory_payload(config)
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def memory_pressure_message(config: Any) -> dict[str, Any] | None:
    size = datememory_size_bytes(config)
    limit = memory_max_bytes_for_config(config)
    if size <= 0 or size < limit:
        return None
    text = render_datememory_text(config)
    content = (
        "短期聊天缓冲 datememory.json 已达到整理阈值。\n"
        f"- 当前大小: {size} bytes\n"
        f"- 阈值: {limit} bytes\n"
        "- 系统会自动把这份聊天缓冲交给隐藏日记角色整理成 SQLite 日记；如果失败，才会回退到人工 memory_add。\n"
        "- memory_add 需要至少 5 个关键词，日期必须是 YYYY-MM-DD，日记要用日记口吻，只写聊天结论、偏好、任务、问题。\n"
        "- memory_add 成功后可传 consume_source=true 清空当前短期缓冲，再继续当前任务。\n"
        "- memory_check 只能用至少 5 个关键词检索，memory_read 只能按精确日期读取。\n"
        f"datememory.json:\n{text}"
    )
    return {"role": "system", "content": content}


def memory_status(config: Any) -> dict[str, Any]:
    ensure_memory_layout(config)
    path = datememory_path_for_config(config)
    db_path = memory_db_path_for_config(config)
    payload = load_datememory_payload(config)
    day_count = len(payload.get("days") or [])
    last_date = ""
    last_turns = 0
    if day_count:
        last_day = payload["days"][-1]
        last_date = str(last_day.get("d") or "")
        last_turns = len(last_day.get("t") or [])

    db_exists = db_path.exists()
    diary_rows = 0
    event_rows = 0
    if db_exists:
        try:
            with sqlite3.connect(str(db_path), timeout=10) as db:
                diary_rows = int(db.execute("SELECT COUNT(*) FROM diaries").fetchone()[0] or 0)
                event_rows = int(db.execute("SELECT COUNT(*) FROM memory_events").fetchone()[0] or 0)
        except Exception:
            diary_rows = 0
            event_rows = 0

    return {
        "memory_dir": str(memory_dir_for_config(config)),
        "datememory_path": str(path),
        "datememory_exists": path.exists(),
        "datememory_bytes": datememory_size_bytes(config),
        "datememory_days": day_count,
        "datememory_last_date": last_date,
        "datememory_last_turns": last_turns,
        "memory_db_path": str(db_path),
        "memory_db_exists": db_exists,
        "memory_db_bytes": db_path.stat().st_size if db_exists else 0,
        "memory_db_diaries": diary_rows,
        "memory_db_events": event_rows,
        "memory_max_bytes": memory_max_bytes_for_config(config),
    }


def _validate_date(date: str) -> str:
    text = str(date or "").strip()
    if len(text) != 10 or text[4] != "-" or text[7] != "-":
        raise ValueError("date 必须是 YYYY-MM-DD。")
    time.strptime(text, "%Y-%m-%d")
    return text


def _load_keywords_from_row(row: sqlite3.Row | tuple[Any, ...]) -> list[str]:
    raw = row["keywords"] if isinstance(row, sqlite3.Row) else row[2]
    try:
        loaded = json.loads(raw) if raw else []
    except Exception:
        loaded = []
    return _coerce_keywords(loaded if isinstance(loaded, Iterable) else [])


def memory_add_record(
    config: Any,
    *,
    date: str,
    diary: str,
    keywords: Iterable[Any],
    mode: str = "append",
    consume_source: bool = False,
) -> dict[str, Any]:
    ensure_memory_layout(config)
    db_path = ensure_memory_db(config)
    date = _validate_date(date)
    diary_text, diary_truncated = _truncate_memory_text(diary)
    keyword_list = _coerce_keywords(keywords)
    if len(keyword_list) < 5:
        raise ValueError("keywords 至少需要 5 个。")
    mode = str(mode or "append").strip().lower()
    if mode not in {"append", "replace"}:
        raise ValueError("mode 只能是 append 或 replace。")

    now = _memory_now()
    with sqlite3.connect(str(db_path), timeout=10) as db:
        db.row_factory = sqlite3.Row
        row = _load_diary_row(db, date)
        if row is None or mode == "replace":
            diary_final = diary_text
            keywords_final = keyword_list
            created_at = now if row is None else str(row["created_at"] or now)
            updated_at = now
            if row is not None and mode == "replace":
                created_at = str(row["created_at"] or now)
            db.execute(
                """
                INSERT INTO diaries(date, diary, keywords, created_at, updated_at)
                VALUES(?,?,?,?,?)
                ON CONFLICT(date) DO UPDATE SET
                  diary = excluded.diary,
                  keywords = excluded.keywords,
                  updated_at = excluded.updated_at
                """,
                (date, diary_final, json.dumps(keywords_final, ensure_ascii=False, separators=(",", ":")), created_at, updated_at),
            )
        else:
            existing_diary = str(row["diary"] or "").strip()
            existing_keywords = _load_keywords_from_row(row)
            diary_final = f"{existing_diary}\n\n{diary_text}".strip() if existing_diary else diary_text
            diary_final, append_truncated = _truncate_memory_text(diary_final)
            diary_truncated = bool(diary_truncated or append_truncated)
            keywords_final = _coerce_keywords([*existing_keywords, *keyword_list])
            db.execute(
                """
                UPDATE diaries
                SET diary = ?, keywords = ?, updated_at = ?
                WHERE date = ?
                """,
                (
                    diary_final,
                    json.dumps(keywords_final, ensure_ascii=False, separators=(",", ":")),
                    now,
                    date,
                ),
            )
        db.execute(
            "INSERT INTO memory_events(event_type, date, detail, created_at) VALUES(?,?,?,?)",
            (
                "memory_add",
                date,
                f"mode={mode} keywords={len(keyword_list)} consume_source={int(bool(consume_source))}",
                now,
            ),
        )
        db.commit()

    if consume_source:
        clear_datememory_payload(config)

    return {
        "date": date,
        "mode": mode,
        "keywords": keyword_list,
        "keyword_count": len(keyword_list),
        "diary_chars": len(diary_final),
        "diary_truncated": diary_truncated,
        "consume_source": bool(consume_source),
        "source_cleared": bool(consume_source),
        "db_path": str(db_path),
    }


def memory_check_records(
    config: Any,
    *,
    keywords: Iterable[Any],
    limit: int = 5,
) -> dict[str, Any]:
    ensure_memory_layout(config)
    db_path = ensure_memory_db(config)
    keyword_list = _coerce_keywords(keywords)
    if len(keyword_list) < 5:
        raise ValueError("keywords 至少需要 5 个。")
    limit = max(1, min(MAX_MEMORY_CHECK_RESULTS, int(limit or 5)))
    query_terms = [text.lower() for text in keyword_list]

    matches: list[dict[str, Any]] = []
    with sqlite3.connect(str(db_path), timeout=10) as db:
        db.row_factory = sqlite3.Row
        rows = db.execute("SELECT date, diary, keywords, created_at, updated_at FROM diaries").fetchall()
        for row in rows:
            diary = str(row["diary"] or "")
            diary_lower = diary.lower()
            row_keywords = _load_keywords_from_row(row)
            row_kw_lower = [item.lower() for item in row_keywords]
            matched = [term for term in query_terms if any(term in diary_lower or term in keyword for keyword in row_kw_lower)]
            hit_count = len(matched)
            if not hit_count:
                continue
            hit_rate = hit_count / len(query_terms)
            matches.append(
                {
                    "date": str(row["date"] or ""),
                    "hit_count": hit_count,
                    "hit_rate": round(hit_rate, 3),
                    "matched_keywords": matched,
                    "keywords": row_keywords[:12],
                    "updated_at": str(row["updated_at"] or row["created_at"] or ""),
                    "created_at": str(row["created_at"] or ""),
                    "diary": diary,
                }
            )

    matches.sort(key=lambda item: (-int(item["hit_count"]), -float(item["hit_rate"]), str(item["updated_at"])))
    trimmed = matches[:limit]
    best = trimmed[0] if trimmed else None
    show_detail = bool(best and float(best["hit_rate"]) >= 0.8)
    best_detail = None
    if best and show_detail:
        diary_text, diary_truncated = _truncate_memory_text(str(best.get("diary") or ""), MAX_MEMORY_READ_CHARS)
        best_detail = {
            "date": best["date"],
            "hit_count": best["hit_count"],
            "hit_rate": best["hit_rate"],
            "matched_keywords": best["matched_keywords"],
            "keywords": best["keywords"],
            "updated_at": best["updated_at"],
            "diary": diary_text,
            "diary_truncated": diary_truncated,
        }
    result: dict[str, Any] = {
        "keywords": keyword_list,
        "limit": limit,
        "result_count": len(trimmed),
        "dates": [{"date": item["date"]} for item in trimmed],
    }
    if best_detail:
        result["best_detail"] = best_detail
    elif best:
        result["best"] = {
            "date": best["date"],
            "hit_count": best["hit_count"],
            "hit_rate": best["hit_rate"],
            "matched_keywords": best["matched_keywords"],
            "keywords": best["keywords"],
            "updated_at": best["updated_at"],
        }
    _log_memory_event(
        config,
        "memory_check",
        date=None,
        detail=f"keywords={len(keyword_list)} results={len(trimmed)} detail={int(show_detail)}",
    )
    return result


def memory_read_records(config: Any, *, dates: Iterable[Any]) -> dict[str, Any]:
    ensure_memory_layout(config)
    db_path = ensure_memory_db(config)
    requested: list[str] = []
    seen: set[str] = set()
    for raw in dates:
        date = _validate_date(raw)
        if date in seen:
            continue
        seen.add(date)
        requested.append(date)
    if not requested:
        raise ValueError("dates 不能为空。")

    entries: list[dict[str, Any]] = []
    missing: list[str] = []
    with sqlite3.connect(str(db_path), timeout=10) as db:
        db.row_factory = sqlite3.Row
        for date in requested:
            row = _load_diary_row(db, date)
            if row is None:
                missing.append(date)
                continue
            diary_text, diary_truncated = _truncate_memory_text(str(row["diary"] or ""), MAX_MEMORY_READ_CHARS)
            entries.append(
                {
                    "date": str(row["date"] or ""),
                    "diary": diary_text,
                    "diary_truncated": diary_truncated,
                    "keywords": _load_keywords_from_row(row),
                    "created_at": str(row["created_at"] or ""),
                    "updated_at": str(row["updated_at"] or ""),
                }
            )

    result = {
        "requested": requested,
        "found": len(entries),
        "missing": missing,
        "entries": entries,
    }
    _log_memory_event(
        config,
        "memory_read",
        date=",".join(requested[:4]) if requested else None,
        detail=f"requested={len(requested)} found={len(entries)} missing={len(missing)}",
    )
    return result


PENDING_FILE_NAME = "pending-command.json"
UPDATE_PLAN_STATE_FILE_NAME = "update-plan.json"
PENDING_TTL_SECONDS = 1800
DEFAULT_TIMEOUT_SECONDS = 90
MAX_TIMEOUT_SECONDS = 180
MAX_COMMAND_CHARS = 8192
MAX_STDOUT_CHARS = 24000
MAX_STDERR_CHARS = 16000
MAX_MODEL_STDOUT_CHARS = 12000
MAX_MODEL_STDERR_CHARS = 6000
MAX_STREAM_EVENT_CHARS = 2048
MAX_STREAM_EVENTS_PER_STREAM = 12
MAX_STREAM_EVENT_TOTAL_CHARS = 12000
MAX_COMPACT_CONTEXT_CHARS = 16000
MAX_CONTEXT_MANAGE_COMPACT_CHARS = 240000
MAX_TERMINAL_PREVIEW_CHARS = 24000
MAX_AIDEBUG_READ_CHARS = 24000
MAX_PATCH_CHARS = 180000
MAX_UPDATE_PLAN_ITEMS = 40
MAX_WEB_SEARCH_RESULTS = 8
MAX_WEB_SEARCH_SUMMARY_CHARS = 2400
MAX_WEB_SEARCH_SNIPPET_CHARS = 600
MAX_WEB_SEARCH_STDOUT_CHARS = 7000
DEFAULT_WEBSEARCH_ENDPOINT = "https://open.feedcoopapi.com/search_api/web_search"
STREAM_POLL_INTERVAL_SECONDS = 0.05
TERMINAL_OUTPUT_DIR_NAME = "terminal output"
TERMINAL_STATE_FILE_NAME = "terminal-sessions.json"
CONTEXT_BUDGET_STATE_FILE_NAME = "context-budget.json"
CONTEXT_BUDGET_LEVELS: tuple[tuple[str, int], ...] = (
    ("tiny", 12),
    ("small", 33),
    ("medium", 66),
    ("large", 85),
    ("full", 100),
)
CONTEXT_BUDGET_LEVEL_LOOKUP = {name: percent for name, percent in CONTEXT_BUDGET_LEVELS}
INLINE_CONTEXT_PERCENT_KEYS = ("context_percent", "next_context_percent", "context_budget_percent")
INLINE_CONTEXT_LEVEL_KEYS = ("context_level", "next_context_level", "context_budget_level")
INLINE_CONTEXT_TURN_KEYS = ("context_turns", "next_context_turns", "context_budget_turns")
PATCH_TEXT_ARG_KEYS = ("patch", "diff", "patch_text", "content", "text")
DEEPSEEK_STRUCTURED_PATCH_OPS = {
    "write",
    "create",
    "replace_file",
    "replace",
    "append",
    "prepend",
    "insert_after",
    "insert_before",
    "delete",
    "delete_file",
    "remove",
    "patch",
}

INTERACTIVE_COMMANDS = {
    "ftp",
    "htop",
    "less",
    "man",
    "more",
    "mosh",
    "nano",
    "nvim",
    "scp",
    "screen",
    "sftp",
    "ssh",
    "tailf",
    "telnet",
    "tmux",
    "top",
    "vi",
    "vim",
    "watch",
}

SHELL_BUILTINS_THAT_CANNOT_PERSIST = {
    ".",
    "alias",
    "builtin",
    "cd",
    "exec",
    "export",
    "popd",
    "pushd",
    "set",
    "source",
    "ulimit",
    "umask",
    "unalias",
    "unset",
}

DIRECT_COMMANDS = {
    "cat",
    "date",
    "df",
    "du",
    "echo",
    "env",
    "file",
    "free",
    "head",
    "id",
    "ip",
    "ls",
    "netstat",
    "ping",
    "printenv",
    "printf",
    "ps",
    "pwd",
    "readlink",
    "rg",
    "ss",
    "stat",
    "tail",
    "test",
    "tree",
    "uname",
    "wc",
    "whoami",
    "which",
}
FIND_MUTATING_TOKENS = {"-delete", "-exec", "-execdir", "-ok", "-okdir", "-fdelete"}
SED_MUTATING_FLAGS = {"-i", "--in-place", "-ni", "-in"}

MUTATING_COMMANDS = {
    "apt",
    "apt-get",
    "apk",
    "chmod",
    "chown",
    "chgrp",
    "cp",
    "dd",
    "dnf",
    "git",
    "install",
    "ln",
    "make",
    "mkdir",
    "mkfs",
    "mount",
    "mv",
    "npm",
    "pacman",
    "pip",
    "pip3",
    "pkg",
    "pnpm",
    "python",
    "python3",
    "rm",
    "rmdir",
    "rsync",
    "tar",
    "touch",
    "umount",
    "uv",
    "wget",
    "curl",
    "yum",
    "zypper",
}

HIGH_RISK_COMMANDS = {
    "dd",
    "mkfs",
    "mount",
    "rm",
    "rmdir",
    "umount",
}

TERMUX_SAFE_COMMANDS = {
    "termux-battery-status",
    "termux-camera-info",
    "termux-clipboard-get",
    "termux-info",
    "termux-notification-list",
    "termux-sensor",
    "termux-storage-get",
    "termux-telephony-deviceinfo",
    "termux-usb",
    "termux-volume",
    "termux-wifi-connectioninfo",
    "termux-wifi-scaninfo",
}

TERMUX_CONFIRM_COMMANDS = {
    "termux-brightness",
    "termux-camera-photo",
    "termux-clipboard-set",
    "termux-dialog",
    "termux-download",
    "termux-keystore",
    "termux-location",
    "termux-media-player",
    "termux-microphone-record",
    "termux-notification",
    "termux-notification-remove",
    "termux-open",
    "termux-open-url",
    "termux-reload-settings",
    "termux-sms-list",
    "termux-sms-send",
    "termux-storage-photo",
    "termux-telephony-call",
    "termux-torch",
    "termux-toast",
    "termux-vibrate",
    "termux-wallpaper",
    "termux-wake-lock",
    "termux-wake-unlock",
}

GIT_READONLY_SUBCOMMANDS = {
    "branch",
    "describe",
    "diff",
    "grep",
    "log",
    "remote",
    "rev-parse",
    "show",
    "status",
    "tag",
}

GIT_MUTATING_SUBCOMMANDS = {
    "add",
    "am",
    "apply",
    "bisect",
    "checkout",
    "cherry-pick",
    "clean",
    "clone",
    "commit",
    "fetch",
    "merge",
    "mv",
    "pull",
    "push",
    "rebase",
    "reset",
    "restore",
    "revert",
    "rm",
    "stash",
    "switch",
    "worktree",
}

PACKAGE_MANAGER_SUBCOMMANDS = {
    "apt",
    "apt-get",
    "apk",
    "dnf",
    "npm",
    "pacman",
    "pip",
    "pip3",
    "pkg",
    "pnpm",
    "uv",
    "yum",
    "zypper",
}

ADB_GLOBAL_OPTIONS_WITH_VALUE = {"-H", "-L", "-P", "-s", "-t"}
SHELL_DETACH_WORDS = {"nohup", "setsid"}


@dataclass(frozen=True)
class ToolContext:
    cwd: Path
    home: Path
    config: Any
    event_callback: Callable[[str, dict[str, Any]], None] | None = None
    active_role: Any | None = None
    active_liaison: Any | None = None
    execution_role: Any | None = None
    persona_path: Path | None = None
    liaison_path: Path | None = None
    dualstar_path: Path | None = None
    toolbox: ToolBox | None = None


@dataclass(frozen=True)
class ToolDefinition:
    name: str
    description: str
    input_schema: dict[str, Any]
    handler: Callable[[dict[str, Any], ToolContext], dict[str, Any]]

    def schema(self, *, description: str | None = None) -> dict[str, Any]:
        input_schema = _schema_with_inline_context_budget(self.name, self.input_schema)
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": (description or self.description).strip(),
                "parameters": input_schema,
            },
        }


def _role_display_name(role: Any | None) -> str:
    if role is None:
        return ""
    zh = str(getattr(role, "name_zh", "") or "").strip()
    en = str(getattr(role, "name_en", "") or "").strip()
    if zh and en:
        return f"{zh} / {en}"
    return zh or en


def _tool_actor_payload(context: ToolContext) -> dict[str, Any]:
    execution_name = _role_display_name(getattr(context, "execution_role", None))
    if not execution_name:
        return {}
    main_name = _role_display_name(getattr(context, "active_role", None))
    return {
        "actor_kind": "executor",
        "actor_label": "执行位",
        "actor_name": execution_name,
        "planner_name": main_name,
    }


@dataclass
class ToolBoxEntry:
    expanded: bool
    summary: str
    detail: str
    pinned: bool = False


class ToolBox:
    def __init__(
        self,
        config: Any,
        tool_defs: dict[str, ToolDefinition] | None = None,
    ) -> None:
        self.config = config
        self.path = _toolbox_path(config)
        self._tools = tool_defs or {}
        self._entries: dict[str, ToolBoxEntry] = {}
        self.reload(self._tools)

    def reload(self, tool_defs: dict[str, ToolDefinition] | None = None) -> None:
        if tool_defs is not None:
            self._tools = tool_defs
        self._load()
        if self._tools:
            self.sync_defaults(self._tools)

    def _load(self) -> None:
        data = _load_json_file(self.path)
        raw_entries = data.get("tools") if isinstance(data, dict) else {}
        entries: dict[str, ToolBoxEntry] = {}
        if isinstance(raw_entries, dict):
            for name, payload in raw_entries.items():
                if not isinstance(payload, dict):
                    continue
                summary = str(payload.get("summary") or "").strip()
                detail = str(payload.get("detail") or payload.get("description") or summary).strip()
                if not detail:
                    detail = summary
                entries[str(name).strip()] = ToolBoxEntry(
                    expanded=bool(payload.get("expanded", True)),
                    summary=summary or _summarize_tool_description(detail),
                    detail=detail or summary or "",
                    pinned=bool(payload.get("pinned", False)),
                )
        self._entries = entries

    def _save(self) -> None:
        payload = {
            "version": 1,
            "updated_at": _utc_now_text(),
            "tools": {
                name: {
                    "expanded": entry.expanded,
                    "summary": entry.summary,
                    "detail": entry.detail,
                    "pinned": entry.pinned,
                }
                for name, entry in sorted(self._entries.items())
            },
        }
        _write_json_file(self.path, payload)

    def sync_defaults(self, tool_defs: dict[str, ToolDefinition]) -> None:
        changed = False
        for name, tool in tool_defs.items():
            summary = _summarize_tool_description(tool.description)
            detail = tool.description.strip()
            entry = self._entries.get(name)
            if entry is None:
                self._entries[name] = ToolBoxEntry(
                    expanded=True,
                    summary=summary,
                    detail=detail,
        pinned=(name in {"tool_manage", "update_plan"}),
                )
                changed = True
                continue
            if entry.summary != summary:
                entry.summary = summary
                changed = True
            if entry.detail != detail:
                entry.detail = detail
                changed = True
            if name in {"tool_manage", "update_plan"} and not entry.pinned:
                entry.pinned = True
                changed = True
        if changed or not self.path.is_file():
            self._save()

    def _active_names(self) -> list[str]:
        if not self._tools:
            return list(self._entries)
        return [name for name in self._entries if name in self._tools]

    def describe(self, name: str, *, expanded: bool | None = None) -> str:
        entry = self._entries.get(name)
        if entry is None:
            return ""
        if expanded is None:
            expanded = self.is_expanded(name)
        if expanded:
            return entry.detail or entry.summary
        return entry.summary or _summarize_tool_description(entry.detail)

    def is_expanded(self, name: str) -> bool:
        entry = self._entries.get(name)
        if entry is None:
            return True
        if entry.pinned:
            return True
        return bool(entry.expanded)

    def set_visibility(self, names: list[str], *, expanded: bool) -> list[str]:
        changed: list[str] = []
        normalized = [str(name or "").strip() for name in names if str(name or "").strip()]
        if not normalized:
            return changed
        for name in normalized:
            entry = self._entries.get(name)
            if entry is None:
                continue
            if entry.pinned:
                continue
            if entry.expanded == expanded:
                continue
            entry.expanded = expanded
            changed.append(name)
        if changed:
            self._save()
        return changed

    def reset(self) -> list[str]:
        changed: list[str] = []
        for name, entry in self._entries.items():
            if entry.pinned:
                continue
            if entry.expanded:
                continue
            entry.expanded = True
            changed.append(name)
        if changed:
            self._save()
        return changed

    def overview(self, *, include_hidden: bool = True, include_detail: bool = True) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for name in sorted(self._active_names()):
            entry = self._entries[name]
            if not include_hidden and not self.is_expanded(name):
                continue
            row = {
                "name": name,
                "expanded": self.is_expanded(name),
                "pinned": entry.pinned,
                "summary": entry.summary,
            }
            if include_detail:
                row["detail"] = entry.detail
            rows.append(row)
        return rows

    def inspect(self, names: list[str], *, include_schema: bool = True) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for raw_name in names:
            name = str(raw_name or "").strip()
            if not name:
                continue
            if self._tools and name not in self._tools:
                continue
            entry = self._entries.get(name)
            if entry is None:
                continue
            row = {
                "name": name,
                "expanded": self.is_expanded(name),
                "pinned": entry.pinned,
                "summary": entry.summary,
            }
            if include_schema:
                row["detail"] = entry.detail
            rows.append(row)
        return rows

    def visible_names(self) -> list[str]:
        return [name for name in self._active_names() if self.is_expanded(name)]

    def all_names(self) -> list[str]:
        return self._active_names()


def _toolbox_path(config: Any) -> Path:
    config_dir = Path(getattr(config, "config_dir", Path.cwd())).expanduser()
    config_dir.mkdir(parents=True, exist_ok=True)
    return config_dir / "toolbox.json"


def _summarize_tool_description(text: str, *, limit: int = 120) -> str:
    cleaned = " ".join(str(text or "").split()).strip()
    if not cleaned:
        return ""
    for separator in ("。", "！", "？", ".", "!", "?"):
        if separator in cleaned:
            head = cleaned.split(separator, 1)[0].strip()
            if head:
                return head[:limit].rstrip()
    return cleaned[:limit].rstrip()


def _schema_with_inline_context_budget(tool_name: str, input_schema: dict[str, Any]) -> dict[str, Any]:
    try:
        schema = json.loads(json.dumps(input_schema, ensure_ascii=False))
    except (TypeError, ValueError):
        schema = dict(input_schema)
    if str(tool_name or "") == "context":
        return schema
    if schema.get("type") != "object":
        return schema
    properties = schema.setdefault("properties", {})
    if not isinstance(properties, dict):
        return schema
    properties.setdefault(
        "context_percent",
        {
            "type": "integer",
            "minimum": 0,
            "maximum": 100,
            "description": (
                "Optional smart-context visibility percentage for the next model request after this tool result. "
                "This only changes what is injected next turn for the shared entries context; hidden context is not "
                "deleted or forgotten and can be restored later by setting 66, 85, or 100. Percent is the primary "
                "control and the value is approximate: around 40 for light lookups, around 66 for local inspection, "
                "around 85 for code changes, and 100 only when full memory/context is actually needed."
            ),
        },
    )
    properties.setdefault(
        "context_level",
        {
            "type": "string",
            "enum": ["tiny", "small", "medium", "large", "full"],
            "description": "Optional shorthand for context_percent; percent still drives the real control. tiny=12, small=33, medium=66, large=85, full=100.",
        },
    )
    properties.setdefault(
        "context_turns",
        {
            "type": "integer",
            "minimum": 1,
            "maximum": 5,
            "description": "How many upcoming model requests should use this inline context budget. Defaults to 1.",
        },
    )
    return schema


@dataclass(frozen=True)
class CommandDecision:
    action: str
    risk: str
    reason: str
    confirm_command: str = "y"


@dataclass(frozen=True)
class ShellStructureFlags:
    has_composite: bool = False
    has_pipe: bool = False
    has_redirection: bool = False
    has_command_substitution: bool = False


class _BoundedCollector:
    def __init__(self, limit: int) -> None:
        self.limit = max(0, int(limit))
        self.parts: list[str] = []
        self.head = ""
        self.tail = ""
        self.size = 0
        self.truncated = False
        self.marker = "\n...[middle omitted for stability]...\n"

    def _capture_middle_truncated(self, text: str) -> None:
        keep = max(0, self.limit - len(self.marker))
        head_len = keep // 2
        tail_len = keep - head_len
        self.head = text[:head_len]
        self.tail = text[-tail_len:] if tail_len > 0 else ""
        self.parts = []

    def append(self, text: str) -> None:
        if not text:
            return
        if self.limit <= 0:
            self.size += len(text)
            self.truncated = True
            return
        if not self.truncated and self.size + len(text) <= self.limit:
            self.parts.append(text)
            self.size += len(text)
            return

        if not self.truncated:
            combined = "".join(self.parts) + text
            self.size += len(text)
            self.truncated = True
            self._capture_middle_truncated(combined)
            return

        self.size += len(text)
        tail_len = max(0, self.limit - len(self.marker)) - len(self.head)
        self.tail = (self.tail + text)[-tail_len:] if tail_len > 0 else ""

    def text(self) -> str:
        if self.truncated:
            if not self.head and not self.tail:
                return ""
            return f"{self.head.rstrip()}{self.marker}{self.tail.lstrip()}"
        if not self.parts:
            return ""
        return "".join(self.parts)


def _pending_command_path(config: Any) -> Path:
    runtime_dir = Path(getattr(config, "runtime_dir", Path.cwd())).expanduser()
    runtime_dir.mkdir(parents=True, exist_ok=True)
    return runtime_dir / PENDING_FILE_NAME


def _load_pending_command(config: Any) -> dict[str, Any] | None:
    path = _pending_command_path(config)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        try:
            path.unlink()
        except OSError:
            pass
        return None

    expires_at = int(data.get("expires_at") or 0)
    if expires_at and expires_at <= int(time.time()):
        try:
            path.unlink()
        except OSError:
            pass
        return None
    return data


def _store_pending_command(config: Any, payload: dict[str, Any]) -> dict[str, Any]:
    path = _pending_command_path(config)
    _atomic_write_text_file(path, json.dumps(payload, ensure_ascii=False, indent=2))
    return payload


def _clear_pending_command(config: Any) -> dict[str, Any] | None:
    path = _pending_command_path(config)
    previous = _load_pending_command(config)
    try:
        if path.exists():
            path.unlink()
    except OSError:
        pass
    return previous


def _truncate_text(text: str, limit: int) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    head = text[:limit]
    suffix = "\n...[truncated]..."
    keep = max(0, limit - len(suffix))
    return f"{head[:keep]}{suffix}", True


def _middle_truncate_text(text: str, limit: int) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    marker = "\n...[middle omitted for stability]...\n"
    keep = max(0, limit - len(marker))
    head = keep // 2
    tail = keep - head
    return f"{text[:head].rstrip()}{marker}{text[-tail:].lstrip()}", True


def _compact_tool_result_for_model(payload: dict[str, Any]) -> dict[str, Any]:
    compacted = dict(payload)
    patch = str(compacted.get("patch") or "")
    if patch:
        compacted["patch"], patch_truncated = _middle_truncate_text(patch, MAX_MODEL_STDOUT_CHARS)
        compacted["patch_truncated_for_model"] = bool(patch_truncated)
    stdout = str(compacted.get("stdout") or "")
    stderr = str(compacted.get("stderr") or "")
    if stdout:
        compacted["stdout"], truncated = _middle_truncate_text(stdout, MAX_MODEL_STDOUT_CHARS)
        compacted["stdout_truncated_for_model"] = bool(truncated or compacted.get("stdout_truncated"))
    if stderr:
        compacted["stderr"], truncated = _middle_truncate_text(stderr, MAX_MODEL_STDERR_CHARS)
        compacted["stderr_truncated_for_model"] = bool(truncated or compacted.get("stderr_truncated"))
    log_preview = str(compacted.get("log_preview") or "")
    if log_preview:
        compacted["log_preview"], truncated = _middle_truncate_text(log_preview, MAX_MODEL_STDOUT_CHARS)
        compacted["log_preview_truncated_for_model"] = bool(truncated or compacted.get("log_preview_truncated"))
    if str(compacted.get("tool") or "") == "update_plan":
        items = compacted.get("items") or []
        if isinstance(items, list) and len(items) > 12:
            compacted["items"] = items[:10]
            compacted["items_truncated_for_model"] = True
    tool_name = str(compacted.get("tool") or "")
    if tool_name and not compacted.get("kind"):
        compacted["kind"] = tool_name
    summary = str(compacted.get("summary") or "").strip()
    if not summary:
        summary = _tool_fact_summary(compacted)
    if summary:
        compacted["summary"] = summary
    return compacted


def _shorten_fact_text(text: str, limit: int = 140) -> str:
    cleaned = " ".join(str(text or "").split())
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: limit - 1].rstrip() + "…"


def _tool_name_list(value: Any) -> list[str]:
    items: list[Any]
    if isinstance(value, str):
        items = [value]
    elif isinstance(value, (list, tuple)):
        items = list(value)
    else:
        items = []
    names: list[str] = []
    seen: set[str] = set()
    for item in items:
        if isinstance(item, dict):
            raw = str(item.get("name") or item.get("tool") or "").strip()
        else:
            raw = str(item or "").strip()
        key = raw.lower()
        if not raw or key in seen:
            continue
        seen.add(key)
        names.append(raw)
    return names


def _tool_fact_summary(payload: dict[str, Any]) -> str:
    tool = str(payload.get("tool") or "").strip()
    status = str(payload.get("status") or "").strip()
    if tool == "command":
        channel = str(payload.get("channel") or "Bash").strip() or "Bash"
        command = _shorten_fact_text(str(payload.get("command") or ""), limit=96)
        rc = payload.get("returncode")
        if status == "pending_confirmation":
            return f"{channel} pending confirmation · {command}" if command else f"{channel} pending confirmation"
        if status == "blocked":
            return f"{channel} blocked · {command}" if command else f"{channel} blocked"
        if status == "timeout":
            return f"{channel} timeout · {command}" if command else f"{channel} timeout"
        if rc is not None:
            return f"{channel} rc={rc} · {command}" if command else f"{channel} rc={rc}"
        return f"{channel} ok · {command}" if command else f"{channel} ok"

    if tool == "apply_patch":
        changed = payload.get("changed_files") or []
        if not isinstance(changed, list):
            changed = []
        files = ", ".join(_shorten_fact_text(str(item or ""), limit=36) for item in changed[:3] if str(item or "").strip())
        mode = _shorten_fact_text(str(payload.get("mode_used") or ""), limit=32)
        warnings = payload.get("warnings") or []
        warning_note = f"{len(warnings)} warning" if isinstance(warnings, list) and warnings else ""
        if status == "error":
            status = "failed"
        parts = ["patch", status or "ok"]
        if files:
            parts.append(files)
        if mode:
            parts.append(mode)
        if warning_note:
            parts.append(warning_note)
        return " · ".join(parts)

    if tool == "tool_manage":
        action = str(payload.get("action") or "list").strip().lower()
        if action == "list":
            expanded = payload.get("expanded_count")
            total = payload.get("total_count")
            if isinstance(expanded, int) and isinstance(total, int) and total > 0:
                return f"toolbox list · {expanded}/{total} visible"
            if isinstance(total, int) and total > 0:
                return f"toolbox list · {total} tools"
            return "toolbox list"
        if action == "inspect":
            names = _tool_name_list(payload.get("tools"))
            if names:
                return f"toolbox inspect · {', '.join(names[:4])}"
            return "toolbox inspect"
        names = _tool_name_list(payload.get("requested") or payload.get("changed") or payload.get("tools") or payload.get("tool"))
        if names:
            return f"toolbox {action} · {', '.join(names[:4])}"
        return f"toolbox {action}"

    if tool == "persona_link":
        action = str(payload.get("action") or "").strip().lower()
        if action == "switch":
            target = str(payload.get("target") or payload.get("speaker_mode") or "").strip().lower() or "liaison"
            speaker = _shorten_fact_text(str(payload.get("speaker_name") or ""), limit=48)
            if speaker:
                return f"persona_link · switch · {target} · {speaker}"
            return f"persona_link · switch · {target}"
        if action == "mission":
            task = _shorten_fact_text(str(payload.get("task") or payload.get("message") or ""), limit=96)
            status_text = str(payload.get("mission_status") or payload.get("status") or "queued").strip().lower()
            parts = ["persona_link", "mission", status_text]
            if task:
                parts.append(task)
            return " · ".join(parts)
        if action in {"send", "contact", "liaison"}:
            name = _shorten_fact_text(str(payload.get("liaison_name") or payload.get("name") or "liaison"), limit=48)
            rounds = payload.get("rounds")
            parts = ["persona_link", action, name]
            if isinstance(rounds, int) and rounds > 0:
                parts.append(f"{rounds} rounds")
            return " · ".join(parts)
        return "persona_link"

    if tool == "link":
        action = str(payload.get("action") or "").strip().lower()
        target = str(payload.get("target") or "").strip().lower()
        message = _shorten_fact_text(str(payload.get("message") or payload.get("task") or ""), limit=96)
        parts = ["X-Link"]
        if action:
            parts.append(action)
        if target:
            parts.append(target)
        if message:
            parts.append(message)
        return " · ".join(parts)

    if tool == "update_plan":
        action = str(payload.get("action") or "").strip().lower() or "status"
        mode = str(payload.get("mode") or "").strip().lower() or "todo"
        title = _shorten_fact_text(str(payload.get("title") or ""), limit=48)
        completed = payload.get("completed_count")
        total = payload.get("total_count")
        parts = ["update_plan", action, mode]
        if isinstance(completed, int) and isinstance(total, int):
            parts.append(f"{completed}/{total}")
        if title:
            parts.append(title)
        return " · ".join(parts)

    if tool == "model_mode":
        action = str(payload.get("action") or "").strip().lower() or "status"
        mode = str(payload.get("mode") or "").strip().lower()
        planner = str(payload.get("planner_model") or "").strip()
        executor = str(payload.get("executor_model") or "").strip()
        parts = ["model_mode", action]
        if mode:
            parts.append(mode)
        if planner or executor:
            parts.append(f"{planner or '?'} -> {executor or '?'}")
        return " · ".join(parts)

    if tool == "context":
        percent = payload.get("context_budget_percent")
        if percent is None:
            percent = payload.get("percent")
        try:
            percent_int = max(0, min(100, int(percent)))
        except (TypeError, ValueError):
            percent_int = 100
        turns = payload.get("context_budget_turns") or payload.get("turns_remaining") or payload.get("turns") or 1
        try:
            turns_int = max(1, int(turns))
        except (TypeError, ValueError):
            turns_int = 1
        if percent_int >= 100:
            return "context · full"
        return f"context · {percent_int}% · {turns_int} turn" + ("s" if turns_int != 1 else "")

    if tool in {"context_manage", "contextmanage"}:
        mode = str(payload.get("mode") or "").strip().lower() or "compact"
        target = str(payload.get("target") or "").strip().lower()
        saved = payload.get("saved_chars")
        parts = [f"{tool} · {mode}"]
        if target:
            parts.append(target)
        if isinstance(saved, int):
            parts.append(f"saved {saved} chars")
        return " · ".join(parts)

    if tool == "memory_status":
        action = str(payload.get("action") or "status").strip().lower()
        if action == "clear_datememory":
            return "memory_status · datememory cleared"
        db_count = payload.get("db_count")
        diary_count = payload.get("diary_count")
        if isinstance(db_count, int) and isinstance(diary_count, int):
            return f"memory_status · {diary_count} diaries · {db_count} records"
        return "memory_status"
    if tool == "memory_add":
        date = str(payload.get("date") or "").strip()
        keyword_count = payload.get("keyword_count") or len(payload.get("keywords") or [])
        parts = ["memory_add"]
        if date:
            parts.append(date)
        if isinstance(keyword_count, int):
            parts.append(f"{keyword_count} kw")
        return " · ".join(parts)
    if tool == "memory_check":
        result_count = payload.get("result_count")
        parts = ["memory_check"]
        if isinstance(result_count, int):
            parts.append(f"{result_count} hits")
        if payload.get("best_detail"):
            parts.append("detail")
        return " · ".join(parts)
    if tool == "memory_read":
        found = payload.get("found")
        parts = ["memory_read"]
        if isinstance(found, int):
            parts.append(f"{found} entries")
        return " · ".join(parts)

    if tool == "web_search":
        mode = str(payload.get("mode") or "auto").strip().lower()
        count = payload.get("count") or payload.get("results_count")
        if isinstance(count, int):
            return f"web_search · {mode} · {count} results"
        return f"web_search · {mode}"

    if tool == "terminal":
        session_name = _shorten_fact_text(str(payload.get("session_name") or ""), limit=32)
        log_path = _shorten_fact_text(str(payload.get("log_path") or ""), limit=48)
        parts = ["terminal"]
        if session_name:
            parts.append(session_name)
        if log_path:
            parts.append(log_path)
        return " · ".join(parts)

    if tool == "aidebug":
        action = str(payload.get("action") or "").strip().lower()
        score = payload.get("overall_score")
        parts = ["aidebug"]
        if action:
            parts.append(action)
        if score is not None:
            parts.append(f"score {score}")
        return " · ".join(parts)

    if status:
        return f"{tool} · {status}"
    return tool


def _load_text_file(path: Path) -> str:
    if not path.is_file():
        return ""
    return path.read_text(encoding="utf-8", errors="replace")


def _write_text_file(path: Path, text: str) -> None:
    _atomic_write_text_file(path, text)


def _load_json_file(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _write_json_file(path: Path, payload: dict[str, Any]) -> None:
    _atomic_write_text_file(path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


def _atomic_write_text_file(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            delete=False,
            dir=str(path.parent),
            prefix=f".{path.name}.",
            suffix=".tmp",
        ) as handle:
            temp_path = Path(handle.name)
            handle.write(text)
        if temp_path is not None:
            temp_path.replace(path)
    finally:
        if temp_path is not None:
            try:
                if temp_path.exists():
                    temp_path.unlink()
            except OSError:
                pass


def _utc_now_text() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _context_budget_root(config: Any) -> Path:
    root = Path(getattr(config, "runtime_dir", Path.cwd())).expanduser()
    root.mkdir(parents=True, exist_ok=True)
    return root


def _context_budget_path(config: Any) -> Path:
    return _context_budget_root(config) / CONTEXT_BUDGET_STATE_FILE_NAME


def _context_budget_bar(percent: int, *, width: int = 8) -> str:
    width = max(1, int(width))
    percent = max(0, min(100, int(percent)))
    filled = round(width * percent / 100)
    filled = max(0, min(width, filled))
    return f"[{'█' * filled}{'░' * (width - filled)}]"


def _context_budget_level_for_percent(percent: int) -> str:
    percent = max(0, min(100, int(percent)))
    if percent <= 12:
        return "tiny"
    if percent <= 33:
        return "small"
    if percent <= 66:
        return "medium"
    if percent <= 85:
        return "large"
    return "full"


def _normalize_context_budget_percent(args: dict[str, Any]) -> tuple[int, str]:
    raw_percent = args.get("percent")
    raw_level = str(args.get("level") or "").strip().lower()
    percent_was_explicit = raw_percent is not None and str(raw_percent).strip() != ""
    if percent_was_explicit:
        try:
            percent = int(float(raw_percent))
        except (TypeError, ValueError):
            percent = 100
    elif raw_level:
        percent = CONTEXT_BUDGET_LEVEL_LOOKUP.get(raw_level, 100)
    else:
        percent = 100
    percent = max(0, min(100, int(percent)))
    if not percent_was_explicit and raw_level and raw_level in CONTEXT_BUDGET_LEVEL_LOOKUP:
        level = raw_level
    else:
        level = _context_budget_level_for_percent(percent)
    return percent, level


def _extract_inline_context_budget(args: dict[str, Any]) -> tuple[int, str, int] | None:
    if not isinstance(args, dict):
        return None
    percent_found = False
    normalized: dict[str, Any] = {}
    for key in INLINE_CONTEXT_PERCENT_KEYS:
        value = args.get(key)
        if value is None or str(value).strip() == "":
            continue
        normalized["percent"] = value
        percent_found = True
        break
    for key in INLINE_CONTEXT_LEVEL_KEYS:
        value = str(args.get(key) or "").strip().lower()
        if value:
            normalized["level"] = value
            break
    if not percent_found and not normalized.get("level"):
        return None
    turns = 1
    for key in INLINE_CONTEXT_TURN_KEYS:
        value = args.get(key)
        if value is None or str(value).strip() == "":
            continue
        try:
            turns = int(value)
        except (TypeError, ValueError):
            turns = 1
        break
    turns = max(1, min(5, turns))
    percent, level = _normalize_context_budget_percent(normalized)
    return percent, level, turns


def _default_context_budget_state() -> dict[str, Any]:
    percent = 66
    return {
        "status": "default",
        "tool": "context",
        "level": _context_budget_level_for_percent(percent),
        "percent": percent,
        "turns_remaining": 1,
        "revision": 0,
        "updated_at": "",
        "reason": "",
        "brief": "",
        "message": "当前使用默认自动上下文预算。",
        "context_budget_bar": _context_budget_bar(percent),
        "context_budget_label": _context_budget_level_for_percent(percent),
        "applies_from": "next_model_turn",
    }


def load_context_budget(config: Any) -> dict[str, Any]:
    payload = _load_json_file(_context_budget_path(config))
    if not payload:
        return _default_context_budget_state()
    percent, level = _normalize_context_budget_percent(payload)
    turns_remaining = max(0, int(payload.get("turns_remaining") or payload.get("turns") or 0))
    revision = max(0, int(payload.get("revision") or 0))
    active = bool(payload.get("active", True))
    if percent < 100 and turns_remaining <= 0:
        turns_remaining = 1
    return {
        "status": "active" if active or percent < 100 else "default",
        "tool": "context",
        "level": str(payload.get("level") or level or _context_budget_level_for_percent(percent)),
        "percent": percent,
        "turns_remaining": turns_remaining,
        "revision": revision,
        "updated_at": str(payload.get("updated_at") or ""),
        "reason": str(payload.get("reason") or ""),
        "brief": str(payload.get("brief") or ""),
        "message": str(payload.get("message") or ""),
        "context_budget_bar": _context_budget_bar(percent),
        "context_budget_label": str(payload.get("context_budget_label") or level or _context_budget_level_for_percent(percent)),
        "applies_from": "next_model_turn",
    }


def save_context_budget(
    config: Any,
    *,
    percent: int,
    level: str | None = None,
    turns_remaining: int = 1,
    reason: str = "",
    brief: str = "",
    message: str = "",
) -> dict[str, Any]:
    percent = max(0, min(100, int(percent)))
    turns_remaining = max(1, min(5, int(turns_remaining or 1)))
    current = _load_json_file(_context_budget_path(config))
    revision = max(0, int(current.get("revision") or 0)) + 1
    resolved_level = str(level or current.get("level") or _context_budget_level_for_percent(percent)).strip().lower()
    state = {
        "status": "active",
        "tool": "context",
        "level": resolved_level,
        "percent": percent,
        "turns_remaining": turns_remaining,
        "revision": revision,
        "updated_at": _utc_now_text(),
        "reason": reason.strip(),
        "brief": brief.strip(),
        "message": message.strip(),
        "context_budget_bar": _context_budget_bar(percent),
        "context_budget_label": _context_budget_level_for_percent(percent),
        "applies_from": "next_model_turn",
        "active": True,
    }
    _write_json_file(_context_budget_path(config), state)
    return state


def consume_context_budget(config: Any, *, expected_revision: int) -> dict[str, Any] | None:
    path = _context_budget_path(config)
    current = _load_json_file(path)
    if not current:
        return None
    try:
        current_revision = int(current.get("revision") or 0)
    except (TypeError, ValueError):
        current_revision = 0
    if current_revision != int(expected_revision or 0):
        return None
    raw_percent = current.get("percent")
    percent = 100 if raw_percent is None or raw_percent == "" else max(0, min(100, int(raw_percent)))
    turns_remaining = max(0, int(current.get("turns_remaining") or 0))
    if percent >= 100 or turns_remaining <= 1:
        if path.exists():
            path.unlink()
        return _default_context_budget_state()
    next_state = dict(current)
    next_state["turns_remaining"] = turns_remaining - 1
    next_state["revision"] = current_revision + 1
    next_state["updated_at"] = _utc_now_text()
    _write_json_file(path, next_state)
    return next_state


def _update_plan_path(config: Any) -> Path:
    return _context_budget_root(config) / UPDATE_PLAN_STATE_FILE_NAME


def _normalize_update_plan_mode(value: Any, *, default: str = "todo") -> str:
    raw = str(value or "").strip().lower().replace("_", "-")
    aliases = {
        "": default,
        "simple": "todo",
        "task": "todo",
        "tasks": "todo",
        "todo": "todo",
        "to-do": "todo",
        "medium": "todo",
        "basic": "todo",
        "advanced": "plan",
        "complex": "plan",
        "phase": "plan",
        "phased": "plan",
        "plan": "plan",
    }
    return aliases.get(raw, default if default in {"todo", "plan"} else "todo")


def _normalize_update_plan_action(value: Any) -> str:
    raw = str(value or "").strip().lower().replace("_", "-")
    aliases = {
        "": "status",
        "show": "status",
        "inspect": "status",
        "get": "status",
        "status": "status",
        "create": "start",
        "begin": "start",
        "start": "start",
        "set": "update",
        "patch": "update",
        "progress": "update",
        "update": "update",
        "done": "complete",
        "finish": "complete",
        "finished": "complete",
        "complete": "complete",
        "completed": "complete",
        "clear": "reset",
        "reset": "reset",
    }
    return aliases.get(raw, raw if raw in {"status", "start", "update", "complete", "reset"} else "status")


def _normalize_update_plan_item_status(value: Any, *, default: str = "pending") -> str:
    raw = str(value or "").strip().lower().replace("_", "-")
    aliases = {
        "": default,
        "todo": "pending",
        "queued": "pending",
        "wait": "pending",
        "waiting": "pending",
        "pending": "pending",
        "doing": "in_progress",
        "active": "in_progress",
        "current": "in_progress",
        "progress": "in_progress",
        "in-progress": "in_progress",
        "running": "in_progress",
        "ok": "done",
        "success": "done",
        "succeed": "done",
        "succeeded": "done",
        "complete": "done",
        "completed": "done",
        "done": "done",
        "finish": "done",
        "finished": "done",
        "fail": "blocked",
        "failed": "blocked",
        "block": "blocked",
        "blocked": "blocked",
        "pause": "blocked",
        "paused": "blocked",
    }
    return aliases.get(raw, default if default in {"pending", "in_progress", "done", "blocked"} else "pending")


def _normalize_update_plan_phase(value: Any) -> str:
    raw = " ".join(str(value or "").strip().split())
    if not raw:
        return ""
    return raw[:32]


def _normalize_update_plan_item_id(value: Any, *, fallback: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        raw = fallback
    cleaned = re.sub(r"[^A-Za-z0-9_.:-]+", "-", raw).strip("-")
    return (cleaned or fallback)[:32]


def _normalize_update_plan_item(raw: Any, *, index: int, mode: str) -> dict[str, Any] | None:
    fallback_id = f"T{index:02d}" if mode == "todo" else f"P{index:02d}"
    if isinstance(raw, dict):
        phase = _normalize_update_plan_phase(raw.get("phase") or raw.get("stage"))
        item_id = _normalize_update_plan_item_id(
            raw.get("id") or raw.get("step_id") or raw.get("key") or raw.get("name"),
            fallback=fallback_id,
        )
        title = str(raw.get("title") or raw.get("task") or raw.get("step") or raw.get("summary") or "").strip()
        status = _normalize_update_plan_item_status(raw.get("status") or raw.get("state"), default="pending")
        note = str(raw.get("note") or raw.get("notes") or raw.get("message") or "").strip()
    else:
        phase = ""
        item_id = fallback_id
        title = str(raw or "").strip()
        status = "pending"
        note = ""
    if not title and not note:
        return None
    return {
        "id": item_id,
        "title": title or note,
        "status": status,
        "phase": phase,
        "note": note,
        "updated_at": _utc_now_text(),
    }


def _normalize_update_plan_items(raw_items: Any, *, mode: str) -> list[dict[str, Any]]:
    if isinstance(raw_items, (list, tuple)):
        raw_list = list(raw_items)
    elif isinstance(raw_items, str) and raw_items.strip():
        raw_list = [line.strip() for line in raw_items.splitlines() if line.strip()]
    else:
        raw_list = []
    items: list[dict[str, Any]] = []
    for index, raw in enumerate(raw_list[:MAX_UPDATE_PLAN_ITEMS], start=1):
        item = _normalize_update_plan_item(raw, index=index, mode=mode)
        if item is not None:
            items.append(item)
    return items


def _load_update_plan_state(config: Any) -> dict[str, Any]:
    path = _update_plan_path(config)
    raw = _load_json_file(path)
    if not raw:
        return {
            "version": 1,
            "mode": "todo",
            "title": "",
            "items": [],
            "current_step_id": "",
            "next": "",
            "revision": 0,
            "created_at": "",
            "updated_at": "",
        }
    mode = _normalize_update_plan_mode(raw.get("mode"), default="todo")
    items = _normalize_update_plan_items(raw.get("items") or [], mode=mode)
    try:
        revision = max(0, int(raw.get("revision") or 0))
    except (TypeError, ValueError):
        revision = 0
    return {
        "version": 1,
        "mode": mode,
        "title": str(raw.get("title") or "").strip(),
        "items": items,
        "current_step_id": str(raw.get("current_step_id") or "").strip(),
        "next": str(raw.get("next") or "").strip(),
        "revision": revision,
        "created_at": str(raw.get("created_at") or ""),
        "updated_at": str(raw.get("updated_at") or ""),
    }


def _write_update_plan_state(config: Any, state: dict[str, Any]) -> None:
    _write_json_file(_update_plan_path(config), state)


def _update_plan_counts(items: list[dict[str, Any]]) -> tuple[int, int]:
    total = len(items)
    completed = sum(1 for item in items if str(item.get("status") or "") == "done")
    return completed, total


def _update_plan_active_item(items: list[dict[str, Any]]) -> dict[str, Any] | None:
    for status in ("in_progress", "blocked", "pending"):
        for item in items:
            if str(item.get("status") or "") == status:
                return dict(item)
    return dict(items[-1]) if items else None


def _update_plan_status_from_items(items: list[dict[str, Any]]) -> str:
    if not items:
        return "empty"
    statuses = {str(item.get("status") or "pending") for item in items}
    if "blocked" in statuses:
        return "blocked"
    if statuses == {"done"}:
        return "done"
    if "in_progress" in statuses:
        return "in_progress"
    return "pending"


def _update_plan_payload(
    *,
    action: str,
    state: dict[str, Any],
    brief: str = "",
    message: str = "",
    needs_review: bool = False,
) -> dict[str, Any]:
    items = [dict(item) for item in state.get("items") or [] if isinstance(item, dict)]
    completed, total = _update_plan_counts(items)
    active_item = _update_plan_active_item(items)
    payload = {
        "status": "ok",
        "tool": "update_plan",
        "action": action,
        "mode": _normalize_update_plan_mode(state.get("mode"), default="todo"),
        "title": str(state.get("title") or "").strip(),
        "items": items,
        "active_item": active_item or {},
        "current_step_id": str((active_item or {}).get("id") or state.get("current_step_id") or "").strip(),
        "completed_count": completed,
        "total_count": total,
        "plan_status": _update_plan_status_from_items(items),
        "next": str(state.get("next") or "").strip(),
        "revision": int(state.get("revision") or 0),
        "updated_at": str(state.get("updated_at") or ""),
        "brief": brief or "更新计划",
        "message": message,
        "needs_review": bool(needs_review),
        "review_trigger": "planner" if needs_review else "",
    }
    if total:
        payload["progress_text"] = f"{completed}/{total}"
    return payload


def _merge_update_plan_items(
    existing: list[dict[str, Any]],
    updates: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    merged = [dict(item) for item in existing]
    index_by_id = {str(item.get("id") or ""): index for index, item in enumerate(merged)}
    for item in updates:
        item_id = str(item.get("id") or "").strip()
        if item_id in index_by_id:
            target = dict(merged[index_by_id[item_id]])
            for key in ("title", "status", "phase", "note"):
                value = item.get(key)
                if value not in {None, ""}:
                    target[key] = value
            target["updated_at"] = _utc_now_text()
            merged[index_by_id[item_id]] = target
        else:
            index_by_id[item_id] = len(merged)
            merged.append(dict(item))
        if len(merged) >= MAX_UPDATE_PLAN_ITEMS:
            break
    return merged[:MAX_UPDATE_PLAN_ITEMS]


def _execute_update_plan_tool(args: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    action = _normalize_update_plan_action(args.get("action"))
    raw_items = args.get("items")
    if action == "status" and raw_items:
        action = "start"
    brief = str(args.get("brief") or "").strip() or "更新计划"

    if action == "reset":
        path = _update_plan_path(context.config)
        if path.exists():
            path.unlink()
        state = _load_update_plan_state(context.config)
        return _update_plan_payload(
            action=action,
            state=state,
            brief=brief,
            message="计划已清空。",
            needs_review=False,
        )

    state = _load_update_plan_state(context.config)
    if action == "status":
        return _update_plan_payload(
            action=action,
            state=state,
            brief=brief or "查看计划",
            message="当前计划状态已读取。" if state.get("items") else "当前没有活动计划。",
            needs_review=False,
        )

    mode = _normalize_update_plan_mode(args.get("mode"), default=str(state.get("mode") or "todo"))
    now = _utc_now_text()
    if not state.get("created_at"):
        state["created_at"] = now
    state["updated_at"] = now
    state["mode"] = mode
    if args.get("title") not in {None, ""}:
        state["title"] = str(args.get("title") or "").strip()
    if args.get("next") not in {None, ""}:
        state["next"] = str(args.get("next") or "").strip()
    if args.get("phase") not in {None, ""}:
        state["phase"] = _normalize_update_plan_phase(args.get("phase"))

    updates = _normalize_update_plan_items(raw_items, mode=mode)
    replace_items = bool(args.get("replace_items", action == "start"))
    if updates:
        if replace_items or action == "start":
            state["items"] = updates
        else:
            state["items"] = _merge_update_plan_items(
                [dict(item) for item in state.get("items") or [] if isinstance(item, dict)],
                updates,
            )

    step_id = str(args.get("step_id") or args.get("id") or "").strip()
    step_title = str(args.get("step_title") or "").strip()
    note = str(args.get("note") or args.get("message") or "").strip()
    step_status_raw = args.get("status") or args.get("state")
    step_status = _normalize_update_plan_item_status(
        step_status_raw,
        default="done" if action == "complete" else "in_progress",
    )
    phase = _normalize_update_plan_phase(args.get("phase"))
    items = [dict(item) for item in state.get("items") or [] if isinstance(item, dict)]
    if step_id:
        found = False
        for item in items:
            if str(item.get("id") or "") != step_id:
                continue
            if step_title:
                item["title"] = step_title
            if step_status_raw not in {None, ""} or action == "complete":
                item["status"] = step_status
            if note:
                item["note"] = note
            if phase:
                item["phase"] = phase
            item["updated_at"] = now
            found = True
            break
        if not found and len(items) < MAX_UPDATE_PLAN_ITEMS:
            items.append(
                {
                    "id": _normalize_update_plan_item_id(step_id, fallback=f"T{len(items) + 1:02d}"),
                    "title": step_title or note or step_id,
                    "status": step_status,
                    "phase": phase,
                    "note": note,
                    "updated_at": now,
                }
            )
        state["items"] = items
        state["current_step_id"] = step_id
    elif action == "complete" and items:
        active_id = str(state.get("current_step_id") or "").strip()
        target_index = -1
        if active_id:
            for index, item in enumerate(items):
                if str(item.get("id") or "") == active_id:
                    target_index = index
                    break
        if target_index < 0:
            for index, item in enumerate(items):
                if str(item.get("status") or "") in {"in_progress", "blocked"}:
                    target_index = index
                    break
        if target_index >= 0:
            items[target_index]["status"] = "done"
            if note:
                items[target_index]["note"] = note
            items[target_index]["updated_at"] = now
            state["items"] = items

    active = _update_plan_active_item([dict(item) for item in state.get("items") or [] if isinstance(item, dict)])
    if active:
        state["current_step_id"] = str(active.get("id") or "")
    try:
        state["revision"] = max(0, int(state.get("revision") or 0)) + 1
    except (TypeError, ValueError):
        state["revision"] = 1
    _write_update_plan_state(context.config, state)

    message_map = {
        "start": "计划已建立，执行位按步骤推进；每完成一步继续调用 update_plan。",
        "update": "计划已更新，主角色将复审方向后继续。",
        "complete": "计划已收束，主角色将做最终审查。",
    }
    return _update_plan_payload(
        action=action,
        state=state,
        brief=brief,
        message=message_map.get(action, "计划已更新。"),
        needs_review=action in {"start", "update", "complete"},
    )


def _aidebug_root(config: Any) -> Path:
    root_dir = Path(getattr(config, "root_dir", Path.cwd())).expanduser()
    aitermux_home = root_dir.parent if root_dir.name == "projectling" else root_dir
    return Path(os.environ.get("AITERMUX_AIDEBUG_DIR", str(aitermux_home / "aidebug"))).expanduser()


def _terminal_output_dir(config: Any) -> Path:
    output_dir = _aidebug_root(config) / "projectling" / TERMINAL_OUTPUT_DIR_NAME
    output_dir.mkdir(parents=True, exist_ok=True)
    return output_dir


def _terminal_state_path(config: Any) -> Path:
    return _terminal_output_dir(config) / TERMINAL_STATE_FILE_NAME


def _terminal_echo_suppress_path(config: Any, session_name: str) -> Path:
    return _terminal_output_dir(config) / f"{session_name}.suppress-echo"


def _queue_terminal_echo_suppression(config: Any, session_name: str, command: str) -> str:
    path = _terminal_echo_suppress_path(config, session_name)
    cleaned = command.strip()
    if not cleaned:
        return str(path)
    existing: list[str] = []
    if path.is_file():
        existing = [line for line in path.read_text(encoding="utf-8", errors="replace").splitlines() if line.strip()]
    existing.append(cleaned)
    path.write_text("\n".join(existing[-20:]) + "\n", encoding="utf-8")
    return str(path)


def _terminal_log_filter_script(config: Any) -> Path:
    path = _terminal_output_dir(config) / "clean-terminal-log.py"
    script = r'''from __future__ import annotations

import os
from pathlib import Path
import re
import sys

ANSI_RE = re.compile(
    rb"\x1b\][^\x07]*(?:\x07|\x1b\\)"
    rb"|\x1b\[[0-9;?]*[ -/]*[@-~]"
    rb"|\x1b[=>]"
)
CONTROL_RE = re.compile(rb"[\x00-\x08\x0b\x0c\x0d\x0e-\x1f\x7f]")
ORPHAN_ANSI_TEXT_RE = re.compile(r"\[(?:\??[0-9;]{1,40})[A-Za-z]")


SUPPRESS_PATH = Path(os.environ.get("PROJECTLING_TERMINAL_ECHO_SUPPRESS_PATH", "") or "/dev/null")


def clean(chunk: bytes) -> str:
    chunk = ANSI_RE.sub(b"", chunk)
    while b"\x08" in chunk:
        chunk = re.sub(rb".?\x08", b"", chunk)
    chunk = CONTROL_RE.sub(b"", chunk)
    text = chunk.decode("utf-8", errors="replace")
    return ORPHAN_ANSI_TEXT_RE.sub("", text)


def load_suppressed_commands() -> list[str]:
    try:
        return [line.strip() for line in SUPPRESS_PATH.read_text(encoding="utf-8", errors="replace").splitlines() if line.strip()]
    except OSError:
        return []


def remove_suppressed_command(command: str) -> None:
    commands = load_suppressed_commands()
    remaining: list[str] = []
    removed = False
    for entry in commands:
        if not removed and entry == command:
            removed = True
            continue
        remaining.append(entry)
    try:
        if remaining:
            SUPPRESS_PATH.write_text("\n".join(remaining) + "\n", encoding="utf-8")
        else:
            SUPPRESS_PATH.unlink(missing_ok=True)
    except OSError:
        pass


def should_skip_line(line: str) -> bool:
    stripped = line.strip()
    if not stripped:
        return False
    if any(marker in stripped for marker in ("└─", "┌─", " λ ", " main")):
        return True
    for command in load_suppressed_commands():
        if stripped == command or stripped.endswith(command):
            return True
        if command in stripped and any(marker in stripped for marker in ("└─", "┌─", " λ ", "◈", "", "localhost", "~/")):
            return True
    return False


def main() -> int:
    pending = ""
    while True:
        chunk = os.read(0, 4096)
        if not chunk:
            break
        pending += clean(chunk)
        while "\n" in pending:
            line, pending = pending.split("\n", 1)
            if should_skip_line(line):
                continue
            sys.stdout.write(line + "\n")
        sys.stdout.flush()
    if pending and not should_skip_line(pending):
        sys.stdout.write(pending)
        sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
'''
    if not path.is_file() or path.read_text(encoding="utf-8", errors="replace") != script:
        path.write_text(script, encoding="utf-8")
        try:
            path.chmod(0o755)
        except OSError:
            pass
    return path


def _load_terminal_state(config: Any) -> dict[str, Any]:
    path = _terminal_state_path(config)
    if not path.is_file():
        return {"sessions": {}, "latest": None}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"sessions": {}, "latest": None}
    sessions = data.get("sessions")
    if not isinstance(sessions, dict):
        sessions = {}
    latest = data.get("latest")
    return {"sessions": sessions, "latest": latest}


def _save_terminal_state(config: Any, state: dict[str, Any]) -> None:
    path = _terminal_state_path(config)
    _atomic_write_text_file(path, json.dumps(state, ensure_ascii=False, indent=2))


def _sanitize_terminal_session_name(value: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "-" for ch in value.strip())
    cleaned = "-".join(part for part in cleaned.split("-") if part)
    return cleaned[:48] or f"projectling-{int(time.time())}"


def _read_preview_from_file(path: Path, *, head_bytes: int = 6000, tail_bytes: int = 6000) -> tuple[str, bool]:
    if not path.is_file():
        return "", False
    size = path.stat().st_size
    if size <= MAX_TERMINAL_PREVIEW_CHARS:
        try:
            return path.read_text(encoding="utf-8", errors="replace"), False
        except Exception:
            return "", False
    try:
        with path.open("rb") as handle:
            head = handle.read(head_bytes)
            handle.seek(max(0, size - tail_bytes))
            tail = handle.read(tail_bytes)
    except Exception:
        return "", False
    head_text = head.decode("utf-8", errors="replace")
    tail_text = tail.decode("utf-8", errors="replace")
    return f"{head_text}\n...[middle omitted]...\n{tail_text}", True


def _count_file_lines(path: Path) -> int:
    if not path.is_file():
        return 0
    if path.stat().st_size == 0:
        return 0
    total = 0
    last_byte = b""
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                total += chunk.count(b"\n")
                if chunk:
                    last_byte = chunk[-1:]
    except Exception:
        return 0
    return total if last_byte == b"\n" else total + 1


def _file_size_text(path: Path) -> str:
    if not path.exists():
        return "0 B"
    size = float(path.stat().st_size)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024.0 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024.0
    return f"{int(path.stat().st_size)} B"


def _tmux_available() -> bool:
    return bool(shutil.which("tmux"))


def _termux_run_command_service_available() -> bool:
    return bool(shutil.which("am"))


def _termux_allow_external_apps_enabled() -> bool:
    path = Path.home() / ".termux" / "termux.properties"
    if not path.is_file():
        return False
    try:
        for raw_line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = (part.strip().lower() for part in line.split("=", 1))
            if key == "allow-external-apps":
                return value == "true"
    except OSError:
        return False
    return False


def _launch_termux_session(*, root_dir: Path, cwd: Path, session_name: str) -> dict[str, Any]:
    aitermux_home = root_dir.parent if root_dir.name == "projectling" else root_dir
    aidebug_dir = Path(os.environ.get("AITERMUX_AIDEBUG_DIR", str(aitermux_home / "aidebug"))).expanduser()
    launch_path = aidebug_dir / "projectling" / TERMINAL_OUTPUT_DIR_NAME / f"{session_name}.launch.sh"
    launch_path.parent.mkdir(parents=True, exist_ok=True)
    script = "#!/data/data/com.termux/files/usr/bin/sh\nexec tmux attach -t " + shlex.quote(session_name) + "\n"
    launch_path.write_text(script, encoding="utf-8")
    try:
        launch_path.chmod(0o755)
    except OSError:
        pass
    service_cmd = [
        "am",
        "startservice",
        "--user",
        "0",
        "-n",
        "com.termux/com.termux.app.RunCommandService",
        "-a",
        "com.termux.RUN_COMMAND",
        "--es",
        "com.termux.RUN_COMMAND_PATH",
        str(launch_path),
        "--es",
        "com.termux.RUN_COMMAND_WORKDIR",
        str(cwd),
        "--ez",
        "com.termux.RUN_COMMAND_BACKGROUND",
        "false",
        "--es",
        "com.termux.RUN_COMMAND_SESSION_ACTION",
        "0",
    ]
    completed = subprocess.run(service_cmd, capture_output=True, text=True)
    return {
        "returncode": completed.returncode,
        "stdout": completed.stdout.strip(),
        "stderr": completed.stderr.strip(),
        "command": " ".join(shlex.quote(part) for part in service_cmd),
        "launch_path": str(launch_path),
    }


def _terminal_log_metadata(log_path: Path) -> dict[str, Any]:
    preview, truncated = _read_preview_from_file(log_path)
    lines = _count_file_lines(log_path)
    bytes_size = log_path.stat().st_size if log_path.exists() else 0
    quoted = shlex.quote(str(log_path))
    return {
        "log_path": str(log_path),
        "log_lines": lines,
        "log_bytes": bytes_size,
        "log_size": _file_size_text(log_path),
        "log_preview": preview.strip(),
        "log_preview_truncated": truncated,
        "read_head_command": f"sed -n '1,120p' {quoted}",
        "read_tail_command": f"tail -n 160 {quoted}",
        "read_slice_command": f"sed -n 'START,ENDp' {quoted}",
    }


def _resolve_terminal_session(args: dict[str, Any], context: ToolContext) -> tuple[str, dict[str, Any] | None]:
    requested = str(args.get("session_name") or "").strip()
    state = _load_terminal_state(context.config)
    sessions = state.get("sessions") if isinstance(state.get("sessions"), dict) else {}
    if requested:
        name = _sanitize_terminal_session_name(requested)
        session = sessions.get(name)
        return name, session if isinstance(session, dict) else None
    latest = str(state.get("latest") or "").strip()
    if latest:
        session = sessions.get(latest)
        return latest, session if isinstance(session, dict) else None
    return "", None


def _tmux_has_session(session_name: str) -> bool:
    completed = subprocess.run(
        ["tmux", "has-session", "-t", session_name],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return completed.returncode == 0


def _terminal_command_gate(command: str, context: ToolContext) -> CommandDecision | None:
    decision = _analyze_command(command, context)
    if decision.action == "execute":
        return None
    return decision


def _terminal_ai_command(command: str) -> tuple[str, list[str]]:
    stripped = str(command or "").strip()
    if not stripped:
        return stripped, []
    try:
        tokens = _parse_tokens(stripped)
    except ValueError:
        return stripped, []
    if not tokens:
        return stripped, []
    first = tokens[0]
    if first == "command" or first in SHELL_BUILTINS_THAT_CANNOT_PERSIST or first in INTERACTIVE_COMMANDS:
        return stripped, []
    # Commands sent by the AI go through an interactive user shell. Prefixing
    # with `command` bypasses user aliases/functions such as cat->bat while
    # keeping the tmux session interactive for the human.
    env_prefix = "NO_COLOR=1 CLICOLOR=0 PAGER=cat BAT_PAGER=cat LESS=-FRX"
    return f"{env_prefix} command {stripped}", ["bypass_shell_aliases", "disable_color_and_pager"]


def _execute_terminal_start(args: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    if not _tmux_available():
        return {
            "status": "blocked",
            "tool": "terminal",
            "action": "start",
            "message": "缺少 tmux，无法建立可由用户和 AI 同时操作的终端会话。",
            "next_step": "先执行：pkg install tmux",
        }
    if not _termux_run_command_service_available():
        return {
            "status": "blocked",
            "tool": "terminal",
            "action": "start",
            "message": "当前环境没有 Android am 命令，无法自动新建 Termux 标签页。",
        }

    raw_name = str(args.get("session_name") or "").strip()
    session_name = _sanitize_terminal_session_name(raw_name or f"projectling-{time.strftime('%Y%m%d-%H%M%S')}")
    command = str(args.get("command") or "").strip()
    gate = _terminal_command_gate(command, context) if command else None
    if gate is not None:
        return {
            "status": "blocked",
            "tool": "terminal",
            "action": "start",
            "session_name": session_name,
            "command": command,
            "cwd": str(context.cwd),
            "risk": gate.risk,
            "reason": gate.reason,
            "message": gate.reason,
        }
    cwd_raw = str(args.get("cwd") or "").strip()
    cwd = Path(cwd_raw).expanduser() if cwd_raw else context.cwd
    if not cwd.is_absolute():
        cwd = (context.cwd / cwd).resolve()
    if not cwd.exists() or not cwd.is_dir():
        cwd = context.cwd

    output_dir = _terminal_output_dir(context.config)
    log_path = output_dir / f"{session_name}.log"
    log_filter = _terminal_log_filter_script(context.config)
    suppress_path = _terminal_echo_suppress_path(context.config, session_name)
    shell = _shell_program()

    if _tmux_has_session(session_name):
        return {
            "status": "error",
            "tool": "terminal",
            "action": "start",
            "session_name": session_name,
            "message": "同名 tmux 会话已经存在，请换一个 session_name 或使用 send/info。",
            **_terminal_log_metadata(log_path),
        }

    started_at = int(time.time())
    subprocess.run(["tmux", "new-session", "-d", "-s", session_name, "-c", str(cwd), shell], check=True)
    subprocess.run(
        [
            "tmux",
            "pipe-pane",
            "-o",
            "-t",
            session_name,
            (
                f"PROJECTLING_TERMINAL_ECHO_SUPPRESS_PATH={shlex.quote(str(suppress_path))} "
                f"{shlex.quote(sys.executable or 'python3')} -u {shlex.quote(str(log_filter))} "
                f">> {shlex.quote(str(log_path))}"
            ),
        ],
        check=True,
    )
    if command:
        sent_command, terminal_notes = _terminal_ai_command(command)
        _queue_terminal_echo_suppression(context.config, session_name, sent_command)
        subprocess.run(["tmux", "send-keys", "-l", "-t", session_name, sent_command], check=True)
        subprocess.run(["tmux", "send-keys", "-t", session_name, "C-m"], check=True)
    else:
        sent_command = ""
        terminal_notes = []

    launch = _launch_termux_session(
        root_dir=Path(getattr(context.config, "root_dir", context.cwd)).expanduser(),
        cwd=cwd,
        session_name=session_name,
    )
    state = _load_terminal_state(context.config)
    sessions = state.get("sessions") if isinstance(state.get("sessions"), dict) else {}
    sessions[session_name] = {
        "session_name": session_name,
        "cwd": str(cwd),
        "log_path": str(log_path),
        "created_at": started_at,
        "last_command": command,
        "last_sent_command": sent_command,
        "attach_script": launch.get("launch_path"),
        "suppress_path": str(suppress_path),
    }
    state["sessions"] = sessions
    state["latest"] = session_name
    _save_terminal_state(context.config, state)

    status = "ok" if launch.get("returncode") == 0 else "warning"
    setup_warning = ""
    if not _termux_allow_external_apps_enabled():
        setup_warning = (
            "如果新标签页没有自动弹出，请在 ~/.termux/termux.properties 设置 "
            "allow-external-apps=true 后执行 termux-reload-settings。"
        )
    return {
        "status": status,
        "tool": "terminal",
        "action": "start",
        "channel": "Terminal",
        "session_name": session_name,
        "command": command,
        "sent_command": sent_command,
        "terminal_safety": terminal_notes,
        "cwd": str(cwd),
        "tmux_target": session_name,
        "launch_returncode": launch.get("returncode"),
        "launch_stdout": launch.get("stdout"),
        "launch_stderr": launch.get("stderr"),
        "launch_command": launch.get("command"),
        "attach_script": launch.get("launch_path"),
        "message": "已创建 tmux 协作终端并请求 Termux 新建前台会话。" if status == "ok" else "tmux 会话已创建，但 Termux 新标签页启动命令返回非零状态。",
        "setup_warning": setup_warning,
        **_terminal_log_metadata(log_path),
    }


def _execute_terminal_send(args: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    if not _tmux_available():
        return {"status": "blocked", "tool": "terminal", "action": "send", "message": "缺少 tmux。"}
    session_name, session = _resolve_terminal_session(args, context)
    if not session_name or session is None:
        return {"status": "error", "tool": "terminal", "action": "send", "message": "没有可用 terminal 会话，请先 action=start。"}
    if not _tmux_has_session(session_name):
        return {
            "status": "error",
            "tool": "terminal",
            "action": "send",
            "session_name": session_name,
            "message": "tmux 会话不存在或已结束。",
        }
    command = str(args.get("command") or "").strip()
    enter = bool(args.get("enter", True))
    if not command:
        return {"status": "error", "tool": "terminal", "action": "send", "session_name": session_name, "message": "command 为空。"}
    gate = _terminal_command_gate(command, context)
    if gate is not None:
        return {
            "status": "blocked",
            "tool": "terminal",
            "action": "send",
            "channel": "Terminal",
            "session_name": session_name,
            "command": command,
            "risk": gate.risk,
            "reason": gate.reason,
            "message": gate.reason,
        }
    sent_command, terminal_notes = _terminal_ai_command(command)
    _queue_terminal_echo_suppression(context.config, session_name, sent_command)
    subprocess.run(["tmux", "send-keys", "-l", "-t", session_name, sent_command], check=True)
    if enter:
        subprocess.run(["tmux", "send-keys", "-t", session_name, "C-m"], check=True)
    log_path = Path(str(session.get("log_path") or _terminal_output_dir(context.config) / f"{session_name}.log"))
    session["last_command"] = command
    session["last_sent_command"] = sent_command
    session["suppress_path"] = str(_terminal_echo_suppress_path(context.config, session_name))
    state = _load_terminal_state(context.config)
    sessions = state.get("sessions") if isinstance(state.get("sessions"), dict) else {}
    sessions[session_name] = session
    state["sessions"] = sessions
    state["latest"] = session_name
    _save_terminal_state(context.config, state)
    return {
        "status": "ok",
        "tool": "terminal",
        "action": "send",
        "channel": "Terminal",
        "session_name": session_name,
        "command": command,
        "sent_command": sent_command,
        "terminal_safety": terminal_notes,
        "enter": enter,
        "message": "已发送到协作终端。",
        **_terminal_log_metadata(log_path),
    }


def _execute_terminal_info(args: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    session_name, session = _resolve_terminal_session(args, context)
    if not session_name or session is None:
        return {"status": "empty", "tool": "terminal", "action": "info", "message": "没有记录中的 terminal 会话。"}
    log_path = Path(str(session.get("log_path") or _terminal_output_dir(context.config) / f"{session_name}.log"))
    return {
        "status": "ok" if _tmux_available() and _tmux_has_session(session_name) else "stopped",
        "tool": "terminal",
        "action": "info",
        "channel": "Terminal",
        "session_name": session_name,
        "cwd": str(session.get("cwd") or ""),
        "last_command": str(session.get("last_command") or ""),
        "attach_script": str(session.get("attach_script") or ""),
        **_terminal_log_metadata(log_path),
    }


def _execute_terminal_stop(args: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    if not _tmux_available():
        return {"status": "blocked", "tool": "terminal", "action": "stop", "message": "缺少 tmux。"}
    session_name, session = _resolve_terminal_session(args, context)
    if not session_name or session is None:
        return {"status": "empty", "tool": "terminal", "action": "stop", "message": "没有可停止的 terminal 会话。"}
    existed = _tmux_has_session(session_name)
    if existed:
        subprocess.run(["tmux", "kill-session", "-t", session_name], check=False)
    log_path = Path(str(session.get("log_path") or _terminal_output_dir(context.config) / f"{session_name}.log"))
    state = _load_terminal_state(context.config)
    sessions = state.get("sessions") if isinstance(state.get("sessions"), dict) else {}
    stored = sessions.get(session_name) if isinstance(sessions.get(session_name), dict) else {}
    stored.update({"stopped_at": int(time.time()), "alive": False})
    sessions[session_name] = stored
    state["sessions"] = sessions
    _save_terminal_state(context.config, state)
    return {
        "status": "ok" if existed else "stopped",
        "tool": "terminal",
        "action": "stop",
        "channel": "Terminal",
        "session_name": session_name,
        "message": "已停止协作终端。" if existed else "tmux 会话之前已经结束。",
        **_terminal_log_metadata(log_path),
    }


def _execute_terminal_tool(args: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    action = str(args.get("action") or "start").strip().lower()
    if action == "start":
        return _execute_terminal_start(args, context)
    if action == "send":
        return _execute_terminal_send(args, context)
    if action == "info":
        return _execute_terminal_info(args, context)
    if action in {"stop", "close"}:
        return _execute_terminal_stop(args, context)
    return {
        "status": "error",
        "tool": "terminal",
        "action": action,
        "message": "未知 terminal action。支持 start/send/info/stop。",
    }


def _aidebug_logs_dir(config: Any) -> Path:
    path = _aidebug_root(config) / "logs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _safe_aidebug_path(config: Any, relative_path: str) -> Path | None:
    root = _aidebug_root(config).resolve()
    rel = (relative_path or "logs/startup.log").strip().lstrip("/")
    if not rel or ".." in Path(rel).parts or "projectying" in Path(rel).parts:
        return None
    path = (root / rel).resolve()
    try:
        path.relative_to(root)
    except ValueError:
        return None
    return path


def _aidebug_file_meta(path: Path, root: Path) -> dict[str, Any]:
    return {
        "path": str(path),
        "relative_path": str(path.relative_to(root)),
        "lines": _count_file_lines(path),
        "bytes": path.stat().st_size if path.exists() else 0,
        "size": _file_size_text(path),
        "mtime": int(path.stat().st_mtime) if path.exists() else 0,
        "read_tail_command": f"tail -n 160 {shlex.quote(str(path))}",
        "read_head_command": f"sed -n '1,120p' {shlex.quote(str(path))}",
        "read_slice_command": f"sed -n 'START,ENDp' {shlex.quote(str(path))}",
    }


def _aidebug_list_files(config: Any) -> list[dict[str, Any]]:
    root = _aidebug_root(config)
    root.mkdir(parents=True, exist_ok=True)
    entries: list[dict[str, Any]] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        if "projectying" in path.parts:
            continue
        if path.suffix not in {".log", ".err", ".out", ".meta", ".json"}:
            continue
        entries.append(_aidebug_file_meta(path, root))
    return entries


def _execute_aidebug_status(args: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    del args
    files = _aidebug_list_files(context.config)
    terminal_dir = _terminal_output_dir(context.config)
    summary_lines = [
        f"aidebug_dir={_aidebug_root(context.config)}",
        "projectying_excluded=1",
        f"files={len(files)}",
    ]
    for item in files[:40]:
        summary_lines.append(
            f"{item['relative_path']} lines={item['lines']} bytes={item['bytes']} mtime={item['mtime']}"
        )
    if len(files) > 40:
        summary_lines.append(f"... +{len(files) - 40} files")
    return {
        "status": "ok",
        "tool": "aidebug",
        "action": "status",
        "aidebug_dir": str(_aidebug_root(context.config)),
        "terminal_output_dir": str(terminal_dir),
        "files": files,
        "stdout": "\n".join(summary_lines),
    }


def _execute_aidebug_read(args: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    rel = str(args.get("path") or "logs/startup.log")
    path = _safe_aidebug_path(context.config, rel)
    if path is None:
        return {
            "status": "blocked",
            "tool": "aidebug",
            "action": "read",
            "message": "只能读取 aidebug 内部路径，且不能包含 projectying 或 ..。",
        }
    if not path.is_file():
        return {
            "status": "error",
            "tool": "aidebug",
            "action": "read",
            "path": str(path),
            "message": "aidebug 文件不存在。",
        }

    mode = str(args.get("mode") or "tail").strip().lower()
    lines_raw = args.get("lines")
    try:
        lines = max(1, min(1000, int(lines_raw if lines_raw not in {None, ""} else 160)))
    except (TypeError, ValueError):
        lines = 160
    start_raw = args.get("start_line")
    end_raw = args.get("end_line")
    text = ""
    if mode == "head":
        completed = subprocess.run(["head", "-n", str(lines), str(path)], capture_output=True, text=True)
        text = completed.stdout
    elif mode == "slice" and start_raw not in {None, ""} and end_raw not in {None, ""}:
        try:
            start_line = max(1, int(start_raw))
            end_line = max(start_line, int(end_raw))
        except (TypeError, ValueError):
            start_line, end_line = 1, lines
        completed = subprocess.run(["sed", "-n", f"{start_line},{end_line}p", str(path)], capture_output=True, text=True)
        text = completed.stdout
    else:
        completed = subprocess.run(["tail", "-n", str(lines), str(path)], capture_output=True, text=True)
        mode = "tail"
        text = completed.stdout

    compacted, truncated = _middle_truncate_text(text, MAX_AIDEBUG_READ_CHARS)
    root = _aidebug_root(context.config)
    return {
        "status": "ok",
        "tool": "aidebug",
        "action": "read",
        "mode": mode,
        **_aidebug_file_meta(path, root),
        "stdout": compacted,
        "stdout_truncated": truncated,
    }


def _execute_aidebug_event(args: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    component = str(args.get("component") or "projectling").strip().lower()
    component = re.sub(r"[^a-z0-9_-]+", "-", component)[:40] or "projectling"
    if component == "projectying":
        return {
            "status": "blocked",
            "tool": "aidebug",
            "action": "event",
            "message": "projectying 使用独立 Aidebug 链路，这里不写入。",
        }
    message = str(args.get("message") or "").strip()
    if not message:
        return {
            "status": "error",
            "tool": "aidebug",
            "action": "event",
            "message": "message 为空。",
        }
    logs_dir = _aidebug_logs_dir(context.config)
    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    line = f"{ts} {component} {message}\n"
    for path in (logs_dir / "events.log", logs_dir / f"{component}.log"):
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line)
    return {
        "status": "ok",
        "tool": "aidebug",
        "action": "event",
        "component": component,
        "log_path": str(logs_dir / f"{component}.log"),
        "message": message,
    }


def _execute_aidebug_health(args: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    del args
    script = _aidebug_root(context.config) / "runner" / "aidebug_health.py"
    if not script.is_file():
        return {
            "status": "error",
            "tool": "aidebug",
            "action": "health",
            "message": "缺少 aidebug health runner。",
        }
    completed = subprocess.run([sys.executable, str(script), "--json"], capture_output=True, text=True, timeout=45)
    if completed.returncode not in {0, 1}:
        return {
            "status": "error",
            "tool": "aidebug",
            "action": "health",
            "stdout": completed.stdout,
            "stderr": completed.stderr,
            "message": f"health runner rc={completed.returncode}",
        }
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError:
        return {
            "status": "error",
            "tool": "aidebug",
            "action": "health",
            "stdout": completed.stdout,
            "stderr": completed.stderr,
            "message": "health runner 输出不是 JSON。",
        }
    lines = [f"overall {payload.get('overall_status')} score={payload.get('overall_score')}"]
    for check in payload.get("checks") or []:
        if not isinstance(check, dict):
            continue
        lines.append(f"- {check.get('name')} {check.get('status')} score={check.get('score')}")
        if check.get("next_action"):
            lines.append(f"  next: {check.get('next_action')}")
    history = payload.get("history") or {}
    if isinstance(history, dict):
        lines.append(
            f"history runs={history.get('run_count')} trend={history.get('trend')} "
            f"delta={history.get('current_delta')} avg={history.get('recent_average')}"
        )
    return {
        "status": "ok" if payload.get("overall_status") in {"ok", "warn"} else "error",
        "tool": "aidebug",
        "action": "health",
        "overall_status": payload.get("overall_status"),
        "overall_score": payload.get("overall_score"),
        "health_path": str(_aidebug_root(context.config) / "logs" / "aidebug-health.json"),
        "note_path": str(_aidebug_root(context.config) / "notes" / "aidebug-health.md"),
        "checks": payload.get("checks") or [],
        "stdout": "\n".join(lines),
    }


def _execute_aidebug_tool(args: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    action = str(args.get("action") or "status").strip().lower()
    if action == "status":
        return _execute_aidebug_status(args, context)
    if action == "read":
        return _execute_aidebug_read(args, context)
    if action == "event":
        return _execute_aidebug_event(args, context)
    if action == "health":
        return _execute_aidebug_health(args, context)
    return {
        "status": "error",
        "tool": "aidebug",
        "action": action,
        "message": "未知 aidebug action。支持 status/read/event/health。",
    }


def _resolve_tool_cwd(args: dict[str, Any], context: ToolContext) -> Path:
    raw = str(args.get("cwd") or "").strip()
    cwd = Path(raw).expanduser() if raw else context.cwd
    if not cwd.is_absolute():
        cwd = (context.cwd / cwd).resolve()
    if not cwd.exists() or not cwd.is_dir():
        return context.cwd
    return cwd


def _normalize_patch_path_token(path: str) -> str:
    token = str(path or "").strip().strip('"').strip("'")
    if not token or token == "/dev/null":
        return token
    if token.startswith(("a/", "b/")):
        token = token[2:]
    return token.strip()


def _patch_changed_paths(patch_text: str) -> list[str]:
    paths: list[str] = []
    seen: set[str] = set()
    for raw_line in patch_text.splitlines():
        line = raw_line.rstrip("\n")
        candidates: list[str] = []
        if line.startswith("diff --git "):
            try:
                parts = shlex.split(line)
            except ValueError:
                parts = line.split()
            if len(parts) >= 4:
                candidates.extend([parts[2], parts[3]])
        elif line.startswith("Index: "):
            candidates.append(line[7:].strip())
        elif line.startswith("--- ") or line.startswith("+++ "):
            if _is_context_diff_range_header(line):
                continue
            part = line[4:].split("\t", 1)[0].strip()
            candidates.append(part)
        elif line.startswith("*** Add File: "):
            candidates.append(line[len("*** Add File: ") :].strip())
        elif line.startswith("*** Delete File: "):
            candidates.append(line[len("*** Delete File: ") :].strip())
        elif line.startswith("*** Update File: "):
            candidates.append(line[len("*** Update File: ") :].strip())
        elif line.startswith("*** Move to: "):
            candidates.append(line[len("*** Move to: ") :].strip())
        elif line.startswith("*** ") and not line.startswith(("*** Begin Patch", "*** End Patch")):
            candidate = _context_diff_header_path(line)
            if candidate:
                candidates.append(candidate)
        for candidate in candidates:
            if candidate == "/dev/null":
                continue
            candidate = _normalize_patch_path_token(candidate)
            if not candidate or candidate in seen:
                continue
            seen.add(candidate)
            paths.append(candidate)
    return paths


def _validate_patch_paths(paths: list[str]) -> tuple[bool, str]:
    for path in paths:
        candidate = Path(path)
        if candidate.is_absolute() or ".." in candidate.parts:
            return False, f"patch path escapes cwd: {path}"
    return True, ""


def _patch_text_from_args(args: dict[str, Any]) -> tuple[str, list[str]]:
    notes: list[str] = []
    for key in PATCH_TEXT_ARG_KEYS:
        if key not in args:
            continue
        value = args.get(key)
        if value is None:
            continue
        if isinstance(value, str):
            text = value
        elif isinstance(value, list) and all(isinstance(item, str) for item in value):
            text = "\n".join(value)
            notes.append(f"joined_{key}_list")
        else:
            text = str(value)
        if not text.strip():
            continue
        if key != "patch":
            notes.append(f"used_{key}_alias")
        return text, notes
    return "", notes


def _has_structured_patch_operation(text: str) -> bool:
    return bool(
        re.search(r"(?m)^\*\*\* (?:Add|Update|Delete) File:\s+\S+", text)
    )


def _ensure_structured_patch_wrapper(text: str, notes: list[str]) -> str:
    value = str(text or "").strip()
    if not value or "*** Begin Patch" in value:
        return value
    if not _has_structured_patch_operation(value):
        return value
    notes.append("wrapped_structured_patch")
    if "*** End Patch" not in value:
        value = value.rstrip() + "\n*** End Patch"
    return "*** Begin Patch\n" + value


def _starts_with_structured_patch_operation(text: str) -> bool:
    for line in str(text or "").splitlines():
        if not line.strip():
            continue
        return bool(re.match(r"^\*\*\* (?:Add|Update|Delete) File:\s+\S+", line))
    return False


def _is_context_diff_range_header(line: str) -> bool:
    return bool(re.match(r"^(?:\*\*\*|---)\s+\d+(?:,\d+)?\s+(?:\*{4}|-{4})\s*$", str(line or "")))


def _context_diff_header_path(line: str) -> str:
    if _is_context_diff_range_header(line):
        return ""
    marker = "*** " if line.startswith("*** ") else "--- " if line.startswith("--- ") else ""
    if not marker:
        return ""
    rest = line[len(marker) :].strip()
    if not rest or rest.startswith(("*", "-")):
        return ""
    if rest in {"Begin Patch", "End Patch"}:
        return ""
    if re.match(r"^(?:Add|Update|Delete|Move)\s", rest):
        return ""
    rest = rest.split("\t", 1)[0].strip()
    if " " in rest:
        first, tail = rest.split(" ", 1)
        if re.match(r"^\d{4}-\d{2}-\d{2}", tail) or re.match(r"^\d{2}:\d{2}", tail):
            rest = first
    return rest


def _normalize_patch_marker_typos(text: str) -> tuple[str, list[str]]:
    notes: list[str] = []
    normalized_lines: list[str] = []
    for raw_line in str(text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = raw_line.rstrip("\n")
        stripped = line.lstrip()
        if not stripped:
            normalized_lines.append(line)
            continue

        fixed = line
        if stripped.startswith(("＊＊＊ ", "*** ", "**** ", "＊")):
            normalized = stripped.replace("＊＊＊", "***").replace("***", "***").replace("＊", "*")
            normalized = re.sub(r"^\*{3}\s*(Begin|End)\s*Patch\b", lambda m: f"*** {m.group(1).title()} Patch", normalized, flags=re.IGNORECASE)
            normalized = re.sub(r"^\*{3}\s*(Add|Update|Delete)\s*File\s*:\s*", lambda m: f"*** {m.group(1).title()} File: ", normalized, flags=re.IGNORECASE)
            normalized = re.sub(r"^\*{3}\s*Move\s*to\s*:\s*", "*** Move to: ", normalized, flags=re.IGNORECASE)
            normalized = re.sub(r"^\*{3}\s*(Add|Update|Delete)\s*:\s*", lambda m: f"*** {m.group(1).title()} File: ", normalized, flags=re.IGNORECASE)
            if normalized != stripped:
                fixed = normalized
                notes.append("normalized_structured_patch_marker")
            elif stripped != line:
                fixed = stripped
                notes.append("trimmed_patch_marker_indent")
        elif re.match(r"^\s*\*\*\*\s*(?:Add|Update|Delete|Move|Begin|End)\b", line, flags=re.IGNORECASE):
            fixed = stripped
            if fixed != line:
                notes.append("trimmed_patch_marker_indent")

        fixed = re.sub(r"^\*\*\*\s+(BEGIN|END)\s+PATCH\b", lambda m: f"*** {m.group(1).title()} Patch", fixed, flags=re.IGNORECASE)
        fixed = re.sub(r"^\*\*\*\s*(ADD|UPDATE|DELETE)\s+FILE\b\s*:?", lambda m: f"*** {m.group(1).title()} File:", fixed, flags=re.IGNORECASE)
        fixed = re.sub(r"^\*\*\*\s*MOVE\s+TO\b\s*:?", "*** Move to:", fixed, flags=re.IGNORECASE)
        normalized_lines.append(fixed)

    result = "\n".join(normalized_lines)
    if result != text:
        notes.append("normalized_patch_markers")
    return result, notes


def _extract_patch_from_model_text(raw_text: str) -> tuple[str, list[str]]:
    text = str(raw_text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    notes: list[str] = []
    if "\\n" in text and "\n" not in text:
        text = text.replace("\\n", "\n")
        notes.append("decoded_literal_newlines")
    text, marker_notes = _normalize_patch_marker_typos(text)
    notes.extend(marker_notes)

    fence_matches = list(
        re.finditer(
            r"```(?:diff|patch|udiff|text|txt)?[ \t]*\n(.*?)\n```",
            text,
            flags=re.IGNORECASE | re.DOTALL,
        )
    )
    if fence_matches:
        blocks = [match.group(1).strip("\n") for match in fence_matches]
        for block in blocks:
            if _looks_like_patch(block):
                notes.append("stripped_markdown_fence")
                block = _ensure_structured_patch_wrapper(block, notes)
                return block.strip() + "\n", notes
        if len(blocks) == 1:
            notes.append("stripped_markdown_fence")
            text = blocks[0].strip()

    begin_index = text.find("*** Begin Patch")
    if begin_index >= 0:
        end_index = text.find("*** End Patch", begin_index)
        if end_index >= 0:
            end_index += len("*** End Patch")
            notes.append("trimmed_structured_patch")
            return text[begin_index:end_index].strip() + "\n", notes

    if _starts_with_structured_patch_operation(text):
        text = _ensure_structured_patch_wrapper(text, notes)
    if text.startswith("*** Begin Patch"):
        return text.strip() + "\n", notes

    lines = text.splitlines()
    first_patch_line = None
    for index, line in enumerate(lines):
        if (
            line.startswith("diff --git ")
            or line.startswith("Index: ")
            or line.startswith("--- ")
            or line.startswith("*** ")
            or line.startswith("***************")
        ):
            first_patch_line = index
            break
    if first_patch_line is not None and first_patch_line > 0:
        text = "\n".join(lines[first_patch_line:])
        notes.append("trimmed_leading_prose")

    text = _ensure_structured_patch_wrapper(text, notes)
    return text.strip() + "\n", notes


def _looks_like_patch(text: str) -> bool:
    value = str(text or "")
    if "*** Begin Patch" in value and "*** End Patch" in value:
        return True
    if _has_structured_patch_operation(value):
        return True
    return bool(
        re.search(r"(?m)^diff --git ", value)
        or (re.search(r"(?m)^---\s+\S+", value) and re.search(r"(?m)^\+\+\+\s+\S+", value))
        or (re.search(r"(?m)^\*\*\*\s+\S+", value) and re.search(r"(?m)^---\s+\S+", value) and re.search(r"(?m)^\*{15}", value))
        or re.search(r"(?m)^@@", value)
    )


def _patch_target_path_from_args(args: dict[str, Any]) -> str:
    for key in ("file", "path", "target_file", "filename"):
        value = str(args.get(key) or "").strip()
        if value:
            return _normalize_patch_path_token(value)
    return ""


def _looks_like_filename_token(value: str) -> bool:
    text = str(value or "").strip().strip("`'\"")
    if not text or len(text) > 240:
        return False
    if text in {"/dev/null", ".", ".."}:
        return False
    path = Path(text)
    if path.is_absolute() or ".." in path.parts:
        return False
    suffix = path.suffix.lower()
    if suffix:
        return bool(re.match(r"^[A-Za-z0-9._@%+=:,/ -]+$", text))
    return "/" in text and bool(re.match(r"^[A-Za-z0-9._@%+=:,/ -]+$", text))


def _candidate_file_tokens(*texts: str) -> list[str]:
    candidates: list[str] = []
    seen: set[str] = set()
    patterns = (
        r"(?:target(?:_file)?|file|path|filename|目标文件|文件|路径)\s*[:=：]\s*[`'\"]?([^`'\"\s,，;；]+)",
        r"[`'\"]([^`'\"]+\.[A-Za-z0-9]{1,12})[`'\"]",
        r"(?<![\w./-])([A-Za-z0-9._@%+=:-]+(?:/[A-Za-z0-9._@%+=:-]+)*\.[A-Za-z0-9]{1,12})(?![\w./-])",
    )
    for text in texts:
        raw = str(text or "")
        if not raw:
            continue
        for pattern in patterns:
            for match in re.finditer(pattern, raw):
                candidate = _normalize_patch_path_token(str(match.group(1) or ""))
                if not _looks_like_filename_token(candidate):
                    continue
                if candidate not in seen:
                    seen.add(candidate)
                    candidates.append(candidate)
    return candidates


def _infer_patch_target_path(args: dict[str, Any], patch_text: str, cwd: Path, notes: list[str]) -> str:
    explicit = _patch_target_path_from_args(args)
    if explicit:
        return explicit
    brief = str(args.get("brief") or "")
    message = str(args.get("message") or "")
    for candidate in _candidate_file_tokens(brief, message, patch_text):
        notes.append("inferred_target_file")
        return candidate
    try:
        files = [
            path
            for path in cwd.iterdir()
            if path.is_file() and not path.name.startswith(".") and path.name not in {"__pycache__"}
        ]
    except OSError:
        files = []
    if len(files) == 1:
        notes.append("inferred_single_file_in_cwd")
        return files[0].name
    html_files = [path for path in files if path.suffix.lower() in {".html", ".htm"}]
    if len(html_files) == 1 and re.search(r"html|网页|页面|游戏|canvas|script|style", f"{brief}\n{patch_text}", flags=re.IGNORECASE):
        notes.append("inferred_single_html_file_in_cwd")
        return html_files[0].name
    return ""


def _fill_missing_structured_paths(patch_text: str, target_path: str, notes: list[str]) -> str:
    target = _normalize_patch_path_token(target_path)
    if not target:
        return patch_text
    lines: list[str] = []
    changed = False
    for raw_line in str(patch_text or "").splitlines():
        line = raw_line.rstrip("\n")
        if re.match(r"^\*\*\* (?:Add|Update|Delete) File\s*:?\s*$", line, flags=re.IGNORECASE):
            op_match = re.match(r"^\*\*\* (Add|Update|Delete) File", line, flags=re.IGNORECASE)
            op = (op_match.group(1).title() if op_match else "Update")
            line = f"*** {op} File: {target}"
            changed = True
        lines.append(line)
    if changed:
        notes.append("filled_missing_structured_file_path")
        return "\n".join(lines) + ("\n" if patch_text.endswith("\n") else "")
    return patch_text


def _repair_unified_headers_with_target(patch_text: str, target_path: str, notes: list[str]) -> str:
    target = _normalize_patch_path_token(target_path)
    if not target:
        return patch_text
    lines = str(patch_text or "").splitlines()
    changed = False
    repaired: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped in {"---", "--- a/", "--- /dev/null"} or re.match(r"^---\s*$", line):
            repaired.append(f"--- a/{target}")
            changed = True
            continue
        if stripped in {"+++", "+++ b/"} or re.match(r"^\+\+\+\s*$", line):
            repaired.append(f"+++ b/{target}")
            changed = True
            continue
        repaired.append(line)
    if changed:
        notes.append("filled_missing_unified_file_path")
        return "\n".join(repaired) + ("\n" if patch_text.endswith("\n") else "")
    return patch_text


def _unified_patch_to_structured_update(patch_text: str, target_path: str, notes: list[str]) -> str:
    target = _normalize_patch_path_token(target_path)
    if not target:
        paths = _patch_changed_paths(patch_text)
        if paths:
            target = _normalize_patch_path_token(paths[-1])
    if not target or "@@" not in str(patch_text or ""):
        return ""
    change_lines: list[str] = []
    in_hunk = False
    for raw_line in str(patch_text or "").replace("\r\n", "\n").replace("\r", "\n").splitlines():
        line = raw_line.rstrip("\n")
        if line.startswith("@@"):
            in_hunk = True
            change_lines.append(line)
            continue
        if not in_hunk:
            continue
        if line.startswith((" ", "+", "-")):
            if line.startswith(("+++", "---")):
                continue
            change_lines.append(line)
        elif line.startswith("\\ No newline"):
            continue
        elif line.startswith(("diff --git ", "Index: ", "*** ")):
            break
    if not change_lines:
        return ""
    notes.append("converted_unified_hunk_to_structured_update")
    return "*** Begin Patch\n*** Update File: " + target + "\n" + "\n".join(change_lines) + "\n*** End Patch\n"


def _wrap_bare_patch_for_target(patch_text: str, target_path: str, notes: list[str]) -> str:
    value = str(patch_text or "").strip()
    target = _normalize_patch_path_token(target_path)
    if not value or not target:
        return value
    if _patch_changed_paths(value) or value.startswith("*** Begin Patch"):
        return value
    if re.search(r"(?m)^@@", value) and re.search(r"(?m)^[+-]", value):
        if re.search(r"(?m)^@@\s+-\d", value):
            notes.append("added_unified_headers_from_target_file")
            return f"--- a/{target}\n+++ b/{target}\n{value}\n"
        notes.append("wrapped_bare_hunk_as_structured_update")
        return f"*** Begin Patch\n*** Update File: {target}\n{value}\n*** End Patch\n"
    if re.search(r"(?m)^[+-]", value):
        notes.append("wrapped_bare_hunk_as_structured_update")
        return f"*** Begin Patch\n*** Update File: {target}\n{value}\n*** End Patch\n"
    return value


def _new_file_fragment_to_structured_add(patch_text: str, target_path: str, notes: list[str]) -> str:
    value = str(patch_text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not value or "new file mode" not in value.lower():
        return value
    path = _normalize_patch_path_token(target_path)
    if not path:
        match = re.search(r"(?m)^\+\+\+\s+(?:b/)?(\S+)", value)
        if match:
            path = _normalize_patch_path_token(match.group(1))
    if not path:
        match = re.search(r"(?m)^diff --git\s+\S+\s+(?:b/)?(\S+)", value)
        if match:
            path = _normalize_patch_path_token(match.group(1))
    if not path or path == "/dev/null":
        return value
    body: list[str] = []
    for raw_line in value.splitlines():
        line = raw_line.rstrip("\n")
        if line.startswith("+++") or line.startswith("diff --git "):
            continue
        if line.startswith("+"):
            body.append(line[1:])
    if not body:
        return value
    notes.append("converted_new_file_fragment_to_structured_add")
    return "*** Begin Patch\n*** Add File: " + path + "\n" + "\n".join(f"+{line}" for line in body) + "\n*** End Patch\n"


def _raw_content_write_for_target(
    *,
    content: str,
    target_path: str,
    cwd: Path,
    brief: str,
    check_only: bool,
    notes: list[str],
) -> dict[str, Any] | None:
    value = str(content or "")
    target = _normalize_patch_path_token(target_path)
    if not value.strip() or not target:
        return None
    if _looks_like_patch(value) or _patch_changed_paths(value):
        return None
    valid, reason = _validate_patch_paths([target])
    if not valid:
        return {
            "status": "blocked",
            "tool": "apply_patch",
            "brief": brief,
            "cwd": str(cwd),
            "message": reason,
            "changed_files": [target],
        }
    target_file = cwd / target
    normalized_content = value.replace("\r\n", "\n").replace("\r", "\n")
    if not normalized_content.endswith("\n"):
        normalized_content += "\n"
    mode_used = "raw-content-create" if not target_file.exists() else "raw-content-replace"
    if check_only:
        return {
            "status": "ok",
            "tool": "apply_patch",
            "brief": brief,
            "cwd": str(cwd),
            "check_only": True,
            "changed_files": [target],
            "mode_used": mode_used,
            "normalization": [*notes, "raw_content_target_file"],
            "message": "raw content 写入校验通过，未写入文件。",
        }
    target_file.parent.mkdir(parents=True, exist_ok=True)
    target_file.write_text(normalized_content, encoding="utf-8")
    return _finalize_apply_patch_result(
        {
            "status": "ok",
            "tool": "apply_patch",
            "brief": brief,
            "cwd": str(cwd),
            "changed_files": [target],
            "patch": (
                f"*** Begin Patch\n"
                f"*** {'Add' if mode_used == 'raw-content-create' else 'Update'} File: {target}\n"
                "[raw full-file content normalized by apply_patch]\n"
                "*** End Patch\n"
            ),
            "mode_used": mode_used,
            "normalization": [*notes, "raw_content_target_file"],
            "message": "raw content 已通过 apply_patch 智能写入。",
        },
        cwd=cwd,
    )


def _deepseek_edit_target(spec: dict[str, Any], fallback: str = "") -> str:
    for key in ("target_file", "file", "path", "filename"):
        value = str(spec.get(key) or "").strip()
        if value:
            return _normalize_patch_path_token(value)
    return _normalize_patch_path_token(fallback)


def _deepseek_edit_text(spec: dict[str, Any], *keys: str) -> tuple[bool, str]:
    for key in keys:
        if key in spec and spec.get(key) is not None:
            value = spec.get(key)
            if isinstance(value, list) and all(isinstance(item, str) for item in value):
                return True, "\n".join(value)
            return True, str(value)
    return False, ""


def _deepseek_edit_bool(spec: dict[str, Any], key: str, default: bool) -> bool:
    value = spec.get(key)
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().lower() not in {"0", "false", "no", "off", "否"}
    return bool(value)


def _deepseek_normalize_operation(value: Any) -> str:
    op = str(value or "").strip().lower().replace("-", "_")
    aliases = {
        "create": "write",
        "replace_file": "write",
        "overwrite": "write",
        "remove": "delete",
        "delete_file": "delete",
        "insertafter": "insert_after",
        "insertbefore": "insert_before",
    }
    return aliases.get(op, op)


def _deepseek_occurrence(value: Any) -> str | int:
    if isinstance(value, int):
        return max(1, value)
    raw = str(value or "").strip().lower()
    if raw in {"", "first", "1", "one"}:
        return "first"
    if raw in {"last", "tail", "末次", "最后"}:
        return "last"
    if raw in {"all", "*", "every", "全部"}:
        return "all"
    try:
        return max(1, int(raw))
    except ValueError:
        return "first"


def _deepseek_find_span(text: str, needle: str, occurrence: str | int) -> tuple[int, int]:
    if not needle:
        return -1, -1
    if occurrence == "last":
        index = text.rfind(needle)
        return (index, index + len(needle)) if index >= 0 else (-1, -1)
    if isinstance(occurrence, int):
        cursor = -1
        for _ in range(max(1, occurrence)):
            cursor = text.find(needle, cursor + 1)
            if cursor < 0:
                return -1, -1
        return cursor, cursor + len(needle)
    index = text.find(needle)
    return (index, index + len(needle)) if index >= 0 else (-1, -1)


def _deepseek_replace_occurrence(
    text: str,
    needle: str,
    replacement: str,
    occurrence: str | int,
) -> tuple[str, int]:
    if not needle:
        return text, 0
    if occurrence == "all":
        count = text.count(needle)
        return text.replace(needle, replacement), count
    start, end = _deepseek_find_span(text, needle, occurrence)
    if start < 0:
        return text, 0
    return text[:start] + replacement + text[end:], 1


def _deepseek_normalize_content(value: str, *, ensure_trailing_newline: bool = False) -> str:
    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n")
    if ensure_trailing_newline and text and not text.endswith("\n"):
        text += "\n"
    return text


def _deepseek_structured_specs(args: dict[str, Any]) -> list[dict[str, Any]]:
    edits = args.get("edits")
    fallback_target = _deepseek_edit_target(args)
    if isinstance(edits, list) and edits:
        specs: list[dict[str, Any]] = []
        for item in edits:
            if not isinstance(item, dict):
                continue
            spec = dict(item)
            if fallback_target and not _deepseek_edit_target(spec):
                spec["target_file"] = fallback_target
            specs.append(spec)
        return specs
    operation = str(args.get("operation") or "").strip()
    if operation:
        return [dict(args)]
    return []


def _execute_deepseek_structured_apply_patch(
    args: dict[str, Any],
    *,
    cwd: Path,
    brief: str,
    check_only: bool,
) -> dict[str, Any] | None:
    specs = _deepseek_structured_specs(args)
    if not specs:
        return None

    changed_paths: list[str] = []
    for spec in specs:
        op = _deepseek_normalize_operation(spec.get("operation") or spec.get("op"))
        if op == "patch":
            return None
        target = _deepseek_edit_target(spec)
        if not op:
            return {
                "status": "error",
                "tool": "apply_patch",
                "brief": brief,
                "cwd": str(cwd),
                "message": "结构化 apply_patch 缺少 operation。",
            }
        if op not in DEEPSEEK_STRUCTURED_PATCH_OPS:
            return {
                "status": "error",
                "tool": "apply_patch",
                "brief": brief,
                "cwd": str(cwd),
                "message": f"未知结构化编辑 operation: {op}",
            }
        if not target:
            return {
                "status": "error",
                "tool": "apply_patch",
                "brief": brief,
                "cwd": str(cwd),
                "message": f"结构化编辑 {op} 缺少 target_file。",
            }
        if target not in changed_paths:
            changed_paths.append(target)

    valid, reason = _validate_patch_paths(changed_paths)
    if not valid:
        return {
            "status": "blocked",
            "tool": "apply_patch",
            "brief": brief,
            "cwd": str(cwd),
            "message": reason,
            "changed_files": changed_paths,
            "mode_used": "deepseek-structured",
        }

    total_text_size = 0
    for spec in specs:
        for key in ("content", "replace", "replacement", "new_text", "find", "old_text", "marker"):
            value = spec.get(key)
            if value is not None:
                total_text_size += len(str(value))
    if total_text_size > MAX_PATCH_CHARS:
        return {
            "status": "blocked",
            "tool": "apply_patch",
            "brief": brief,
            "cwd": str(cwd),
            "message": f"结构化编辑内容太大：{total_text_size} chars，超过 {MAX_PATCH_CHARS}。",
            "changed_files": changed_paths,
            "mode_used": "deepseek-structured",
        }

    staged: dict[str, str | None] = {}

    def read_current(rel_path: str, *, create_if_missing: bool) -> tuple[str | None, str]:
        if rel_path in staged:
            current = staged[rel_path]
            if current is None:
                if create_if_missing:
                    return "", ""
                return None, f"目标文件已在本次编辑中删除: {rel_path}"
            return current, ""
        target = cwd / rel_path
        if target.exists():
            if not target.is_file():
                return None, f"目标不是普通文件: {rel_path}"
            try:
                return target.read_text(encoding="utf-8", errors="replace"), ""
            except OSError as exc:
                return None, f"读取目标失败 {rel_path}: {exc}"
        if create_if_missing:
            return "", ""
        return None, f"目标文件不存在: {rel_path}"

    applied_ops: list[str] = []
    for index, spec in enumerate(specs, start=1):
        op = _deepseek_normalize_operation(spec.get("operation") or spec.get("op"))
        target = _deepseek_edit_target(spec)
        if op in {"write", "create", "replace_file"}:
            has_content, content = _deepseek_edit_text(spec, "content", "text")
            if not has_content:
                return {
                    "status": "error",
                    "tool": "apply_patch",
                    "brief": brief,
                    "cwd": str(cwd),
                    "changed_files": changed_paths,
                    "message": f"第 {index} 个 write 编辑缺少 content。",
                    "mode_used": "deepseek-structured-write",
                }
            ensure_newline = _deepseek_edit_bool(spec, "ensure_trailing_newline", True)
            staged[target] = _deepseek_normalize_content(content, ensure_trailing_newline=ensure_newline)
            applied_ops.append("write")
            continue

        if op in {"append", "prepend"}:
            has_content, content = _deepseek_edit_text(spec, "content", "text", "insert")
            if not has_content:
                return {
                    "status": "error",
                    "tool": "apply_patch",
                    "brief": brief,
                    "cwd": str(cwd),
                    "changed_files": changed_paths,
                    "message": f"第 {index} 个 {op} 编辑缺少 content。",
                    "mode_used": f"deepseek-structured-{op}",
                }
            current, error_message = read_current(
                target,
                create_if_missing=_deepseek_edit_bool(spec, "create_if_missing", True),
            )
            if error_message:
                return {
                    "status": "error",
                    "tool": "apply_patch",
                    "brief": brief,
                    "cwd": str(cwd),
                    "changed_files": changed_paths,
                    "message": error_message,
                    "mode_used": f"deepseek-structured-{op}",
                }
            insert_text = _deepseek_normalize_content(content, ensure_trailing_newline=True)
            base = str(current or "").replace("\r\n", "\n").replace("\r", "\n")
            if op == "append":
                if base and not base.endswith("\n") and insert_text:
                    base += "\n"
                staged[target] = base + insert_text
            else:
                staged[target] = insert_text + base
            applied_ops.append(op)
            continue

        if op == "replace":
            has_find, find_text = _deepseek_edit_text(spec, "find", "old_text", "old")
            has_replace, replace_text = _deepseek_edit_text(spec, "replace", "replacement", "new_text", "new", "content")
            if not has_find or not find_text:
                return {
                    "status": "error",
                    "tool": "apply_patch",
                    "brief": brief,
                    "cwd": str(cwd),
                    "changed_files": changed_paths,
                    "message": f"第 {index} 个 replace 编辑缺少 find。",
                    "mode_used": "deepseek-structured-replace",
                }
            if not has_replace:
                return {
                    "status": "error",
                    "tool": "apply_patch",
                    "brief": brief,
                    "cwd": str(cwd),
                    "changed_files": changed_paths,
                    "message": f"第 {index} 个 replace 编辑缺少 replace。",
                    "mode_used": "deepseek-structured-replace",
                }
            current, error_message = read_current(
                target,
                create_if_missing=_deepseek_edit_bool(spec, "create_if_missing", False),
            )
            if error_message:
                return {
                    "status": "error",
                    "tool": "apply_patch",
                    "brief": brief,
                    "cwd": str(cwd),
                    "changed_files": changed_paths,
                    "message": error_message,
                    "mode_used": "deepseek-structured-replace",
                }
            updated, count = _deepseek_replace_occurrence(
                str(current or ""),
                _deepseek_normalize_content(find_text),
                _deepseek_normalize_content(replace_text),
                _deepseek_occurrence(spec.get("occurrence")),
            )
            if count <= 0:
                return {
                    "status": "error",
                    "tool": "apply_patch",
                    "brief": brief,
                    "cwd": str(cwd),
                    "changed_files": changed_paths,
                    "message": f"第 {index} 个 replace 未找到 find 文本。",
                    "mode_used": "deepseek-structured-replace",
                    "recovery_hint": ["读取目标片段，复制精确 find 文本后重试；不要改用 shell 写文件。"],
                }
            staged[target] = updated
            applied_ops.append("replace")
            continue

        if op in {"insert_after", "insert_before"}:
            has_marker, marker = _deepseek_edit_text(spec, "marker", "find", "after", "before")
            has_content, content = _deepseek_edit_text(spec, "content", "insert", "text")
            if not has_marker or not marker:
                return {
                    "status": "error",
                    "tool": "apply_patch",
                    "brief": brief,
                    "cwd": str(cwd),
                    "changed_files": changed_paths,
                    "message": f"第 {index} 个 {op} 编辑缺少 marker/find。",
                    "mode_used": f"deepseek-structured-{op}",
                }
            if not has_content:
                return {
                    "status": "error",
                    "tool": "apply_patch",
                    "brief": brief,
                    "cwd": str(cwd),
                    "changed_files": changed_paths,
                    "message": f"第 {index} 个 {op} 编辑缺少 content。",
                    "mode_used": f"deepseek-structured-{op}",
                }
            current, error_message = read_current(
                target,
                create_if_missing=_deepseek_edit_bool(spec, "create_if_missing", False),
            )
            if error_message:
                return {
                    "status": "error",
                    "tool": "apply_patch",
                    "brief": brief,
                    "cwd": str(cwd),
                    "changed_files": changed_paths,
                    "message": error_message,
                    "mode_used": f"deepseek-structured-{op}",
                }
            marker_text = _deepseek_normalize_content(marker)
            start, end = _deepseek_find_span(str(current or ""), marker_text, _deepseek_occurrence(spec.get("occurrence")))
            if start < 0:
                return {
                    "status": "error",
                    "tool": "apply_patch",
                    "brief": brief,
                    "cwd": str(cwd),
                    "changed_files": changed_paths,
                    "message": f"第 {index} 个 {op} 未找到 marker/find 文本。",
                    "mode_used": f"deepseek-structured-{op}",
                    "recovery_hint": ["读取目标片段，复制精确 marker 后重试；不要改用 shell 写文件。"],
                }
            insert_text = _deepseek_normalize_content(content)
            pos = end if op == "insert_after" else start
            staged[target] = str(current or "")[:pos] + insert_text + str(current or "")[pos:]
            applied_ops.append(op)
            continue

        if op == "delete":
            has_find, find_text = _deepseek_edit_text(spec, "find", "old_text", "content")
            if not has_find:
                current, error_message = read_current(target, create_if_missing=False)
                if error_message:
                    return {
                        "status": "error",
                        "tool": "apply_patch",
                        "brief": brief,
                        "cwd": str(cwd),
                        "changed_files": changed_paths,
                        "message": error_message,
                        "mode_used": "deepseek-structured-delete",
                    }
                del current
                staged[target] = None
                applied_ops.append("delete_file")
                continue
            current, error_message = read_current(target, create_if_missing=False)
            if error_message:
                return {
                    "status": "error",
                    "tool": "apply_patch",
                    "brief": brief,
                    "cwd": str(cwd),
                    "changed_files": changed_paths,
                    "message": error_message,
                    "mode_used": "deepseek-structured-delete",
                }
            updated, count = _deepseek_replace_occurrence(
                str(current or ""),
                _deepseek_normalize_content(find_text),
                "",
                _deepseek_occurrence(spec.get("occurrence")),
            )
            if count <= 0:
                return {
                    "status": "error",
                    "tool": "apply_patch",
                    "brief": brief,
                    "cwd": str(cwd),
                    "changed_files": changed_paths,
                    "message": f"第 {index} 个 delete 未找到 find 文本。",
                    "mode_used": "deepseek-structured-delete",
                }
            staged[target] = updated
            applied_ops.append("delete_text")
            continue

        return {
            "status": "error",
            "tool": "apply_patch",
            "brief": brief,
            "cwd": str(cwd),
            "changed_files": changed_paths,
            "message": f"结构化编辑 operation 暂不支持: {op}",
            "mode_used": "deepseek-structured",
        }

    if check_only:
        return {
            "status": "ok",
            "tool": "apply_patch",
            "brief": brief,
            "cwd": str(cwd),
            "check_only": True,
            "changed_files": changed_paths,
            "operations_count": len(applied_ops),
            "mode_used": "deepseek-structured",
            "message": "DeepSeek 结构化编辑校验通过，未写入文件。",
        }

    for rel_path, content in staged.items():
        target = cwd / rel_path
        if content is None:
            try:
                target.unlink()
            except FileNotFoundError:
                pass
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

    return _finalize_apply_patch_result(
        {
            "status": "ok",
            "tool": "apply_patch",
            "brief": brief,
            "cwd": str(cwd),
            "changed_files": changed_paths,
            "operations_count": len(applied_ops),
            "mode_used": "deepseek-structured",
            "patch": "[DeepSeek structured edit: content omitted]",
            "message": "DeepSeek 结构化编辑已应用。",
        },
        cwd=cwd,
    )


def _strip_candidates(requested_strip: int | None) -> list[int]:
    ordered: list[int] = []
    if requested_strip is not None:
        ordered.append(max(0, min(5, int(requested_strip))))
    ordered.extend([1, 0, 2, 3, 4, 5])
    result: list[int] = []
    for value in ordered:
        if value not in result:
            result.append(value)
    return result


def _run_patch_dry_run_command(command: list[str], *, cwd: Path, input_text: str) -> subprocess.CompletedProcess[str]:
    return _run_input_command(command, cwd=cwd, input_text=input_text, timeout_seconds=30)


def _find_line_block(lines: list[str], block: list[str], *, start: int = 0) -> int:
    if not block:
        return -1
    max_start = len(lines) - len(block)
    for index in range(max(0, start), max_start + 1):
        if lines[index : index + len(block)] == block:
            return index
    return -1


def _normalize_structured_patch_line(line: str) -> str:
    return " ".join(str(line or "").strip().split())


def _find_line_block_fuzzy(lines: list[str], block: list[str], *, start: int = 0) -> int:
    if not block:
        return -1
    max_start = len(lines) - len(block)
    if max_start < 0:
        return -1

    target = "\n".join(_normalize_structured_patch_line(line) for line in block)
    if not target.strip():
        return -1

    best_index = -1
    best_ratio = 0.0
    for index in range(max(0, start), max_start + 1):
        candidate = "\n".join(
            _normalize_structured_patch_line(line) for line in lines[index : index + len(block)]
        )
        ratio = difflib.SequenceMatcher(None, target, candidate).ratio()
        if ratio > best_ratio:
            best_ratio = ratio
            best_index = index
            if ratio >= 0.98:
                break
    if best_ratio >= 0.82:
        return best_index
    return -1


def _apply_structured_update(
    lines: list[str],
    change_lines: list[str],
    *,
    allow_fuzzy: bool = True,
) -> tuple[list[str], str, bool]:
    output = list(lines)
    cursor = 0
    chunk: list[str] = []
    used_fuzzy = False

    def apply_chunk(active: list[str], current_cursor: int) -> tuple[int, str]:
        nonlocal used_fuzzy
        old_block: list[str] = []
        new_block: list[str] = []
        for raw_line in active:
            if raw_line.startswith("@@"):
                continue
            if not raw_line:
                old_block.append("")
                new_block.append("")
                continue
            prefix = raw_line[:1]
            body = raw_line[1:]
            if prefix == " ":
                old_block.append(body)
                new_block.append(body)
            elif prefix == "-":
                old_block.append(body)
            elif prefix == "+":
                new_block.append(body)
            elif raw_line == "*** End of File":
                continue
            else:
                old_block.append(raw_line)
                new_block.append(raw_line)
        if not old_block:
            return current_cursor, "structured patch hunk has no context/removal lines"
        match_index = _find_line_block(output, old_block, start=current_cursor)
        if match_index < 0:
            match_index = _find_line_block(output, old_block, start=0)
        if match_index < 0 and allow_fuzzy:
            match_index = _find_line_block_fuzzy(output, old_block, start=current_cursor)
            if match_index < 0:
                match_index = _find_line_block_fuzzy(output, old_block, start=0)
            if match_index >= 0:
                used_fuzzy = True
        if match_index < 0:
            preview = "\\n".join(old_block[:4])
            return current_cursor, f"structured patch context not found: {preview[:240]}"
        output[match_index : match_index + len(old_block)] = new_block
        return match_index + len(new_block), ""

    for line in change_lines:
        if line.startswith("@@"):
            if chunk:
                cursor, error_message = apply_chunk(chunk, cursor)
                if error_message:
                    return lines, error_message, used_fuzzy
                chunk = []
            continue
        if line.startswith("*** "):
            continue
        chunk.append(line)
    if chunk:
        cursor, error_message = apply_chunk(chunk, cursor)
        if error_message:
            return lines, error_message, used_fuzzy
    return output, "", used_fuzzy


def _execute_structured_patch(
    patch_text: str,
    *,
    cwd: Path,
    brief: str,
    check_only: bool,
    notes: list[str],
    allow_fuzzy: bool,
) -> dict[str, Any]:
    lines = patch_text.splitlines()
    if not lines or lines[0].strip() != "*** Begin Patch":
        return {
            "status": "error",
            "tool": "apply_patch",
            "brief": brief,
            "cwd": str(cwd),
            "message": "structured patch 缺少 *** Begin Patch。",
            "patch": patch_text,
        }

    operations: list[dict[str, Any]] = []
    index = 1
    while index < len(lines):
        line = lines[index]
        if line.strip() == "*** End Patch":
            break
        if line.startswith("*** Add File: "):
            path = _normalize_patch_path_token(line[len("*** Add File: ") :])
            index += 1
            body: list[str] = []
            while index < len(lines) and not lines[index].startswith("*** "):
                raw = lines[index]
                if not raw.startswith("+"):
                    if "add_file_missing_plus" not in notes:
                        notes.append("add_file_missing_plus")
                    body.append(raw)
                else:
                    body.append(raw[1:])
                index += 1
            operations.append({"op": "add", "path": path, "body": body})
            continue
        if line.startswith("*** Delete File: "):
            path = _normalize_patch_path_token(line[len("*** Delete File: ") :])
            operations.append({"op": "delete", "path": path})
            index += 1
            continue
        if line.startswith("*** Update File: "):
            path = _normalize_patch_path_token(line[len("*** Update File: ") :])
            move_to = ""
            index += 1
            if index < len(lines) and lines[index].startswith("*** Move to: "):
                move_to = _normalize_patch_path_token(lines[index][len("*** Move to: ") :])
                index += 1
            change_lines: list[str] = []
            while index < len(lines) and not lines[index].startswith("*** Update File: ") and not lines[index].startswith("*** Add File: ") and not lines[index].startswith("*** Delete File: ") and lines[index].strip() != "*** End Patch":
                change_lines.append(lines[index])
                index += 1
            operations.append({"op": "update", "path": path, "move_to": move_to, "changes": change_lines})
            continue
        return {
            "status": "error",
            "tool": "apply_patch",
            "brief": brief,
            "cwd": str(cwd),
            "message": f"未知 structured patch 操作: {line[:80]}",
            "patch": patch_text,
        }

    changed_paths = [str(op.get("move_to") or op.get("path") or "") for op in operations if str(op.get("move_to") or op.get("path") or "").strip()]
    valid, reason = _validate_patch_paths(changed_paths)
    if not valid:
        return {
            "status": "blocked",
            "tool": "apply_patch",
            "brief": brief,
            "cwd": str(cwd),
            "message": reason,
            "changed_files": changed_paths,
            "patch": patch_text,
        }
    if not operations:
        return {
            "status": "error",
            "tool": "apply_patch",
            "brief": brief,
            "cwd": str(cwd),
            "message": "structured patch 没有可执行操作。",
            "patch": patch_text,
        }

    staged_writes: list[tuple[Path, str | None]] = []
    fuzzy_used = False
    for op in operations:
        path = cwd / str(op.get("path") or "")
        op_name = str(op.get("op") or "")
        if op_name == "add":
            if path.exists():
                return {
                    "status": "error",
                    "tool": "apply_patch",
                    "brief": brief,
                    "cwd": str(cwd),
                    "message": f"Add File 目标已存在: {op.get('path')}",
                    "changed_files": changed_paths,
                    "patch": patch_text,
                }
            staged_writes.append((path, "\n".join(op.get("body") or []) + "\n"))
        elif op_name == "delete":
            if not path.is_file():
                return {
                    "status": "error",
                    "tool": "apply_patch",
                    "brief": brief,
                    "cwd": str(cwd),
                    "message": f"Delete File 目标不存在: {op.get('path')}",
                    "changed_files": changed_paths,
                    "patch": patch_text,
                }
            staged_writes.append((path, None))
        elif op_name == "update":
            if not path.is_file():
                return {
                    "status": "error",
                    "tool": "apply_patch",
                    "brief": brief,
                    "cwd": str(cwd),
                    "message": f"Update File 目标不存在: {op.get('path')}",
                    "changed_files": changed_paths,
                    "patch": patch_text,
                }
            original = path.read_text(encoding="utf-8", errors="replace")
            updated_lines, error_message, update_fuzzy_used = _apply_structured_update(
                original.splitlines(),
                list(op.get("changes") or []),
                allow_fuzzy=allow_fuzzy,
            )
            if error_message:
                return {
                    "status": "error",
                    "tool": "apply_patch",
                    "brief": brief,
                    "cwd": str(cwd),
                    "message": error_message,
                    "changed_files": changed_paths,
                    "patch": patch_text,
                }
            fuzzy_used = fuzzy_used or update_fuzzy_used
            target_path = cwd / str(op.get("move_to") or op.get("path") or "")
            staged_writes.append((path, None if target_path != path else "\n".join(updated_lines) + "\n"))
            if target_path != path:
                staged_writes.append((target_path, "\n".join(updated_lines) + "\n"))

    if fuzzy_used:
        notes = [*notes, "structured_fuzzy_match"]

    if check_only:
        return {
            "status": "ok",
            "tool": "apply_patch",
            "brief": brief,
            "cwd": str(cwd),
            "check_only": True,
            "changed_files": changed_paths,
            "patch": patch_text,
            "mode_used": "structured-fuzzy" if fuzzy_used else "structured",
            "normalization": notes,
            "message": "structured patch 校验通过，未写入文件。",
        }

    for target_path, content in staged_writes:
        if content is None:
            try:
                target_path.unlink()
            except FileNotFoundError:
                pass
            continue
        target_path.parent.mkdir(parents=True, exist_ok=True)
        target_path.write_text(content, encoding="utf-8")

    return {
        "status": "ok",
        "tool": "apply_patch",
        "brief": brief,
        "cwd": str(cwd),
        "changed_files": changed_paths,
        "patch": patch_text,
        "mode_used": "structured-fuzzy" if fuzzy_used else "structured",
        "normalization": notes,
        "message": "structured patch 已应用。",
    }


def _run_input_command(command: list[str], *, cwd: Path, input_text: str, timeout_seconds: int = 30) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=str(cwd),
        input=input_text,
        text=True,
        capture_output=True,
        timeout=timeout_seconds,
    )


def _node_check_text(script_text: str) -> str:
    node_bin = shutil.which("node")
    if not node_bin or not script_text.strip():
        return ""
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as handle:
            handle.write(script_text)
            temp_path = Path(handle.name)
        completed = subprocess.run(
            [node_bin, "--check", str(temp_path)],
            text=True,
            capture_output=True,
            timeout=15,
        )
    except Exception as exc:
        return f"node --check unavailable: {exc}"
    finally:
        if temp_path is not None:
            try:
                temp_path.unlink()
            except FileNotFoundError:
                pass
    if completed.returncode == 0:
        return ""
    lines = [line.strip() for line in (completed.stderr or completed.stdout or "").splitlines() if line.strip()]
    syntax_line = next((line for line in lines if "SyntaxError" in line), "")
    return syntax_line or (lines[0] if lines else f"node --check failed rc={completed.returncode}")


def _js_duplicate_declaration_warnings(script_text: str) -> list[str]:
    text = str(script_text or "")
    if not text.strip():
        return []
    counts: dict[str, int] = {}
    pattern = re.compile(r"^\s*(?:let|const|var|function|class)\s+([A-Za-z_$][\w$]*)\b")
    brace_depth = 0
    in_block_comment = False
    for raw_line in text.splitlines():
        line = str(raw_line)
        if in_block_comment:
            end = line.find("*/")
            if end < 0:
                continue
            line = line[end + 2 :]
            in_block_comment = False
        while "/*" in line:
            start = line.find("/*")
            end = line.find("*/", start + 2)
            if end < 0:
                line = line[:start]
                in_block_comment = True
                break
            line = line[:start] + line[end + 2 :]
        line = re.sub(r"(['\"`])(?:\\.|(?!\1).)*\1", "''", line)
        line = line.split("//", 1)[0]
        match = pattern.match(line)
        # Most project scripts are wrapped in an IIFE, so depth 1 is the
        # useful "module top level"; locals inside functions are deeper.
        if match and brace_depth <= 1:
            name = match.group(1)
            counts[name] = counts.get(name, 0) + 1
        brace_depth += line.count("{") - line.count("}")
        brace_depth = max(0, brace_depth)
    duplicates = [name for name, count in sorted(counts.items()) if count > 1]
    if not duplicates:
        return []
    joined = ", ".join(duplicates[:8])
    return [f"duplicate JS declarations detected: {joined}"]


def _js_structure_warnings(script_text: str) -> list[str]:
    text = str(script_text or "")
    if not text.strip():
        return []
    warnings: list[str] = []
    pattern = re.compile(r"^\s*(?:function|class)\s+([A-Za-z_$][\w$]*)\b")
    brace_depth = 0
    in_block_comment = False
    for lineno, raw_line in enumerate(text.splitlines(), start=1):
        line = str(raw_line)
        if in_block_comment:
            end = line.find("*/")
            if end < 0:
                continue
            line = line[end + 2 :]
            in_block_comment = False
        while "/*" in line:
            start = line.find("/*")
            end = line.find("*/", start + 2)
            if end < 0:
                line = line[:start]
                in_block_comment = True
                break
            line = line[:start] + line[end + 2 :]
        line = re.sub(r"(['\"`])(?:\\.|(?!\1).)*\1", "''", line)
        line = line.split("//", 1)[0]
        match = pattern.match(line)
        if match and brace_depth > 1:
            warnings.append(f"nested JS declaration near line {lineno}: {match.group(1)}")
            if len(warnings) >= 4:
                return warnings
        brace_depth += line.count("{") - line.count("}")
        brace_depth = max(0, brace_depth)
    return warnings


def _strip_css_comments_from_line(line: str, in_comment: bool) -> tuple[str, bool]:
    output: list[str] = []
    index = 0
    while index < len(line):
        if in_comment:
            end = line.find("*/", index)
            if end < 0:
                return "".join(output), True
            index = end + 2
            in_comment = False
            continue
        start = line.find("/*", index)
        if start < 0:
            output.append(line[index:])
            break
        output.append(line[index:start])
        index = start + 2
        in_comment = True
    return "".join(output), in_comment


def _css_structure_warnings(css_text: str) -> list[str]:
    text = str(css_text or "")
    if not text.strip():
        return []
    warnings: list[str] = []
    stack: list[str] = []
    in_comment = False
    unmatched_close = 0
    for lineno, raw_line in enumerate(text.splitlines(), start=1):
        line, in_comment = _strip_css_comments_from_line(raw_line, in_comment)
        if not line.strip():
            continue
        offset = 0
        while offset < len(line):
            open_at = line.find("{", offset)
            close_at = line.find("}", offset)
            if open_at < 0 and close_at < 0:
                break
            if close_at >= 0 and (open_at < 0 or close_at < open_at):
                if stack:
                    stack.pop()
                else:
                    unmatched_close += 1
                offset = close_at + 1
                continue
            header = line[offset:open_at].strip()
            kind = "at" if header.startswith("@") else "rule"
            if kind == "rule" and "rule" in stack:
                preview = header[:80] or "<anonymous>"
                warnings.append(f"nested CSS rule near line {lineno}: {preview}")
                if len(warnings) >= 4:
                    return warnings
            stack.append(kind)
            offset = open_at + 1
    if unmatched_close:
        warnings.append("CSS has unmatched closing brace")
    if stack:
        warnings.append("CSS braces may be unbalanced")
    return warnings[:4]


def _html_duplicate_id_warnings(html_text: str) -> list[str]:
    text = str(html_text or "")
    if not text.strip():
        return []
    counts: dict[str, int] = {}
    pattern = re.compile(r"""\bid\s*=\s*(['"])(.*?)\1""", flags=re.IGNORECASE | re.DOTALL)
    for match in pattern.finditer(text):
        value = " ".join(str(match.group(2) or "").strip().split())
        if not value:
            continue
        counts[value] = counts.get(value, 0) + 1
    duplicates = [value for value, count in sorted(counts.items()) if count > 1]
    if not duplicates:
        return []
    joined = ", ".join(duplicates[:8])
    return [f"duplicate HTML id attributes detected: {joined}"]


def _patch_integrity_warnings(cwd: Path, changed_paths: list[str]) -> list[str]:
    warnings: list[str] = []
    seen: set[str] = set()
    for raw_path in changed_paths[:12]:
        rel_path = str(raw_path or "").strip()
        if not rel_path or rel_path in seen:
            continue
        seen.add(rel_path)
        target = (cwd / rel_path).resolve()
        if not target.is_file():
            continue
        suffix = target.suffix.lower()
        if suffix not in {".html", ".htm", ".css", ".js", ".mjs", ".cjs"}:
            continue
        try:
            text = target.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            warnings.append(f"{rel_path}: unable to read for integrity check ({exc})")
            continue
        lower = text.lower()
        if suffix in {".html", ".htm"}:
            if lower.count("<script") > lower.count("</script>"):
                warnings.append(f"{rel_path}: HTML script tag may be unclosed")
            if "<body" in lower and "</body>" not in lower:
                warnings.append(f"{rel_path}: missing </body>")
            if "<html" in lower and "</html>" not in lower:
                warnings.append(f"{rel_path}: missing </html>")
            warnings.extend(f"{rel_path}: {warning}" for warning in _html_duplicate_id_warnings(text))
            inline_scripts = re.findall(r"<script\b[^>]*>(.*?)</script>", text, flags=re.IGNORECASE | re.DOTALL)
            script_text = "\n;\n".join(script.strip() for script in inline_scripts if script.strip())
            node_error = _node_check_text(script_text) if script_text else ""
            if node_error:
                warnings.append(f"{rel_path}: inline script syntax check failed: {node_error[:180]}")
            compact_script = re.sub(r"\s+", "", script_text)
            game_loop_match = re.search(r"function\s+gameLoop\s*\([^)]*\)\s*\{(.*?)\n?\}", script_text, flags=re.DOTALL)
            game_loop_body = re.sub(r"\s+", "", game_loop_match.group(1)) if game_loop_match else ""
            if "clearTimeout(loop)" in compact_script and "setTimeout(gameLoop" in game_loop_body and "loop=setTimeout(gameLoop" not in game_loop_body:
                warnings.append(f"{rel_path}: timer handle is cleared but recurring gameLoop timeout is not reassigned inside gameLoop")
            warnings.extend(f"{rel_path}: {warning}" for warning in _js_duplicate_declaration_warnings(script_text))
            warnings.extend(f"{rel_path}: {warning}" for warning in _js_structure_warnings(script_text))
            inline_styles = re.findall(r"<style\b[^>]*>(.*?)</style>", text, flags=re.IGNORECASE | re.DOTALL)
            style_text = "\n".join(style.strip() for style in inline_styles if style.strip())
            warnings.extend(f"{rel_path}: inline style {warning}" for warning in _css_structure_warnings(style_text))
        elif suffix == ".css":
            warnings.extend(f"{rel_path}: {warning}" for warning in _css_structure_warnings(text))
        else:
            node_error = _node_check_text(text)
            if node_error:
                warnings.append(f"{rel_path}: JS syntax check failed: {node_error[:180]}")
            warnings.extend(f"{rel_path}: {warning}" for warning in _js_duplicate_declaration_warnings(text))
            warnings.extend(f"{rel_path}: {warning}" for warning in _js_structure_warnings(text))
    return warnings[:8]


def _patch_recovery_hints(changed_paths: list[str]) -> list[str]:
    suffixes = {Path(str(path or "")).suffix.lower() for path in changed_paths}
    hints = [
        "先读取失败位置附近 20-40 行，再用更小的 apply_patch hunk 重试。",
        "不要退回 sed/python/touch/cat/heredoc 写文件；结构性修改继续使用 apply_patch。",
        "单行替换请在同一个 hunk 里写 -旧行 / +新行，不要在附近追加重复行。",
        "patch 成功但有告警时，立刻读取目标片段并运行语法或结构检查，不要直接进入最终回复。",
    ]
    if suffixes & {".html", ".htm", ".js", ".mjs", ".cjs"}:
        hints.append("HTML/JS 先检查尾部与目标函数边界，补齐括号/标签后再对脚本运行 node --check。")
        hints.append("JS 新函数应放在目标作用域，不要插进另一个函数体内部。")
    if suffixes & {".html", ".htm", ".css"}:
        hints.append("CSS 先确认当前 rule 已闭合，避免把新选择器插入未闭合的规则块里。")
    return hints


def _command_write_warnings(command: str, decision: CommandDecision) -> list[str]:
    text = str(command or "").strip()
    if not text:
        return []
    lowered = text.lower()
    warnings: list[str] = []
    if decision.action in {"confirm", "blocked"} and ("sed -i" in lowered or "sed --in-place" in lowered):
        warnings.append("检测到直接写文件回退：sed -i")
    if decision.action in {"confirm", "blocked"} and re.search(r"\bperl\b[^\n]*\s-i\b", lowered):
        warnings.append("检测到直接写文件回退：perl -i")
    if decision.action in {"confirm", "blocked"} and re.search(r"\bpython(?:3)?\b[^\n]*\s-c\b", lowered):
        if any(marker in lowered for marker in ("open(", "write_text(", "write_bytes(", "path.write_text", "path.write_bytes")):
            warnings.append("检测到直接写文件回退：python -c 写文件")
    shell_write = (
        any(marker in lowered for marker in (" tee ", "cat >", "cat>>", "printf >", "echo >", "echo>>"))
        or bool(re.search(r"\bcat\b[^\n;&|]*<<[^\n]*(?:>>?|1>)\s*\S+", lowered))
        or bool(re.search(r"\b(?:cat|printf|echo)\b[^\n;&|]*(?:>>?|1>)\s*\S+", lowered))
        or bool(re.search(r"(^|[|;&]\s*)tee\s+(-a\s+)?\S+", lowered))
    )
    if decision.action in {"confirm", "blocked"} and shell_write:
        warnings.append("检测到直接写文件回退：shell 重定向/tee/heredoc 写文件")
    if decision.action in {"confirm", "blocked"} and re.search(r"\btouch\s+\S+", lowered):
        warnings.append("检测到直接写文件回退：touch 创建文件")
    if warnings:
        warnings.append("结构性编辑应改用 apply_patch；只有极小且不可避免的原地修补才考虑 sed/python。")
    return warnings[:4]


def _finalize_apply_patch_result(result: dict[str, Any], *, cwd: Path) -> dict[str, Any]:
    status = str(result.get("status") or "")
    changed_raw = result.get("changed_files") or []
    changed_paths = [str(item) for item in changed_raw] if isinstance(changed_raw, list) else []
    normalization = result.get("normalization") or []
    if isinstance(normalization, list) and normalization and not result.get("repair_attempts"):
        result["repair_attempts"] = [str(item) for item in normalization if str(item).strip()]
    if status == "ok" and not bool(result.get("check_only")):
        warnings = _patch_integrity_warnings(cwd, changed_paths)
        if warnings:
            result["warnings"] = warnings
            result["integrity_status"] = "warn"
            if not result.get("recovery_hint"):
                result["recovery_hint"] = _patch_recovery_hints(changed_paths)
            message = str(result.get("message") or "").strip()
            result["message"] = (message + " " if message else "") + "完整性提示：" + "；".join(warnings[:3])
    elif status in {"error", "blocked", "timeout"}:
        result.setdefault("recovery_hint", _patch_recovery_hints(changed_paths))
    return result


def _execute_apply_patch_tool(args: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    brief = str(args.get("brief") or "应用补丁").strip() or "应用补丁"
    allow_fuzzy = bool(args.get("allow_fuzzy", True))
    check_only = bool(args.get("check_only", False))
    cwd = _resolve_tool_cwd(args, context)
    structured_result = _execute_deepseek_structured_apply_patch(
        args,
        cwd=cwd,
        brief=brief,
        check_only=check_only,
    )
    if structured_result is not None:
        return structured_result

    raw_patch_text, argument_notes = _patch_text_from_args(args)
    if not raw_patch_text.strip():
        return {
            "status": "error",
            "tool": "apply_patch",
            "brief": brief,
            "message": "patch 为空。优先使用 operation + target_file + content/find/replace；高级用法可在 patch 字段提供 diff/patch。",
            "accepted_patch_fields": list(PATCH_TEXT_ARG_KEYS),
        }
    if len(raw_patch_text) > MAX_PATCH_CHARS:
        return {
            "status": "blocked",
            "tool": "apply_patch",
            "brief": brief,
            "message": f"patch 太大：{len(raw_patch_text)} chars，超过 {MAX_PATCH_CHARS}。",
        }
    target_path = _infer_patch_target_path(args, raw_patch_text, cwd, argument_notes)
    raw_patch_text = _new_file_fragment_to_structured_add(raw_patch_text, target_path, argument_notes)
    patch_text, normalization_notes = _extract_patch_from_model_text(raw_patch_text)
    normalization_notes = [*argument_notes, *normalization_notes]
    if not target_path:
        target_path = _infer_patch_target_path(args, patch_text, cwd, normalization_notes)
    patch_text = _new_file_fragment_to_structured_add(patch_text, target_path, normalization_notes)
    patch_text = _fill_missing_structured_paths(patch_text, target_path, normalization_notes)
    patch_text = _repair_unified_headers_with_target(patch_text, target_path, normalization_notes)
    raw_write_result = _raw_content_write_for_target(
        content=patch_text,
        target_path=target_path,
        cwd=cwd,
        brief=brief,
        check_only=bool(args.get("check_only", False)),
        notes=normalization_notes,
    )
    if raw_write_result is not None:
        return raw_write_result
    if target_path:
        patch_text = _wrap_bare_patch_for_target(patch_text, target_path, normalization_notes)
    try:
        requested_strip = max(0, min(5, int(args.get("strip", 1)))) if args.get("strip") not in {None, ""} else None
    except (TypeError, ValueError):
        requested_strip = None
    if patch_text.startswith("*** Begin Patch"):
        return _finalize_apply_patch_result(_execute_structured_patch(
            patch_text,
            cwd=cwd,
            brief=brief,
            check_only=check_only,
            notes=normalization_notes,
            allow_fuzzy=allow_fuzzy,
        ), cwd=cwd)

    if not _looks_like_patch(patch_text):
        return {
            "status": "error",
            "tool": "apply_patch",
            "brief": brief,
            "cwd": str(cwd),
            "message": "没有识别到 unified diff / context diff / structured patch。请提供 diff --git、---/+++ 加 @@ hunk，或 *** Begin Patch / *** Update File 格式的补丁。",
            "patch": patch_text,
            "normalization": normalization_notes,
            "repair_attempts": normalization_notes,
            "accepted_patch_fields": list(PATCH_TEXT_ARG_KEYS),
        }

    changed_paths = _patch_changed_paths(patch_text)
    valid, reason = _validate_patch_paths(changed_paths)
    if not valid:
        return {
            "status": "blocked",
            "tool": "apply_patch",
            "brief": brief,
            "cwd": str(cwd),
            "message": reason,
            "changed_files": changed_paths,
            "patch": patch_text,
            "normalization": normalization_notes,
            "repair_attempts": normalization_notes,
        }
    if not changed_paths:
        return {
            "status": "error",
            "tool": "apply_patch",
            "brief": brief,
            "cwd": str(cwd),
            "message": "无法从 patch header 中识别目标文件路径。",
            "patch": patch_text,
            "normalization": normalization_notes,
            "repair_attempts": normalization_notes,
        }

    attempts: list[str] = []
    try:
        for strip in _strip_candidates(requested_strip):
            for extra_args, mode_label in (([], f"git apply -p{strip}"), (["--unidiff-zero"], f"git apply -p{strip} --unidiff-zero")):
                base_cmd = ["git", "apply", f"-p{strip}", "--whitespace=nowarn", *extra_args]
                check = _run_input_command(base_cmd + ["--check"], cwd=cwd, input_text=patch_text)
                if check.returncode != 0:
                    message = (check.stderr or check.stdout or "").strip().splitlines()
                    attempts.append(f"{mode_label}: {message[0][:180] if message else 'failed'}")
                    continue
                stat = _run_input_command(base_cmd + ["--stat"], cwd=cwd, input_text=patch_text)
                if check_only:
                    return {
                        "status": "ok",
                        "tool": "apply_patch",
                        "brief": brief,
                        "cwd": str(cwd),
                        "check_only": True,
                        "changed_files": changed_paths,
                        "patch": patch_text,
                        "stdout": (stat.stdout or "").strip(),
                        "mode_used": mode_label,
                        "normalization": normalization_notes,
                        "repair_attempts": normalization_notes,
                        "message": "patch 校验通过，未写入文件。",
                    }
                applied = _run_input_command(base_cmd, cwd=cwd, input_text=patch_text)
                stdout = (stat.stdout or applied.stdout or "").strip()
                stderr = (applied.stderr or "").strip()
                return _finalize_apply_patch_result({
                    "status": "ok" if applied.returncode == 0 else "error",
                    "tool": "apply_patch",
                    "brief": brief,
                    "cwd": str(cwd),
                    "returncode": applied.returncode,
                    "changed_files": changed_paths,
                    "patch": patch_text,
                    "stdout": stdout,
                    "stderr": stderr,
                    "mode_used": mode_label,
                    "normalization": normalization_notes,
                    "repair_attempts": normalization_notes,
                    "message": "patch 已应用。" if applied.returncode == 0 else "patch 应用失败。",
                }, cwd=cwd)
    except subprocess.TimeoutExpired:
        return {
            "status": "timeout",
            "tool": "apply_patch",
            "brief": brief,
            "cwd": str(cwd),
            "message": "git apply 超时。",
            "changed_files": changed_paths,
            "patch": patch_text,
            "normalization": normalization_notes,
            "repair_attempts": normalization_notes,
        }

    patch_bin = shutil.which("patch") if allow_fuzzy else None
    if patch_bin:
        try:
            for strip in _strip_candidates(requested_strip):
                mode_label = f"patch -p{strip} --fuzz=3"
                base_cmd = [
                    patch_bin,
                    f"-p{strip}",
                    "--batch",
                    "--forward",
                    "--no-backup-if-mismatch",
                    "--fuzz=3",
                ]
                dry_run = _run_patch_dry_run_command(base_cmd + ["--dry-run"], cwd=cwd, input_text=patch_text)
                if dry_run.returncode != 0:
                    message = (dry_run.stderr or dry_run.stdout or "").strip().splitlines()
                    attempts.append(f"{mode_label}: {message[0][:180] if message else 'failed'}")
                    continue
                if check_only:
                    stdout, stdout_truncated = _middle_truncate_text(dry_run.stdout or "", MAX_MODEL_STDOUT_CHARS)
                    stderr, stderr_truncated = _middle_truncate_text(dry_run.stderr or "", MAX_MODEL_STDERR_CHARS)
                    return {
                        "status": "ok",
                        "tool": "apply_patch",
                        "brief": brief,
                        "cwd": str(cwd),
                        "check_only": True,
                        "changed_files": changed_paths,
                        "patch": patch_text,
                        "stdout": stdout,
                        "stderr": stderr,
                        "stdout_truncated": stdout_truncated,
                        "stderr_truncated": stderr_truncated,
                        "mode_used": mode_label,
                        "normalization": normalization_notes,
                        "repair_attempts": normalization_notes,
                        "message": "patch fuzzy 校验通过，未写入文件。",
                    }
                applied = _run_input_command(base_cmd, cwd=cwd, input_text=patch_text)
                stdout, stdout_truncated = _middle_truncate_text(applied.stdout or "", MAX_MODEL_STDOUT_CHARS)
                stderr, stderr_truncated = _middle_truncate_text(applied.stderr or "", MAX_MODEL_STDERR_CHARS)
                return _finalize_apply_patch_result({
                    "status": "ok" if applied.returncode == 0 else "error",
                    "tool": "apply_patch",
                    "brief": brief,
                    "cwd": str(cwd),
                    "returncode": applied.returncode,
                    "changed_files": changed_paths,
                    "patch": patch_text,
                    "stdout": stdout,
                    "stderr": stderr,
                    "stdout_truncated": stdout_truncated,
                    "stderr_truncated": stderr_truncated,
                    "mode_used": mode_label,
                    "normalization": normalization_notes,
                    "repair_attempts": normalization_notes,
                    "message": "patch fuzzy 已应用。" if applied.returncode == 0 else "patch fuzzy 应用失败。",
                }, cwd=cwd)
        except subprocess.TimeoutExpired:
            return {
                "status": "timeout",
                "tool": "apply_patch",
                "brief": brief,
                "cwd": str(cwd),
                "changed_files": changed_paths,
                "patch": patch_text,
                "normalization": normalization_notes,
                "repair_attempts": normalization_notes,
                "message": "patch fuzzy 执行超时，需检查工作区状态。",
            }

    structured_fallback = _unified_patch_to_structured_update(patch_text, target_path or (changed_paths[-1] if changed_paths else ""), normalization_notes)
    if structured_fallback:
        fallback_result = _execute_structured_patch(
            structured_fallback,
            cwd=cwd,
            brief=brief,
            check_only=check_only,
            notes=normalization_notes,
            allow_fuzzy=allow_fuzzy,
        )
        if str(fallback_result.get("status") or "") == "ok":
            return _finalize_apply_patch_result(fallback_result, cwd=cwd)
        attempts.append(f"structured fallback: {str(fallback_result.get('message') or 'failed')[:180]}")

    stdout, stdout_truncated = _middle_truncate_text("\n".join(attempts[-12:]), MAX_MODEL_STDOUT_CHARS)
    failure_message = "patch 校验失败，已尝试 git apply 多种 -p/--unidiff-zero"
    if allow_fuzzy:
        failure_message += " 以及 GNU patch fuzz"
    failure_message += "，未写入文件。"
    return _finalize_apply_patch_result({
        "status": "error",
        "tool": "apply_patch",
        "brief": brief,
        "cwd": str(cwd),
        "changed_files": changed_paths,
        "patch": patch_text,
        "stdout": stdout,
        "stdout_truncated": stdout_truncated,
        "normalization": normalization_notes,
        "repair_attempts": [*normalization_notes, *attempts[-12:]],
        "message": failure_message,
    }, cwd=cwd)


def _strip_html_tags(value: str) -> str:
    value = re.sub(r"<[^>]+>", " ", value)
    value = html_lib.unescape(value)
    return re.sub(r"\s+", " ", value).strip()


def _decode_duckduckgo_url(url: str) -> str:
    decoded = html_lib.unescape(url)
    parsed = urllib.parse.urlparse(decoded)
    query = urllib.parse.parse_qs(parsed.query)
    uddg = query.get("uddg")
    if uddg:
        return uddg[0]
    return decoded


def _websearch_mode_from_args(args: dict[str, Any]) -> str:
    raw_mode = str(args.get("mode") or "").strip().lower()
    if raw_mode in {"summary", "web", "auto"}:
        return raw_mode
    if "summary" in args:
        summary_value = args.get("summary")
        if isinstance(summary_value, str):
            summary_value = summary_value.strip().lower() not in {"0", "false", "no", "off"}
        return "summary" if bool(summary_value) else "web"
    return "auto"


def _websearch_int_arg(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _websearch_bool_arg(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().lower() not in {"0", "false", "no", "off", ""}
    return bool(value)


def _websearch_trim_result_text(value: str, limit: int) -> str:
    text = " ".join(str(value or "").split())
    compacted, _ = _truncate_text(text, limit)
    return compacted


def _websearch_config(context: ToolContext) -> dict[str, str]:
    config = context.config
    return {
        "summary_key": str(
            getattr(config, "websearch_summary_key", None)
            or os.environ.get("VOLC_WEBSEARCH_SUMMARY_KEY", "")
            or os.environ.get("PROJECTLING_WEBSEARCH_SUMMARY_KEY", "")
            or ""
        ).strip(),
        "web_key": str(
            getattr(config, "websearch_web_key", None)
            or os.environ.get("VOLC_WEBSEARCH_WEB_KEY", "")
            or os.environ.get("PROJECTLING_WEBSEARCH_WEB_KEY", "")
            or ""
        ).strip(),
        "endpoint": str(
            getattr(config, "websearch_endpoint", None)
            or os.environ.get("VOLC_WEBSEARCH_ENDPOINT", "")
            or DEFAULT_WEBSEARCH_ENDPOINT
        ).strip()
        or DEFAULT_WEBSEARCH_ENDPOINT,
    }


def _websearch_post_json(api_key: str, endpoint: str, payload: dict[str, Any]) -> str:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        endpoint,
        data=data,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=45) as response:
            return response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code}: {body}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Network error: {exc}") from exc


def _websearch_build_payload(
    query: str,
    *,
    summary: bool,
    count: int,
    need_content: bool,
    sites: str,
    block_hosts: str,
    query_rewrite: bool,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "Query": query,
        "SearchType": "web_summary" if summary else "web",
        "Count": max(1, min(count, MAX_WEB_SEARCH_RESULTS)),
        "Filter": {
            "NeedContent": bool(need_content),
            "NeedUrl": True,
            "Sites": sites,
            "BlockHosts": block_hosts,
            "AuthInfoLevel": 0,
        },
        "NeedSummary": bool(summary),
        "TimeRange": "",
        "QueryControl": {
            "QueryRewrite": bool(query_rewrite),
        },
    }
    return payload


def _websearch_parse_response(raw: str) -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    summary_parts: list[str] = []
    usage: Any = None
    api_error: dict[str, Any] | None = None

    def handle_object(obj: dict[str, Any]) -> None:
        nonlocal usage, api_error
        error = obj.get("ResponseMetadata", {}).get("Error")
        if error:
            api_error = error
        result = obj.get("Result") or {}
        for item in result.get("WebResults") or []:
            title = _websearch_trim_result_text(item.get("Title") or "", 160)
            site = _websearch_trim_result_text(item.get("SiteName") or "", 80)
            url = _websearch_trim_result_text(item.get("Url") or "", 400)
            snippet = _websearch_trim_result_text(item.get("Summary") or item.get("Snippet") or "", MAX_WEB_SEARCH_SNIPPET_CHARS)
            summary_text = _websearch_trim_result_text(item.get("Summary") or "", MAX_WEB_SEARCH_SNIPPET_CHARS)
            content = _websearch_trim_result_text(item.get("Content") or "", MAX_WEB_SEARCH_SNIPPET_CHARS)
            results.append(
                {
                    "title": title,
                    "source": site,
                    "url": url,
                    "snippet": snippet,
                    "summary": summary_text,
                    "content": content,
                    "publish_time": _websearch_trim_result_text(item.get("PublishTime") or "", 80),
                    "auth": _websearch_trim_result_text(item.get("AuthInfoDes") or "", 32),
                    "auth_level": item.get("AuthInfoLevel"),
                }
            )
        for choice in result.get("Choices") or []:
            delta = choice.get("Delta") or {}
            content = delta.get("Content") or ""
            if content:
                summary_parts.append(str(content))
        if result.get("Usage") is not None:
            usage = result.get("Usage")

    text = raw.strip()
    if text.startswith("data:") or "\ndata:" in text:
        for line in text.splitlines():
            line = line.strip()
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if not data or data == "[DONE]":
                continue
            handle_object(json.loads(data))
    else:
        handle_object(json.loads(text))

    summary_text = _websearch_trim_result_text("".join(summary_parts), MAX_WEB_SEARCH_SUMMARY_CHARS)
    if summary_text:
        summary_text = summary_text.strip()
    return {
        "results": results,
        "summary": summary_text,
        "usage": usage,
        "error": api_error,
    }


def _websearch_format_stdout(*, query: str, mode_used: str, summary: str, results: list[dict[str, Any]]) -> str:
    lines: list[str] = [f"mode={mode_used} query={query}"]
    if summary:
        lines.append("")
        lines.append(f"summary: {summary}")
    if results:
        lines.append("")
        for index, item in enumerate(results, start=1):
            title = item.get("title") or ""
            source = item.get("source") or ""
            url = item.get("url") or ""
            snippet = item.get("snippet") or ""
            prefix = f"{index}. {title}"
            if source:
                prefix = f"{prefix} [{source}]"
            lines.append(prefix)
            if url:
                lines.append(f"   {url}")
            if snippet:
                lines.append(f"   {snippet}")
    stdout = "\n".join(lines).strip()
    compacted, _ = _truncate_text(stdout, MAX_WEB_SEARCH_STDOUT_CHARS)
    return compacted


def _execute_web_search_tool(args: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    query = str(args.get("query") or "").strip()
    if not query:
        return {
            "status": "error",
            "tool": "web_search",
            "brief": str(args.get("brief") or "").strip(),
            "message": "query 为空。",
        }
    mode = _websearch_mode_from_args(args)
    count = _websearch_int_arg(args.get("count") or args.get("max_results"), 5)
    need_content = _websearch_bool_arg(args.get("need_content"), False)
    query_rewrite = _websearch_bool_arg(args.get("query_rewrite"), False)
    sites = str(args.get("sites") or "").strip()
    block_hosts = str(args.get("block_hosts") or "").strip()
    cfg = _websearch_config(context)
    summary_key = cfg["summary_key"]
    web_key = cfg["web_key"]
    endpoint = cfg["endpoint"]

    if mode == "summary":
        use_summary = True
        api_key = summary_key
    elif mode == "web":
        use_summary = False
        api_key = web_key
    else:
        use_summary = bool(summary_key)
        api_key = summary_key if use_summary else web_key

    if not api_key:
        missing_name = "API Key" if not summary_key and not web_key else ("Summary Key" if use_summary else "Web Key")
        return {
            "status": "error",
            "tool": "web_search",
            "brief": str(args.get("brief") or "").strip(),
            "query": query,
            "mode": mode,
            "message": f"未配置 WebSearch {missing_name}。请先在 DeepSeek 设置里写入 Summary Key 和 Web Key。",
        }

    payload = _websearch_build_payload(
        query,
        summary=use_summary,
        count=count,
        need_content=need_content,
        sites=sites,
        block_hosts=block_hosts,
        query_rewrite=query_rewrite,
    )

    try:
        parsed = _websearch_parse_response(_websearch_post_json(api_key, endpoint, payload))
    except Exception as exc:
        return {
            "status": "error",
            "tool": "web_search",
            "brief": str(args.get("brief") or "").strip(),
            "query": query,
            "mode": mode,
            "message": f"搜索请求失败：{exc}",
        }

    error = parsed.get("error") or {}
    fallback_used = ""
    if use_summary and str(error.get("Code") or "") == "10409" and web_key:
        try:
            fallback_payload = _websearch_build_payload(
                query,
                summary=False,
                count=count,
                need_content=need_content,
                sites=sites,
                block_hosts=block_hosts,
                query_rewrite=query_rewrite,
            )
            parsed = _websearch_parse_response(_websearch_post_json(web_key, endpoint, fallback_payload))
            fallback_used = "web"
            error = parsed.get("error") or {}
            use_summary = False
        except Exception as exc:
            return {
                "status": "error",
                "tool": "web_search",
                "brief": str(args.get("brief") or "").strip(),
                "query": query,
                "mode": mode,
                "fallback": "web",
                "message": f"summary key 不支持 web_summary，web 回退也失败：{exc}",
            }

    results = parsed.get("results") or []
    summary_text = str(parsed.get("summary") or "").strip()
    stdout = _websearch_format_stdout(
        query=query,
        mode_used="summary" if use_summary else "web",
        summary=summary_text,
        results=results,
    )
    status = "ok" if results or summary_text else "empty"
    payload: dict[str, Any] = {
        "status": status,
        "tool": "web_search",
        "brief": str(args.get("brief") or "").strip(),
        "query": query,
        "mode": mode,
        "mode_used": "summary" if use_summary else "web",
        "endpoint": endpoint,
        "result_count": len(results),
        "summary": summary_text,
        "results": results,
        "stdout": stdout or "没有解析到搜索结果。",
    }
    if fallback_used:
        payload["fallback"] = fallback_used
    if parsed.get("usage") is not None:
        payload["usage"] = parsed.get("usage")
    if error:
        payload["api_error"] = error
    return payload


def _emit_tool_event(context: ToolContext | None, kind: str, payload: dict[str, Any]) -> None:
    callback = None if context is None else context.event_callback
    if callback is None:
        return
    callback(kind, payload)


def _shell_program() -> str:
    candidates = [
        os.environ.get("SHELL", "").strip(),
        "/data/data/com.termux/files/usr/bin/bash",
        "/bin/bash",
        "bash",
        "sh",
    ]
    for candidate in candidates:
        if not candidate:
            continue
        if os.path.isabs(candidate) and os.path.exists(candidate):
            return candidate
        if not os.path.isabs(candidate):
            return candidate
    return "sh"


def _parse_tokens(command: str) -> list[str]:
    return shlex.split(command, posix=True)


def _scan_shell_structure(command: str) -> ShellStructureFlags:
    in_single = False
    in_double = False
    escaped = False
    index = 0
    length = len(command)
    has_composite = False
    has_pipe = False
    has_redirection = False
    has_command_substitution = False

    while index < length:
        char = command[index]

        if escaped:
            escaped = False
            index += 1
            continue

        if in_single:
            if char == "'":
                in_single = False
            index += 1
            continue

        if char == "\\":
            escaped = True
            index += 1
            continue

        if in_double:
            if char == '"':
                in_double = False
                index += 1
                continue
            if char == "`":
                has_command_substitution = True
            if char == "$" and index + 1 < length and command[index + 1] == "(":
                has_command_substitution = True
            index += 1
            continue

        if char == "'":
            in_single = True
            index += 1
            continue

        if char == '"':
            in_double = True
            index += 1
            continue

        if char == "\n":
            has_composite = True
            index += 1
            continue

        if char == "`":
            has_command_substitution = True
            index += 1
            continue

        if char == "$" and index + 1 < length and command[index + 1] == "(":
            has_command_substitution = True
            index += 1
            continue

        if char == ";":
            has_composite = True
            index += 1
            continue

        if char == "&":
            has_composite = True
            if index + 1 < length and command[index + 1] == "&":
                index += 2
                continue
            index += 1
            continue

        if char == "|":
            has_composite = True
            has_pipe = True
            if index + 1 < length and command[index + 1] == "|":
                index += 2
                continue
            index += 1
            continue

        if char in {"<", ">"}:
            has_redirection = True
            if index + 1 < length and command[index + 1] == char:
                index += 2
                continue
            index += 1
            continue

        if char in {"(", ")"}:
            has_composite = True
            index += 1
            continue

        index += 1

    return ShellStructureFlags(
        has_composite=has_composite,
        has_pipe=has_pipe,
        has_redirection=has_redirection,
        has_command_substitution=has_command_substitution,
    )


def _is_private_tcp_target(raw_target: str) -> bool:
    target = raw_target.strip()
    if not target:
        return False
    host = target
    if target.startswith("[") and "]" in target:
        host = target[1 : target.index("]")]
    elif target.count(":") == 1:
        left, right = target.rsplit(":", 1)
        if right.isdigit():
            host = left
    if host in {"localhost", "127.0.0.1", "::1"}:
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return host.endswith(".local")
    return bool(
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_reserved
    )


def _risk_level(first_word: str, reason: str) -> str:
    if first_word in HIGH_RISK_COMMANDS:
        return "high"
    if "高危" in reason or "破坏" in reason:
        return "high"
    if "确认" in reason or "改动" in reason or "敏感" in reason:
        return "medium"
    return "low"


def _command_channel(command: str) -> str:
    try:
        tokens = _parse_tokens(command.strip())
    except ValueError:
        return "Bash"
    if not tokens:
        return "Bash"
    first = tokens[0]
    if first == "adb":
        return "ADB"
    if first.startswith("termux-"):
        return "Termux API"
    return "Bash"


def _adb_subcommand_tokens(tokens: list[str]) -> tuple[str | None, list[str]]:
    index = 1
    while index < len(tokens):
        token = tokens[index]
        if token in ADB_GLOBAL_OPTIONS_WITH_VALUE:
            index += 2
            continue
        if token.startswith("-"):
            index += 1
            continue
        return token, tokens[index + 1 :]
    return None, []


def _analyze_android_remote(tokens: list[str]) -> CommandDecision:
    if not tokens:
        return CommandDecision("blocked", "medium", "adb shell 缺少远端命令，交互式会话不允许。")

    first = tokens[0]
    second = tokens[1] if len(tokens) > 1 else ""
    low_risk = {
        "cat",
        "dumpsys",
        "getprop",
        "grep",
        "head",
        "id",
        "ls",
        "pm",
        "ps",
        "pwd",
        "sed",
        "service",
        "settings",
        "tail",
        "whoami",
    }

    if first == "pm" and second not in {"list", "path"}:
        return CommandDecision("confirm", "medium", "adb shell pm 可能修改包状态，需确认执行。")
    if first == "settings" and second != "get":
        return CommandDecision("confirm", "medium", "adb shell settings 非只读操作，需确认执行。")
    if first in {"am", "cmd", "input", "setprop", "svc"}:
        return CommandDecision("confirm", "medium", "adb shell 将改动设备状态，需确认执行。")
    if first in {"reboot", "stop", "start"}:
        return CommandDecision("confirm", "high", "adb shell 包含高风险设备操作，需确认执行。")
    if first in low_risk:
        return CommandDecision("execute", "low", "ADB 远端只读命令。")
    return CommandDecision("confirm", "medium", "ADB 远端命令不在只读白名单内，需确认执行。")


def _analyze_adb_command(tokens: list[str]) -> CommandDecision:
    subcommand, remaining = _adb_subcommand_tokens(tokens)
    if subcommand is None:
        return CommandDecision("execute", "low", "ADB 帮助或默认输出。")

    safe_subcommands = {"devices", "disconnect", "get-state", "help", "reconnect", "version"}
    if subcommand in safe_subcommands:
        return CommandDecision("execute", "low", "ADB 只读或本地安全命令。")

    if subcommand == "connect":
        if not remaining:
            return CommandDecision("blocked", "medium", "adb connect 缺少 TCP 目标。")
        target = remaining[0]
        if _is_private_tcp_target(target):
            return CommandDecision("execute", "low", "ADB TCP 目标位于本地或私网。")
        return CommandDecision("confirm", "medium", "ADB TCP 目标不在私网范围内，需确认执行。")

    if subcommand == "shell":
        return _analyze_android_remote(remaining)

    if subcommand in {"forward", "install", "pair", "pull", "push", "reverse", "sync", "tcpip", "uninstall", "usb"}:
        return CommandDecision("confirm", "medium", f"adb {subcommand} 将改动连接或设备状态，需确认执行。")

    if subcommand in {"reboot", "root", "unroot", "disable-verity", "enable-verity"}:
        return CommandDecision("confirm", "high", f"adb {subcommand} 属于高风险设备操作，需确认执行。")

    return CommandDecision("confirm", "medium", "ADB 子命令未归类为只读，需确认执行。")


def _analyze_termux_command(first_word: str) -> CommandDecision:
    if first_word in TERMUX_SAFE_COMMANDS:
        return CommandDecision("execute", "low", "Termux API 只读查询。")
    if first_word in TERMUX_CONFIRM_COMMANDS:
        return CommandDecision("confirm", "medium", "Termux API 涉及设备能力或隐私，需确认执行。")
    return CommandDecision("confirm", "medium", "Termux API 命令未归类，需确认执行。")


def _analyze_git_command(tokens: list[str]) -> CommandDecision:
    subcommand = tokens[1] if len(tokens) > 1 else ""
    if subcommand in GIT_READONLY_SUBCOMMANDS:
        return CommandDecision("execute", "low", "Git 只读命令。")
    if subcommand in GIT_MUTATING_SUBCOMMANDS:
        level = "high" if subcommand in {"clean", "reset"} else "medium"
        return CommandDecision("confirm", level, f"git {subcommand} 会改动仓库状态，需确认执行。")
    return CommandDecision("confirm", "medium", "Git 子命令未归类为只读，需确认执行。")


def _analyze_package_manager(tokens: list[str]) -> CommandDecision:
    first = tokens[0]
    if len(tokens) == 1:
        return CommandDecision("execute", "low", f"{first} 仅显示帮助或状态。")

    second = tokens[1]
    readonly = {"help", "list", "search", "show", "version", "why"}
    if first == "pip3":
        first = "pip"
    if first == "uv" and second in {"pip", "tool"} and len(tokens) >= 3:
        second = tokens[2]
    if second in readonly:
        return CommandDecision("execute", "low", "包管理器只读查询。")
    return CommandDecision("confirm", "medium", "包管理器命令会改动环境，需确认执行。")


PYTHON_SAFE_MODULES = {"json.tool", "pydoc", "site"}
PYTHON_INLINE_RISK_MARKERS = (
    "adb",
    "aiohttp",
    "check_call(",
    "check_output(",
    "chmod(",
    "chown(",
    "copy(",
    "copy2(",
    "curl",
    "eval(",
    "exec(",
    "httpx",
    "mkdir(",
    "makedirs(",
    "move(",
    "os.remove",
    "os.rmdir",
    "os.system",
    "path.write_",
    "popen(",
    "rename(",
    "replace(",
    "requests.",
    "rmdir(",
    "rmtree(",
    "run(",
    "socket",
    "subprocess",
    "symlink(",
    "termux",
    "touch(",
    "unlink(",
    "urllib.request",
    "wget",
    "write(",
    "write_bytes(",
    "write_text(",
)


def _python_inline_is_safe(code: str) -> bool:
    lowered = code.strip().lower()
    if not lowered:
        return False
    return not any(marker in lowered for marker in PYTHON_INLINE_RISK_MARKERS)


def _analyze_python_command(tokens: list[str]) -> CommandDecision:
    first = tokens[0]
    if len(tokens) == 1:
        return CommandDecision("blocked", "medium", f"{first} 将进入交互式会话，不允许。")

    for index, token in enumerate(tokens[1:], start=1):
        if token in {"-c", "-m"}:
            value = tokens[index + 1] if index + 1 < len(tokens) else ""
            if token == "-c":
                if _python_inline_is_safe(value):
                    return CommandDecision("execute", "low", f"{first} 单次执行命令。")
                return CommandDecision("confirm", "medium", f"{first} 内联代码可能改动系统，需确认执行。")
            if value in PYTHON_SAFE_MODULES:
                return CommandDecision("execute", "low", f"{first} 只读模块执行。")
            return CommandDecision("confirm", "medium", f"{first} 模块执行可能改动环境，需确认执行。")
        if token == "--":
            break
        if not token.startswith("-"):
            break

    return CommandDecision("confirm", "medium", f"{first} 将运行脚本或模块，需确认执行。")


def _analyze_find_command(tokens: list[str]) -> CommandDecision:
    for token in tokens[1:]:
        if token in FIND_MUTATING_TOKENS or token.startswith("-exec") or token.startswith("-ok"):
            return CommandDecision("confirm", "medium", "find 参数可能改动文件或执行外部命令，需确认执行。")
    return CommandDecision("execute", "low", "find 只读查询。")


def _analyze_sed_command(tokens: list[str]) -> CommandDecision:
    for token in tokens[1:]:
        if token in SED_MUTATING_FLAGS or token.startswith("-i"):
            return CommandDecision("confirm", "medium", "sed -i 会改动文件，需确认执行。")
    return CommandDecision("execute", "low", "sed 只读输出。")


def _option_has_flag(token: str, flag: str) -> bool:
    value = token.strip()
    if not value.startswith("-") or value == "--":
        return False
    return flag.lower() in value.lstrip("-").lower()


def _non_option_arguments(tokens: list[str]) -> list[str]:
    args: list[str] = []
    in_options = True
    for token in tokens[1:]:
        if in_options and token == "--":
            in_options = False
            continue
        if in_options and token.startswith("-") and token != "-":
            continue
        in_options = False
        args.append(token)
    return args


def _is_obvious_mass_target(token: str) -> bool:
    value = str(token or "").strip().strip("'\"").lower()
    if not value:
        return False
    direct_targets = {
        "/",
        "/*",
        "~",
        "~/",
        "~/*",
        "$home",
        "$home/",
        "$home/*",
        ".",
        "./",
        "./*",
        "*",
        ".*",
        "..",
        "../",
        "../*",
    }
    return value in direct_targets


def _catastrophic_command_reason(tokens: list[str], lowered: str) -> str | None:
    if not tokens:
        return None
    first_word = tokens[0]
    targets = _non_option_arguments(tokens)

    if first_word in {"rm", "rmdir"}:
        has_recursive = first_word == "rm" and any(_option_has_flag(token, "r") for token in tokens[1:])
        has_force = first_word == "rm" and any(_option_has_flag(token, "f") for token in tokens[1:])
        if targets and any(_is_obvious_mass_target(target) for target in targets):
            if first_word == "rmdir" or has_recursive or has_force:
                return "检测到可能删除系统、主目录或当前目录的大范围删除命令，需要输入 yes 才能执行。"

    if first_word in {"dd", "mkfs", "mkfs.ext2", "mkfs.ext3", "mkfs.ext4", "mkfs.fat", "mkfs.vfat"} and re.search(r"(/dev/|(?:^|\s)of=)", lowered):
        return "检测到可能改写块设备的命令，需要输入 yes 才能执行。"

    if first_word in {"reboot", "poweroff", "shutdown"}:
        return "检测到会中断当前设备会话的命令，需要输入 yes 才能执行。"

    if first_word in {"chmod", "chown", "chgrp"}:
        has_recursive = any(_option_has_flag(token, "r") for token in tokens[1:])
        if has_recursive and targets and any(_is_obvious_mass_target(target) for target in targets):
            return "检测到递归修改系统、主目录或当前目录权限/所有者的命令，需要输入 yes 才能执行。"

    return None


def _analyze_command(command: str, context: ToolContext) -> CommandDecision:
    stripped = command.strip()
    if not stripped:
        return CommandDecision("blocked", "low", "命令为空。")
    if len(stripped) > MAX_COMMAND_CHARS:
        return CommandDecision("blocked", "medium", "命令长度超过安全上限。")
    shell_flags = _scan_shell_structure(stripped)

    try:
        tokens = _parse_tokens(stripped)
    except ValueError:
        return CommandDecision("blocked", "medium", "命令引号或转义不合法。")

    if not tokens:
        return CommandDecision("blocked", "low", "命令为空。")

    if shell_flags.has_command_substitution:
        return CommandDecision("confirm", "medium", "检测到命令替换或多行 shell 结构，需确认执行。")

    if shell_flags.has_pipe and tokens[0] in {"curl", "wget"} and re.search(r"\|\s*(?:bash|sh|zsh)\b", stripped):
        return CommandDecision("confirm", "high", "检测到网络下载后直接执行脚本，需确认执行。")

    if shell_flags.has_redirection:
        return CommandDecision("confirm", "high", "检测到写入或读取重定向，需确认执行。")

    if shell_flags.has_composite:
        return CommandDecision("confirm", "medium", "检测到复合 shell 结构，需确认执行。")

    if any(token in SHELL_DETACH_WORDS for token in tokens):
        return CommandDecision("confirm", "medium", "检测到后台脱离式执行，需确认执行。")

    first_word = tokens[0]
    lowered = stripped.lower()
    catastrophic_reason = _catastrophic_command_reason(tokens, lowered)
    if catastrophic_reason:
        return CommandDecision("confirm", "high", catastrophic_reason, confirm_command="yes")

    if first_word in {"rm", "rmdir"} and (
        re.search(r"(^|\s)-[a-z]*r[a-z]*f|(^|\s)-[a-z]*f[a-z]*r", lowered)
        or re.search(r"(^|\s)(/|~|\$home)(\s|$)", lowered)
    ):
        return CommandDecision("confirm", "high", "检测到递归或大范围删除命令，需要输入 yes 才能执行。", confirm_command="yes")
    if first_word in {"dd", "mkfs"} and re.search(r"(/dev/|of=)", lowered):
        return CommandDecision("blocked", "high", "检测到可能改写块设备的命令，已阻止。")
    if first_word in {"reboot", "poweroff", "shutdown"}:
        return CommandDecision("blocked", "high", "检测到会中断当前设备会话的命令，已阻止。")
    if first_word in SHELL_BUILTINS_THAT_CANNOT_PERSIST:
        return CommandDecision("blocked", "medium", f"{first_word} 无法在子 shell 中持久生效。")

    if first_word in {"python", "python3"}:
        return _analyze_python_command(tokens)

    if first_word in {"node", "ruby", "lua"} and len(tokens) == 1:
        return CommandDecision("blocked", "medium", f"{first_word} 将进入交互式会话，不允许。")

    if first_word in INTERACTIVE_COMMANDS:
        return CommandDecision("blocked", "medium", f"{first_word} 属于交互式程序，不允许。")

    if first_word == "adb":
        return _analyze_adb_command(tokens)

    if first_word.startswith("termux-"):
        return _analyze_termux_command(first_word)

    if first_word == "git":
        return _analyze_git_command(tokens)

    if first_word in PACKAGE_MANAGER_SUBCOMMANDS:
        return _analyze_package_manager(tokens)

    if first_word == "find":
        return _analyze_find_command(tokens)

    if first_word == "sed":
        return _analyze_sed_command(tokens)

    safe_commands = {cmd.strip() for cmd in getattr(context.config, "safe_commands", ()) if str(cmd).strip()}
    if first_word in safe_commands or first_word in DIRECT_COMMANDS:
        return CommandDecision("execute", "low", "命中本地只读命令白名单。")

    if first_word in MUTATING_COMMANDS:
        return CommandDecision("confirm", _risk_level(first_word, "改动系统"), f"{first_word} 可能改动文件或系统状态，需确认执行。")

    return CommandDecision("confirm", "medium", "命令不在只读白名单内，需确认执行。")


def _resolve_timeout(args: dict[str, Any]) -> int:
    raw = args.get("timeout_seconds")
    if raw in {None, ""}:
        return DEFAULT_TIMEOUT_SECONDS
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_TIMEOUT_SECONDS
    return max(5, min(MAX_TIMEOUT_SECONDS, value))


def _build_pending_payload(
    *,
    command: str,
    cwd: Path,
    timeout_seconds: int,
    decision: CommandDecision,
    channel: str,
) -> dict[str, Any]:
    now = int(time.time())
    return {
        "status": "pending_confirmation",
        "command": command,
        "cwd": str(cwd),
        "channel": channel,
        "risk": decision.risk,
        "reason": decision.reason,
        "timeout_seconds": timeout_seconds,
        "created_at": now,
        "expires_at": now + PENDING_TTL_SECONDS,
        "confirm_command": decision.confirm_command or "y",
        "confirmation_required": decision.confirm_command or "y",
        "deny_command": "n",
        "pending_path": PENDING_FILE_NAME,
    }


def _reader_worker(stream: Any, stream_name: str, output_queue: queue.Queue[tuple[str, bytes | None]]) -> None:
    try:
        fd = stream.fileno()
        while True:
            try:
                chunk = os.read(fd, 512)
            except OSError:
                break
            if not chunk:
                break
            output_queue.put((stream_name, chunk))
    finally:
        output_queue.put((stream_name, None))


def _run_shell_command(
    *,
    command: str,
    cwd: Path,
    timeout_seconds: int,
    channel: str | None = None,
    brief: str = "",
    context: ToolContext | None = None,
) -> dict[str, Any]:
    shell = _shell_program()
    started = time.time()
    stdout_collector = _BoundedCollector(MAX_STDOUT_CHARS)
    stderr_collector = _BoundedCollector(MAX_STDERR_CHARS)
    event_queue: queue.Queue[tuple[str, bytes | None]] = queue.Queue()
    decoders = {
        "stdout": codecs.getincrementaldecoder("utf-8")("replace"),
        "stderr": codecs.getincrementaldecoder("utf-8")("replace"),
    }
    stream_event_chars = {"stdout": 0, "stderr": 0}
    stream_event_counts = {"stdout": 0, "stderr": 0}
    stream_event_capped = {"stdout": False, "stderr": False}
    active_readers = 0
    timed_out = False

    def emit_stream_text(stream_name: str, text: str) -> None:
        if not text:
            return
        if (
            stream_event_counts[stream_name] >= MAX_STREAM_EVENTS_PER_STREAM
            or stream_event_chars[stream_name] >= MAX_STREAM_EVENT_TOTAL_CHARS
        ):
            if not stream_event_capped[stream_name]:
                stream_event_capped[stream_name] = True
                _emit_tool_event(
                    context,
                    f"tool_{stream_name}",
                    {
                        "tool": "command",
                        "channel": channel or _command_channel(command),
                        "command": command,
                        "brief": brief,
                        "compact_receipt": True,
                        "cwd": str(cwd),
                        "text": f"\n...[live {stream_name} output capped]...\n",
                        "stream": stream_name,
                        "capped": True,
                    },
                )
            return

        remaining = MAX_STREAM_EVENT_TOTAL_CHARS - stream_event_chars[stream_name]
        snippet = text[: min(MAX_STREAM_EVENT_CHARS, remaining)]
        stream_event_chars[stream_name] += len(snippet)
        stream_event_counts[stream_name] += 1
        _emit_tool_event(
            context,
            f"tool_{stream_name}",
            {
                "tool": "command",
                "channel": channel or _command_channel(command),
                "command": command,
                "brief": brief,
                "compact_receipt": True,
                "cwd": str(cwd),
                "text": snippet,
                "stream": stream_name,
            },
        )

    _emit_tool_event(
        context,
        "tool_start",
        {
            "tool": "command",
            "channel": channel or _command_channel(command),
            "command": command,
            "brief": brief,
            "compact_receipt": True,
            "cwd": str(cwd),
            "shell": shell,
            "timeout_seconds": timeout_seconds,
            **(_tool_actor_payload(context) if context is not None else {}),
        },
    )

    process = subprocess.Popen(
        [shell, "-lc", command],
        cwd=str(cwd),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        stdin=subprocess.DEVNULL,
        text=False,
        bufsize=0,
        env=os.environ.copy(),
    )

    threads: list[threading.Thread] = []
    for stream_name, stream in (("stdout", process.stdout), ("stderr", process.stderr)):
        if stream is None:
            continue
        active_readers += 1
        thread = threading.Thread(
            target=_reader_worker,
            args=(stream, stream_name, event_queue),
            daemon=True,
        )
        thread.start()
        threads.append(thread)

    while active_readers > 0 or process.poll() is None:
        if time.time() - started > timeout_seconds:
            timed_out = True
            process.kill()
            break
        try:
            stream_name, chunk = event_queue.get(timeout=STREAM_POLL_INTERVAL_SECONDS)
        except queue.Empty:
            continue
        if chunk is None:
            tail = decoders[stream_name].decode(b"", final=True)
            if tail:
                if stream_name == "stdout":
                    stdout_collector.append(tail)
                else:
                    stderr_collector.append(tail)
                emit_stream_text(stream_name, tail)
            active_readers = max(0, active_readers - 1)
            continue
        text = decoders[stream_name].decode(chunk)
        if not text:
            continue
        if stream_name == "stdout":
            stdout_collector.append(text)
        else:
            stderr_collector.append(text)
        emit_stream_text(stream_name, text)

    if timed_out:
        try:
            process.wait(timeout=1.0)
        except subprocess.TimeoutExpired:
            pass
    else:
        process.wait()

    deadline = time.time() + 0.5
    while active_readers > 0 and time.time() < deadline:
        try:
            stream_name, chunk = event_queue.get(timeout=STREAM_POLL_INTERVAL_SECONDS)
        except queue.Empty:
            continue
        if chunk is None:
            tail = decoders[stream_name].decode(b"", final=True)
            if tail:
                if stream_name == "stdout":
                    stdout_collector.append(tail)
                else:
                    stderr_collector.append(tail)
                emit_stream_text(stream_name, tail)
            active_readers = max(0, active_readers - 1)
            continue
        text = decoders[stream_name].decode(chunk)
        if not text:
            continue
        if stream_name == "stdout":
            stdout_collector.append(text)
        else:
            stderr_collector.append(text)
        emit_stream_text(stream_name, text)

    for thread in threads:
        thread.join(timeout=0.1)

    stdout = stdout_collector.text()
    stderr = stderr_collector.text()
    stdout_truncated = stdout_collector.truncated
    stderr_truncated = stderr_collector.truncated

    if timed_out:
        return {
            "status": "timeout",
            "command": command,
            "cwd": str(cwd),
            "returncode": None,
            "duration_seconds": round(time.time() - started, 3),
            "stdout": stdout,
            "stderr": stderr,
            "stdout_truncated": stdout_truncated,
            "stderr_truncated": stderr_truncated,
            "shell": shell,
            "timeout_seconds": timeout_seconds,
        }

    return {
        "status": "ok" if process.returncode == 0 else "error",
        "command": command,
        "cwd": str(cwd),
        "returncode": process.returncode,
        "duration_seconds": round(time.time() - started, 3),
        "stdout": stdout,
        "stderr": stderr,
        "stdout_truncated": stdout_truncated,
        "stderr_truncated": stderr_truncated,
        "shell": shell,
        "timeout_seconds": timeout_seconds,
    }


def _execute_command_tool(args: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    raw_command = str(args.get("command") or "").strip()
    timeout_seconds = _resolve_timeout(args)
    decision = _analyze_command(raw_command, context)
    channel = _command_channel(raw_command)
    reason = decision.reason
    base = {
        "tool": "command",
        "brief": str(args.get("brief") or "").strip(),
        "channel": channel,
        "command": raw_command,
        "cwd": str(context.cwd),
        "home": str(context.home),
        "risk": decision.risk,
        "reason": reason,
        "timeout_seconds": timeout_seconds,
    }
    command_warnings = _command_write_warnings(raw_command, decision)
    if command_warnings:
        base["warnings"] = command_warnings
        base["write_intent"] = "direct_file_write"
        base["recovery_hint"] = [
            "If this was a code edit fallback, retry apply_patch with a smaller exact-context hunk.",
            "Use command for verification; use apply_patch for file edits.",
        ]

    if decision.action == "blocked":
        return {
            **base,
            "status": "blocked",
            "message": reason,
        }

    if decision.action == "confirm" and (decision.confirm_command or "y") == "yes":
        pending = _build_pending_payload(
            command=raw_command,
            cwd=context.cwd,
            timeout_seconds=timeout_seconds,
            decision=decision,
            channel=channel,
        )
        _store_pending_command(context.config, pending)
        return {
            **base,
            **pending,
            "message": reason,
        }

    result = _run_shell_command(
        command=raw_command,
        cwd=context.cwd,
        timeout_seconds=timeout_seconds,
        channel=channel,
        brief=str(args.get("brief") or "").strip(),
        context=context,
    )
    return {
        **base,
        **result,
    }


def _execute_compact_context_tool(args: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    summary = str(args.get("summary") or "").strip()
    preserved_details = str(args.get("preserved_details") or "").strip()
    persona_path = context.persona_path
    if persona_path is None:
        return {
            "status": "error",
            "tool": "compact_context",
            "message": "缺少 persona_path，无法写入角色上下文。",
        }
    if not summary:
        return {
            "status": "error",
            "tool": "compact_context",
            "message": "summary 为空，拒绝覆盖角色上下文。",
        }

    merged = summary
    if preserved_details:
        merged = f"{summary.rstrip()}\n\n保留细节：\n{preserved_details.strip()}"

    target_limit = int(
        getattr(
            context.config,
            "advisorling_compact_target_chars",
            getattr(context.config, "context_compact_target_chars", MAX_COMPACT_CONTEXT_CHARS),
        )
        or MAX_COMPACT_CONTEXT_CHARS
    )
    compacted, truncated = _truncate_text(merged, max(1000, target_limit))
    persona_path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write_text_file(persona_path, compacted.rstrip() + "\n")
    return {
        "status": "ok",
        "tool": "compact_context",
        "context_path": str(persona_path),
        "context_chars": len(compacted),
        "truncated": truncated,
    }


def _context_manage_paths(args: dict[str, Any], context: ToolContext) -> list[tuple[str, Path]]:
    target = str(args.get("target") or "both").strip().lower()
    section = str(args.get("section") or "").strip().lower()
    paths: list[tuple[str, Path]] = []

    def _append(label: str, path: Path | None) -> None:
        if path is None:
            return
        raw_path = str(path).strip()
        if not raw_path:
            return
        path = Path(raw_path).expanduser()
        candidate = (label, path)
        if candidate not in paths:
            paths.append(candidate)

    if target == "all":
        _append("role", context.persona_path)
        _append("liaison", getattr(context, "liaison_path", None))
        return paths

    if target == "both":
        _append("role", context.persona_path)
        return paths

    if target in {"role", "persona", "shared", "fastmemory", "public", "fastmemory.public", "fastmemory_public", "dualstar", "pair", "link"}:
        _append("role", context.persona_path)
        return paths

    if target == "liaison":
        _append("liaison", getattr(context, "liaison_path", None))
        return paths

    if section in {"role", "persona", "shared", "public", "fastmemory", "dualstar", "pair", "link"}:
        _append("role", context.persona_path)
    if section == "liaison":
        _append("liaison", getattr(context, "liaison_path", None))
    return paths


def _fold_tool_receipts(text: str) -> tuple[str, int]:
    blocks = re.split(r"\n{2,}", str(text or "").strip())
    folded_blocks: list[str] = []
    folded_count = 0
    for block in blocks:
        lines = block.splitlines()
        kept: list[str] = []
        tool_lines = 0
        in_tools = False
        for line in lines:
            if line.strip() == "工具：":
                in_tools = True
                kept.append("工具：")
                continue
            if in_tools:
                if line.startswith("回复："):
                    in_tools = False
                    kept.append(line)
                elif line.startswith("- "):
                    summary = re.split(r"\s+\|\s+(?:stdout|stderr):", line, maxsplit=1)[0].strip()
                    if summary:
                        kept.append(f"{summary} | output: 已折叠")
                    tool_lines += 1
                else:
                    tool_lines += 1
                continue
            kept.append(line)
        if not tool_lines:
            folded_blocks.append(block)
            continue
        if tool_lines:
            folded_count += tool_lines
        folded_blocks.append("\n".join(kept).strip())
    return "\n\n".join(block for block in folded_blocks if block).strip(), folded_count


def _fold_old_tool_receipts_only(text: str, *, keep_last: int = 3) -> tuple[str, int]:
    blocks = [block.strip() for block in re.split(r"\n{2,}", str(text or "").strip()) if block.strip()]
    if not blocks:
        return "", 0
    preserved: list[str] = []
    folded_count = 0
    for index, block in enumerate(blocks):
        if index >= max(0, len(blocks) - keep_last):
            preserved.append(block)
            continue
        folded, count = _fold_tool_receipts(block)
        if count:
            folded_count += count
            preserved.append(folded)
        else:
            preserved.append(block)
    return "\n\n".join(preserved).strip(), folded_count


def _compact_with_summary(
    *,
    mode: str,
    old_text: str,
    compacted_text: str,
    target_limit: int,
) -> tuple[str, bool, int]:
    old_text = str(old_text or "").strip()
    compacted_text = str(compacted_text or "").strip()
    before_chars = len(old_text)
    if not old_text:
        return "", False, before_chars
    if mode == "fold_tools":
        folded, _folded_count = _fold_old_tool_receipts_only(old_text)
        return folded.rstrip() + "\n", False, before_chars
    if not compacted_text:
        return old_text.rstrip() + "\n", False, before_chars
    if mode == "half":
        split = len(old_text) // 2
        newline = old_text.find("\n\n", split)
        if newline >= 0:
            split = newline + 2
        newer = old_text[split:].strip()
        merged = f"[旧上下文半量 compact]\n{compacted_text}"
        if newer:
            merged += f"\n\n[较新上下文原文保留]\n{newer}"
    else:
        merged = f"[全量 compact]\n{compacted_text}"
    compacted, truncated = _truncate_text(merged, max(1000, target_limit))
    return compacted.rstrip() + "\n", truncated, before_chars


def _execute_context_tool(args: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    percent, level = _normalize_context_budget_percent(args)
    turns_remaining = max(1, min(5, int(args.get("turns") or args.get("turns_remaining") or 1)))
    brief = str(args.get("brief") or "").strip()
    reason = str(args.get("reason") or "").strip()
    if percent >= 100:
        message = "已恢复到默认全量上下文可见度。"
    elif turns_remaining <= 1:
        message = f"下一轮上下文可见度约 {percent}%。"
    else:
        message = f"接下来 {turns_remaining} 轮上下文可见度约 {percent}%。"
    state = save_context_budget(
        context.config,
        percent=percent,
        level=level,
        turns_remaining=turns_remaining,
        reason=reason,
        brief=brief,
        message=(
            f"已把下一轮上下文回传预算设为 {percent}%"
            + (f"，持续 {turns_remaining} 轮" if turns_remaining > 1 else "")
            + "。"
        ),
    )
    state = dict(state)
    state.update(
        {
            "status": "ok",
            "tool": "context",
            "brief": brief or reason or "调整上下文预算",
            "percent": percent,
            "level": level,
            "turns_remaining": 0 if percent >= 100 else turns_remaining,
            "reason": reason,
            "context_budget_percent": percent,
            "context_budget_level": level,
            "context_budget_bar": _context_budget_bar(percent),
            "context_budget_text": f"{_context_budget_bar(percent)} ≈{percent}%",
            "applies_from": "next_model_turn",
            "message": message,
        }
    )
    if percent < 100:
        state["hint"] = "低可见度不会删除旧上下文；如果任务需要更多背景，下一轮可再次调用 context，把 percent 调高到 66、85 或 100。"
    return state


def _execute_context_manage_tool(args: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    mode = str(args.get("mode") or "half").strip().lower()
    if mode in {"status", "list", "replace", "fold"}:
        payload = _execute_contextmanage_tool(args, context)
        if isinstance(payload, dict):
            payload = dict(payload)
            payload["tool"] = "context_manage"
            payload.setdefault("compat_tool", "contextmanage")
        return payload
    if mode in {"full", "half", "fold_tools"}:
        return {
            "status": "error",
            "tool": "context_manage",
            "mode": mode,
            "message": (
                "旧 context_manage 的 full/half/fold_tools 已停用，避免继续整理不存在的 persona 文本。"
                "请改用 contextmanage：status/list 查看 entries，replace 按 entry id 区间摘要替换，fold 折叠旧工具 entries。"
            ),
        }
    return {"status": "error", "tool": "context_manage", "message": "mode 只能是 status/list/replace/fold。"}


def _execute_contextmanage_tool(args: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    mode = str(args.get("mode") or args.get("action") or "status").strip().lower()
    if mode in {"status", "list"}:
        payload = {
            "status": "ok",
            "tool": "contextmanage",
            "mode": mode,
            "brief": str(args.get("brief") or "").strip() or ("列出 entries" if mode == "list" else "查看 entries 状态"),
            **context_entries_status(context.config),
        }
        if mode == "list":
            try:
                limit = int(args.get("limit") or 40)
            except (TypeError, ValueError):
                limit = 40
            payload["entries"] = list_context_entry_summaries(
                context.config,
                limit=limit,
                include_hidden=bool(args.get("include_hidden", False)),
            )
        payload["message"] = "context entries 状态已读取。"
        return payload

    if mode == "replace":
        summary = str(args.get("summary") or args.get("compacted_context") or args.get("role_summary") or "").strip()
        try:
            start_id, end_id = parse_entry_range(
                entry_id=str(args.get("id") or args.get("entry_id") or "").strip(),
                start_id=str(args.get("start_id") or "").strip(),
                end_id=str(args.get("end_id") or "").strip(),
                id_range=str(args.get("id_range") or args.get("range") or "").strip(),
            )
            result = replace_context_entries(
                context.config,
                start_id=start_id,
                end_id=end_id,
                summary=summary,
                speaker="contextmanage",
                reason=str(args.get("reason") or args.get("brief") or "").strip(),
            )
        except Exception as exc:
            return {
                "status": "error",
                "tool": "contextmanage",
                "mode": mode,
                "brief": str(args.get("brief") or "").strip(),
                "message": str(exc),
            }
        return {
            "status": "ok",
            "tool": "contextmanage",
            "mode": mode,
            "brief": str(args.get("brief") or "").strip() or "replace entries",
            **result,
            **context_entries_status(context.config),
            "message": f"已用 {result.get('summary_id')} 替换 {result.get('source_ids')}。",
        }

    if mode == "fold":
        try:
            keep_last_raw = args.get("keep_last")
            keep_last = int(6 if keep_last_raw in {None, ""} else keep_last_raw)
        except (TypeError, ValueError):
            keep_last = 6
        try:
            result = fold_context_tool_entries(
                context.config,
                keep_last=keep_last,
                reason=str(args.get("reason") or args.get("brief") or "").strip(),
            )
        except Exception as exc:
            return {
                "status": "error",
                "tool": "contextmanage",
                "mode": mode,
                "brief": str(args.get("brief") or "").strip(),
                "message": str(exc),
            }
        return {
            "status": "ok",
            "tool": "contextmanage",
            "mode": mode,
            "brief": str(args.get("brief") or "").strip() or "fold tool entries",
            **result,
            **context_entries_status(context.config),
            "message": "旧工具 entries 已折叠。" if int(result.get("folded") or 0) > 0 else str(result.get("message") or "无需折叠。"),
        }

    result = _execute_context_manage_tool(args, context)
    if not isinstance(result, dict):
        return result
    payload = dict(result)
    payload["tool"] = "contextmanage"
    payload.setdefault("compat_tool", "context_manage")
    return payload


def _execute_tool_manage_tool(args: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    action = str(args.get("action") or "list").strip().lower()
    include_schema = bool(args.get("include_schema", True))
    raw_tools = args.get("tools") or args.get("tool") or []
    if isinstance(raw_tools, str):
        raw_tools = [raw_tools]
    if not isinstance(raw_tools, (list, tuple)):
        raw_tools = []
    tool_names = [str(item or "").strip() for item in raw_tools if str(item or "").strip()]

    toolbox = context.toolbox or ToolBox(context.config)
    changed: list[str] = []

    if action == "list":
        return {
            "status": "ok",
            "tool": "tool_manage",
            "brief": str(args.get("brief") or "").strip() or "列出工具箱状态",
            "expanded_count": sum(1 for row in toolbox.overview(include_hidden=True) if row["expanded"]),
            "total_count": len(toolbox.all_names()),
            "tools": toolbox.overview(include_hidden=True, include_detail=False),
            "message": "toolbox list ok.",
        }

    if action == "inspect":
        if not tool_names:
            return {
                "status": "error",
                "tool": "tool_manage",
                "message": "inspect 需要 tools 或 tool 参数。",
            }
        return {
            "status": "ok",
            "tool": "tool_manage",
            "brief": str(args.get("brief") or "").strip() or "查看工具详情",
            "tools": toolbox.inspect(tool_names, include_schema=include_schema),
            "message": "toolbox inspect ok.",
        }

    if action in {"expand", "collapse"}:
        if not tool_names:
            return {
                "status": "error",
                "tool": "tool_manage",
                "message": f"{action} 需要 tools 或 tool 参数。",
            }
        changed = toolbox.set_visibility(tool_names, expanded=(action == "expand"))
        return {
            "status": "ok",
            "tool": "tool_manage",
            "brief": str(args.get("brief") or "").strip() or f"{action} tools",
            "action": action,
            "requested": tool_names,
            "changed": changed,
            "tools": toolbox.inspect(tool_names, include_schema=include_schema),
            "message": f"toolbox {action} ok." if changed else "工具状态未变化。",
        }

    if action == "expand_all":
        changed = [name for name in toolbox.all_names() if not toolbox.is_expanded(name)]
        toolbox.reset()
        return {
            "status": "ok",
            "tool": "tool_manage",
            "brief": str(args.get("brief") or "").strip() or "展开全部工具",
            "action": action,
            "changed": changed,
            "tools": toolbox.overview(include_hidden=True, include_detail=False),
            "message": "toolbox expand_all ok.",
        }

    if action == "collapse_all":
        changed = toolbox.set_visibility(
            [name for name in toolbox.all_names() if name != "tool_manage"],
            expanded=False,
        )
        return {
            "status": "ok",
            "tool": "tool_manage",
            "brief": str(args.get("brief") or "").strip() or "折叠全部工具",
            "action": action,
            "changed": changed,
            "tools": toolbox.overview(include_hidden=True, include_detail=False),
            "message": "toolbox collapse_all ok.",
        }

    if action == "reset":
        changed = toolbox.reset()
        return {
            "status": "ok",
            "tool": "tool_manage",
            "brief": str(args.get("brief") or "").strip() or "重置工具箱",
            "action": action,
            "changed": changed,
            "tools": toolbox.overview(include_hidden=True, include_detail=False),
            "message": "toolbox reset ok.",
        }

    return {
        "status": "error",
        "tool": "tool_manage",
        "message": "action 只能是 list / inspect / expand / collapse / expand_all / collapse_all / reset。",
    }


def _string_list_arg(value: Any) -> list[str]:
    if isinstance(value, str):
        if "\n" in value:
            raw_items = value.splitlines()
        else:
            raw_items = value.split(",")
    elif isinstance(value, (list, tuple)):
        raw_items = list(value)
    else:
        raw_items = []
    normalized: list[str] = []
    seen: set[str] = set()
    for raw in raw_items:
        text = " ".join(str(raw or "").split()).strip()
        key = text.lower()
        if not text or key in seen:
            continue
        seen.add(key)
        normalized.append(text)
    return normalized


def _execute_memory_add_tool(args: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    date = str(args.get("date") or "").strip()
    diary = str(args.get("diary") or args.get("content") or "").strip()
    keywords = _string_list_arg(args.get("keywords") or [])
    mode = str(args.get("mode") or "append").strip().lower()
    consume_source = bool(args.get("consume_source", False))
    if not diary:
        return {
            "status": "error",
            "tool": "memory_add",
            "brief": str(args.get("brief") or "").strip(),
            "message": "diary 为空。",
        }
    try:
        result = memory_add_record(
            context.config,
            date=date,
            diary=diary,
            keywords=keywords,
            mode=mode,
            consume_source=consume_source,
        )
    except Exception as exc:
        return {
            "status": "error",
            "tool": "memory_add",
            "brief": str(args.get("brief") or "").strip(),
            "message": str(exc),
        }
    return {
        "status": "ok",
        "tool": "memory_add",
        "brief": str(args.get("brief") or "").strip(),
        "message": "永久记忆已写入。",
        **result,
    }


def _execute_memory_check_tool(args: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    keywords = _string_list_arg(args.get("keywords") or [])
    try:
        limit = max(1, min(8, int(args.get("limit") or 5)))
    except (TypeError, ValueError):
        limit = 5
    try:
        result = memory_check_records(context.config, keywords=keywords, limit=limit)
    except Exception as exc:
        return {
            "status": "error",
            "tool": "memory_check",
            "brief": str(args.get("brief") or "").strip(),
            "message": str(exc),
        }
    return {
        "status": "ok",
        "tool": "memory_check",
        "brief": str(args.get("brief") or "").strip(),
        "message": "记忆检索完成。",
        **result,
    }


def _execute_memory_read_tool(args: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    dates = _string_list_arg(args.get("dates") or args.get("date") or [])
    try:
        result = memory_read_records(context.config, dates=dates)
    except Exception as exc:
        return {
            "status": "error",
            "tool": "memory_read",
            "brief": str(args.get("brief") or "").strip(),
            "message": str(exc),
        }
    return {
        "status": "ok",
        "tool": "memory_read",
        "brief": str(args.get("brief") or "").strip(),
        "message": "记忆读取完成。",
        **result,
    }


def _execute_memory_status_tool(args: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    action = str(args.get("action") or "status").strip().lower()
    if action == "clear_datememory":
        path = clear_datememory_payload(context.config)
        return {
            "status": "ok",
            "tool": "memory_status",
            "action": action,
            "path": str(path),
            "message": "datememory 已清空。",
            **memory_status(context.config),
        }
    ensure_memory_layout(context.config)
    return {
        "status": "ok",
        "tool": "memory_status",
        "action": "status",
        "message": "记忆状态已读取。",
        **memory_status(context.config),
    }


def show_pending_command(config: Any) -> dict[str, Any]:
    pending = _load_pending_command(config)
    if pending is None:
        return {"status": "empty", "message": "当前没有待确认命令。"}
    remaining = max(0, int(pending.get("expires_at") or 0) - int(time.time()))
    return {
        **pending,
        "remaining_seconds": remaining,
    }


def _normalize_confirmation_answer(answer: Any) -> str:
    return " ".join(str(answer or "").split()).strip().lower()


def confirm_pending_command(
    config: Any,
    answer: Any = "",
    event_callback: Callable[[str, dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    pending = _load_pending_command(config)
    if pending is None:
        return {"status": "empty", "message": "当前没有待确认命令。"}

    expected = _normalize_confirmation_answer(pending.get("confirm_command") or "y") or "y"
    actual = _normalize_confirmation_answer(answer)
    accepted_answers = {expected, "yes"} if expected == "y" else {"yes"}
    if actual not in accepted_answers:
        return {
            "status": "blocked",
            "tool": "command",
            "channel": str(pending.get("channel") or _command_channel(str(pending.get("command") or ""))),
            "command": str(pending.get("command") or ""),
            "cwd": str(pending.get("cwd") or ""),
            "risk": str(pending.get("risk") or "medium"),
            "reason": str(pending.get("reason") or ""),
            "confirm_command": expected,
            "deny_command": str(pending.get("deny_command") or "n"),
            "message": f"需要输入 {expected} 才能执行该命令。",
        }

    _clear_pending_command(config)
    cwd = Path(str(pending.get("cwd") or Path.cwd())).expanduser()
    timeout_seconds = _resolve_timeout({"timeout_seconds": pending.get("timeout_seconds")})
    channel = str(pending.get("channel") or _command_channel(str(pending.get("command") or "")))
    context = ToolContext(
        cwd=cwd,
        home=Path.home(),
        config=config,
        event_callback=event_callback,
    )
    result = _run_shell_command(
        command=str(pending.get("command") or ""),
        cwd=cwd,
        timeout_seconds=timeout_seconds,
        channel=channel,
        brief=str(pending.get("brief") or ""),
        context=context,
    )
    result["approved_via"] = actual or expected
    result["channel"] = channel
    result["risk"] = str(pending.get("risk") or "medium")
    result["reason"] = str(pending.get("reason") or "")
    result["confirm_command"] = expected
    if event_callback is not None:
        event_callback("tool_result", dict(result))
    return result


def reject_pending_command(config: Any) -> dict[str, Any]:
    pending = _clear_pending_command(config)
    if pending is None:
        return {"status": "empty", "message": "当前没有待确认命令。"}
    return {
        "status": "rejected",
        "channel": str(pending.get("channel") or _command_channel(str(pending.get("command") or ""))),
        "command": str(pending.get("command") or ""),
        "cwd": str(pending.get("cwd") or ""),
        "risk": str(pending.get("risk") or "medium"),
        "reason": str(pending.get("reason") or ""),
        "message": "已取消待确认命令。",
    }


class ToolRegistry:
    def __init__(
        self,
        config: Any,
        *,
        error_cls: type[Exception] = RuntimeError,
        include_command: bool = True,
        include_compact: bool = False,
    ) -> None:
        self.config = config
        self.error_cls = error_cls
        self._tools: dict[str, ToolDefinition] = {}
        if include_command:
            self._tools["command"] = ToolDefinition(
                name="command",
                description=(
                    "Run one local shell command in the current Termux working directory. "
                    "Supports shell commands, adb TCP workflows, and termux-api commands. "
                    "Commands usually run immediately and return bounded stdout/stderr. "
                    "Clearly catastrophic commands create a pending confirmation and require "
                    "the human to type yes before execution; blocked commands are not executed."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "command": {
                            "type": "string",
                            "description": "One shell command to run in the current cwd.",
                        },
                        "brief": {
                            "type": "string",
                            "description": "Short human-facing purpose shown in the terminal receipt, such as 列目录 or 检查服务状态.",
                        },
                        "timeout_seconds": {
                            "type": "integer",
                            "minimum": 5,
                            "maximum": MAX_TIMEOUT_SECONDS,
                            "description": "Optional execution timeout in seconds.",
                        },
                    },
                    "required": ["command"],
                    "additionalProperties": False,
                },
                handler=_execute_command_tool,
            )
            self._tools["terminal"] = ToolDefinition(
                name="terminal",
                description=(
                    "Create or control a collaborative Termux terminal backed by tmux. "
                    "Use action=start for long-running or interactive workflows: it opens a new "
                    "Termux foreground session, logs terminal output under aidebug/projectling/terminal output, "
                    "and returns log path/line/size metadata. Use action=send to send another "
                    "command to the same terminal, action=info to inspect log metadata, and "
                    "action=stop or action=close to end the tmux session. Read large logs with command using "
                    "sed/head/tail slices instead of copying the whole log. Assistant-sent command strings "
                    "must be clearly safe; risky or blocked commands are rejected here and should go through "
                    "command so the human confirmation gate can handle them."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "action": {
                            "type": "string",
                            "enum": ["start", "send", "info", "stop", "close"],
                            "description": "Terminal action. Defaults to start.",
                        },
                        "command": {
                            "type": "string",
                            "description": "Command/text to run or send. Required for send, optional for start.",
                        },
                        "brief": {
                            "type": "string",
                            "description": "Short human-facing purpose shown in the terminal receipt.",
                        },
                        "session_name": {
                            "type": "string",
                            "description": "Optional tmux session name. Defaults to the latest session for send/info/stop.",
                        },
                        "cwd": {
                            "type": "string",
                            "description": "Optional working directory for action=start.",
                        },
                        "enter": {
                            "type": "boolean",
                            "description": "For action=send, press Enter after sending text. Defaults to true.",
                        },
                    },
                    "required": ["action"],
                    "additionalProperties": False,
                },
                handler=_execute_terminal_tool,
            )
            self._tools["aidebug"] = ToolDefinition(
                name="aidebug",
                description=(
                    "Inspect or write AITermux aidebug diagnostics. Use action=status to list "
                    "motd/zshrc/bootstrap/projectling logs and metadata, action=health to score each "
                    "runtime/debug chain, action=read to read a bounded head/tail/slice from an aidebug file, "
                    "and action=event to append a diagnostic note. "
                    "projectying is excluded because it has its own Aidebug chain."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "action": {
                            "type": "string",
                            "enum": ["status", "read", "event", "health"],
                            "description": "aidebug action. Defaults to status.",
                        },
                        "brief": {
                            "type": "string",
                            "description": "Short human-facing purpose shown in the terminal receipt.",
                        },
                        "path": {
                            "type": "string",
                            "description": "Relative path inside aidebug, for action=read.",
                        },
                        "mode": {
                            "type": "string",
                            "enum": ["head", "tail", "slice"],
                            "description": "Read mode for action=read. Defaults to tail.",
                        },
                        "lines": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": 1000,
                            "description": "Number of lines for head/tail reads.",
                        },
                        "start_line": {
                            "type": "integer",
                            "minimum": 1,
                            "description": "Start line for slice reads.",
                        },
                        "end_line": {
                            "type": "integer",
                            "minimum": 1,
                            "description": "End line for slice reads.",
                        },
                        "component": {
                            "type": "string",
                            "description": "Component name for action=event, such as motd, zshrc, projectling, bootstrap.",
                        },
                        "message": {
                            "type": "string",
                            "description": "Diagnostic message for action=event.",
                        },
                    },
                    "required": ["action"],
                    "additionalProperties": False,
                },
                handler=_execute_aidebug_tool,
            )
            self._tools["update_plan"] = ToolDefinition(
                name="update_plan",
                description=(
                    "Shared visible task plan for ProjectLing. Use mode=todo for medium multi-step tasks and "
                    "mode=plan for complex phased work. Call action=start to create a plan, action=update whenever "
                    "a step starts, finishes, changes, or blocks, action=complete for the final step, status to inspect, "
                    "and reset to clear. Every start/update/complete triggers planner review before execution continues."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "action": {
                            "type": "string",
                            "enum": ["status", "start", "update", "complete", "reset"],
                            "description": "Plan action. start creates/replaces a task plan; update changes one or more steps; complete marks the active/selected step done; status only reads; reset clears.",
                        },
                        "mode": {
                            "type": "string",
                            "enum": ["todo", "plan"],
                            "description": "todo is a simple checklist; plan is a phased plan with phase labels such as pause/A/B/C/D.",
                        },
                        "title": {
                            "type": "string",
                            "description": "Human-facing plan title.",
                        },
                        "phase": {
                            "type": "string",
                            "description": "Optional phase label for plan mode, such as pause, A, B, C.",
                        },
                        "items": {
                            "type": "array",
                            "description": "Plan items to create or merge. For start, this replaces the plan by default.",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "id": {"type": "string", "description": "Stable step id, such as T1 or A2."},
                                    "title": {"type": "string", "description": "Step title."},
                                    "status": {
                                        "type": "string",
                                        "enum": ["pending", "in_progress", "done", "blocked"],
                                        "description": "Step status.",
                                    },
                                    "phase": {"type": "string", "description": "Optional phase label."},
                                    "note": {"type": "string", "description": "Short status note."},
                                },
                                "required": ["title"],
                                "additionalProperties": False,
                            },
                        },
                        "replace_items": {
                            "type": "boolean",
                            "description": "When true, replace all existing items instead of merging. Defaults true for start and false for update.",
                        },
                        "step_id": {
                            "type": "string",
                            "description": "Step id to update. Use this when completing or blocking a single step.",
                        },
                        "step_title": {
                            "type": "string",
                            "description": "Optional title for a step_id update, or title for a new step.",
                        },
                        "status": {
                            "type": "string",
                            "enum": ["pending", "in_progress", "done", "blocked"],
                            "description": "Status for step_id/current step.",
                        },
                        "note": {
                            "type": "string",
                            "description": "Short note for the update.",
                        },
                        "next": {
                            "type": "string",
                            "description": "Executor's proposed next action after this update.",
                        },
                        "brief": {
                            "type": "string",
                            "description": "Short human-facing purpose shown in the receipt.",
                        },
                    },
                    "required": ["action"],
                    "additionalProperties": False,
                },
                handler=_execute_update_plan_tool,
            )
            self._tools["apply_patch"] = ToolDefinition(
                name="apply_patch",
                description=(
                    "Create or edit files in the current working directory. DeepSeek should prefer the "
                    "structured form: operation + target_file + content/find/replace, or edits[] for "
                    "multiple small operations. Use operation=write for full-file create/replace, "
                    "operation=replace for exact text replacement, append/prepend/insert_before/"
                    "insert_after for simple insertions, and delete for text or file deletion. Unified "
                    "diff and *** Begin Patch remain available for advanced exact-context patches. "
                    "Use this for code edits instead of shell heredocs, python writes, sed -i, tee, "
                    "or ad-hoc file rewriting."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "operation": {
                            "type": "string",
                            "enum": ["write", "replace", "append", "prepend", "insert_after", "insert_before", "delete", "patch"],
                            "description": "Preferred DeepSeek-safe edit operation. write creates/replaces a full file; replace changes exact find text; delete removes find text or deletes the file when find is omitted; patch falls back to patch/diff text.",
                        },
                        "patch": {
                            "type": "string",
                            "description": "Advanced fallback: unified diff, fenced diff block, or structured *** Begin Patch text. Prefer structured operation fields unless exact diff context is required.",
                        },
                        "diff": {
                            "type": "string",
                            "description": "Compatibility alias for patch. Must still contain diff/patch text, not an explanation.",
                        },
                        "patch_text": {
                            "type": "string",
                            "description": "Compatibility alias for patch text when patch is not available.",
                        },
                        "content": {
                            "type": "string",
                            "description": "Structured edit content. For operation=write this is the full file. For append/prepend/insert this is inserted text. Compatibility alias for patch text when no operation is set.",
                        },
                        "text": {
                            "type": "string",
                            "description": "Compatibility alias for content or patch text.",
                        },
                        "find": {
                            "type": "string",
                            "description": "Exact text to find for operation=replace/delete/insert_before/insert_after.",
                        },
                        "old_text": {
                            "type": "string",
                            "description": "Compatibility alias for find.",
                        },
                        "replace": {
                            "type": "string",
                            "description": "Replacement text for operation=replace. Empty string is allowed.",
                        },
                        "replacement": {
                            "type": "string",
                            "description": "Compatibility alias for replace.",
                        },
                        "new_text": {
                            "type": "string",
                            "description": "Compatibility alias for replace.",
                        },
                        "insert": {
                            "type": "string",
                            "description": "Compatibility alias for content in insert/append/prepend operations.",
                        },
                        "marker": {
                            "type": "string",
                            "description": "Insertion marker for operation=insert_before or insert_after. Alias of find.",
                        },
                        "occurrence": {
                            "description": "Which match to edit. Use first, last, all, or a 1-based integer. Defaults to first.",
                            "anyOf": [
                                {"type": "string", "enum": ["first", "last", "all"]},
                                {"type": "integer", "minimum": 1},
                            ],
                        },
                        "file": {
                            "type": "string",
                            "description": "Optional target file. Prefer target_file.",
                        },
                        "path": {
                            "type": "string",
                            "description": "Compatibility alias for file.",
                        },
                        "target_file": {
                            "type": "string",
                            "description": "Compatibility alias for file.",
                        },
                        "filename": {
                            "type": "string",
                            "description": "Compatibility alias for file.",
                        },
                        "create_if_missing": {
                            "type": "boolean",
                            "description": "For structured append/prepend/write, allow creating the target if missing. Defaults true for write/append/prepend and false for replace/insert/delete text.",
                        },
                        "ensure_trailing_newline": {
                            "type": "boolean",
                            "description": "For operation=write, append a trailing newline when content is non-empty. Defaults true.",
                        },
                        "edits": {
                            "type": "array",
                            "description": "Multiple DeepSeek-safe structured edits executed atomically in order. Each item uses operation/target_file/content/find/replace.",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "operation": {
                                        "type": "string",
                                        "enum": ["write", "replace", "append", "prepend", "insert_after", "insert_before", "delete"],
                                        "description": "Structured edit operation.",
                                    },
                                    "target_file": {"type": "string", "description": "Target file path relative to cwd."},
                                    "file": {"type": "string", "description": "Compatibility alias for target_file."},
                                    "path": {"type": "string", "description": "Compatibility alias for target_file."},
                                    "filename": {"type": "string", "description": "Compatibility alias for target_file."},
                                    "content": {"type": "string", "description": "Full file content or inserted text."},
                                    "text": {"type": "string", "description": "Alias for content."},
                                    "insert": {"type": "string", "description": "Alias for inserted content."},
                                    "find": {"type": "string", "description": "Exact text to find."},
                                    "old_text": {"type": "string", "description": "Alias for find."},
                                    "replace": {"type": "string", "description": "Replacement text."},
                                    "replacement": {"type": "string", "description": "Alias for replace."},
                                    "new_text": {"type": "string", "description": "Alias for replace."},
                                    "marker": {"type": "string", "description": "Insertion marker."},
                                    "occurrence": {
                                        "anyOf": [
                                            {"type": "string", "enum": ["first", "last", "all"]},
                                            {"type": "integer", "minimum": 1},
                                        ],
                                        "description": "Which match to edit.",
                                    },
                                    "create_if_missing": {"type": "boolean", "description": "Allow creating missing target where meaningful."},
                                    "ensure_trailing_newline": {"type": "boolean", "description": "For write, ensure trailing newline."},
                                },
                                "required": ["operation"],
                                "additionalProperties": False,
                            },
                        },
                        "brief": {
                            "type": "string",
                            "description": "Optional short human-facing edit purpose shown in the terminal receipt, such as 编辑配置 or 修复渲染.",
                        },
                        "allow_fuzzy": {
                            "type": "boolean",
                            "description": "Allow fuzzy fallback when the patch context moved slightly. Defaults to true.",
                        },
                        "cwd": {
                            "type": "string",
                            "description": "Optional working directory. Defaults to current cwd.",
                        },
                        "strip": {
                            "type": "integer",
                            "minimum": 0,
                            "maximum": 5,
                            "description": "git apply -p strip level. Defaults to 1.",
                        },
                        "check_only": {
                            "type": "boolean",
                            "description": "Only validate the patch without applying it.",
                        },
                    },
                    "anyOf": [
                        {"required": ["operation"]},
                        {"required": ["edits"]},
                        {"required": ["patch"]},
                        {"required": ["diff"]},
                        {"required": ["patch_text"]},
                        {"required": ["content"]},
                        {"required": ["text"]},
                    ],
                    "additionalProperties": False,
                },
                handler=_execute_apply_patch_tool,
            )
            self._tools["web_search"] = ToolDefinition(
                name="web_search",
                description=(
                    "Search the web through the configured Volcengine/Feedcoop WebSearch API. "
                    "Use mode=summary when the user needs a fast synthesized answer with sources, "
                    "mode=web when raw result lists are better, and mode=auto to prefer summary when "
                    "available. Results are bounded for token control."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "Search query.",
                        },
                        "brief": {
                            "type": "string",
                            "description": "Short human-facing search purpose shown in the terminal receipt, such as 搜索关于API打压线索.",
                        },
                        "mode": {
                            "type": "string",
                            "enum": ["auto", "summary", "web"],
                            "description": "Search mode. summary uses web_summary; web returns normal web results; auto prefers summary key.",
                        },
                        "count": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": MAX_WEB_SEARCH_RESULTS,
                            "description": "Maximum number of results. Defaults to 5.",
                        },
                        "max_results": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": MAX_WEB_SEARCH_RESULTS,
                            "description": "Backward-compatible alias for count.",
                        },
                        "need_content": {
                            "type": "boolean",
                            "description": "Whether to request page content. Defaults to false to save tokens.",
                        },
                        "sites": {
                            "type": "string",
                            "description": "Optional comma-separated site filter.",
                        },
                        "block_hosts": {
                            "type": "string",
                            "description": "Optional comma-separated blocked hosts.",
                        },
                        "query_rewrite": {
                            "type": "boolean",
                            "description": "Allow server-side query rewrite. Defaults to false.",
                        },
                    },
                    "required": ["query", "brief"],
                    "additionalProperties": False,
                },
                handler=_execute_web_search_tool,
            )
            self._tools["context"] = ToolDefinition(
                name="context",
                description=(
                    "Set the smart context budget for the next model request. "
                    "Use a percent from 0 to 100 to control how much of the shared entries context is "
                    "returned next round: lower values save tokens for simple checks, higher values restore more "
                    "memory for cross-file or long-running work. Low visibility is not memory loss: hidden context "
                    "remains stored and can be restored later with a higher percent, including 100. This does not "
                    "rewrite memory; contextmanage handles entry-id replacement and folding."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "percent": {
                            "type": "integer",
                            "minimum": 0,
                            "maximum": 100,
                            "description": "Next-round context visibility budget. 0 means omit the shared entries context for that round; 100 restores full configured excerpts. Lower values do not delete or forget memory.",
                        },
                        "level": {
                            "type": "string",
                            "enum": ["tiny", "small", "medium", "large", "full"],
                            "description": "Optional shorthand when percent is omitted: tiny=12, small=33, medium=66, large=85, full=100.",
                        },
                        "turns": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": 5,
                            "description": "How many upcoming model requests should use this budget. Defaults to 1.",
                        },
                        "brief": {
                            "type": "string",
                            "description": "Short human-facing purpose shown in the receipt, such as 降低轮询上下文 or 恢复全量上下文.",
                        },
                        "reason": {
                            "type": "string",
                            "description": "Optional short reason for the budget choice.",
                        },
                    },
                    "anyOf": [
                        {"required": ["percent"]},
                        {"required": ["level"]},
                    ],
                    "required": ["brief"],
                    "additionalProperties": False,
                },
                handler=_execute_context_tool,
            )
            self._tools["context_manage"] = ToolDefinition(
                name="context_manage",
                description=(
                    "Manage projectling's shared entries context when the system says context is large. "
                    "mode=status/list inspects entries, mode=replace replaces an entry id or id range with a compact summary, "
                    "mode=fold folds old tool entries. Legacy mode=full/half/fold_tools is rejected because persona txt "
                    "is no longer the active context store. "
                    "Use target=both by default. target=role or target=persona points at the active persona board; "
                    "legacy shared/public/dualstar aliases are accepted for compatibility but no longer represent "
                    "separate shared memory. target=liaison can still be used only when a separate liaison role path "
                    "is explicitly available in the current session."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "mode": {
                            "type": "string",
                            "enum": ["status", "list", "replace", "fold"],
                            "description": "Entries context governance mode.",
                        },
                        "target": {
                            "type": "string",
                            "enum": ["both", "all", "shared", "role", "dualstar", "pair", "link", "liaison", "fastmemory", "public", "persona"],
                            "description": "Context target. Defaults to both. role/persona selects the active persona board; legacy shared/public/fastmemory aliases map to the active role board; liaison is only meaningful when a separate liaison role is available.",
                        },
                        "section": {
                            "type": "string",
                            "enum": ["public", "shared", "role", "persona", "dualstar", "pair", "link", "liaison"],
                            "description": "Optional section selector. role/persona chooses the active persona board; shared/public/fastmemory resolve to the same active role board; liaison is only meaningful when a separate liaison role is available.",
                        },
                        "brief": {
                            "type": "string",
                            "description": "Short human-facing purpose shown in the receipt.",
                        },
                        "summary": {
                            "type": "string",
                            "description": "Compact durable summary for mode=replace. Preserve paths, decisions, user preferences, unresolved tasks, and tool conclusions.",
                        },
                        "id": {
                            "type": "string",
                            "description": "Single entry id for mode=replace, such as E000123.",
                        },
                        "entry_id": {
                            "type": "string",
                            "description": "Alias for id.",
                        },
                        "start_id": {
                            "type": "string",
                            "description": "Start entry id for mode=replace.",
                        },
                        "end_id": {
                            "type": "string",
                            "description": "End entry id for mode=replace.",
                        },
                        "id_range": {
                            "type": "string",
                            "description": "Entry id range for mode=replace, such as E000010~E000022.",
                        },
                        "limit": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": 200,
                            "description": "Entry count for mode=list.",
                        },
                        "keep_last": {
                            "type": "integer",
                            "minimum": 0,
                            "maximum": 50,
                            "description": "For mode=fold, keep this many recent tool entries visible.",
                        },
                        "include_hidden": {
                            "type": "boolean",
                            "description": "For mode=list, include entries already replaced by summaries.",
                        },
                        "shared_summary": {
                            "type": "string",
                            "description": "Compatibility alias for the shared entries context.",
                        },
                        "role_summary": {
                            "type": "string",
                            "description": "Optional compact summary specifically for the active persona context.",
                        },
                        "dualstar_summary": {
                            "type": "string",
                            "description": "Compatibility alias for legacy dualstar-style context.",
                        },
                        "liaison_summary": {
                            "type": "string",
                            "description": "Optional compact summary specifically for the liaison role context when a separate liaison path is available.",
                        },
                    },
                    "required": ["mode"],
                    "additionalProperties": False,
                },
                handler=_execute_context_manage_tool,
            )
            self._tools["contextmanage"] = ToolDefinition(
                name="contextmanage",
                description=(
                    "New ProjectLing context governance entry. Use it for entry-id status/list/replace/fold flows; "
                    "legacy full/half/fold_tools compaction is rejected."
                ),
                input_schema=self._tools["context_manage"].input_schema,
                handler=_execute_contextmanage_tool,
            )
            self._tools["memory_add"] = ToolDefinition(
                name="memory_add",
                description=(
                    "Write or append a permanent diary memory into projectling/memory/memory.db. "
                    "Use this when datememory.json exceeds the threshold or when the user asks to store "
                    "long-term memory. Diary must be dated YYYY-MM-DD and include at least five keywords."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "date": {
                            "type": "string",
                            "description": "Exact day in YYYY-MM-DD format.",
                        },
                        "diary": {
                            "type": "string",
                            "description": "Diary-style summary of what happened, user preferences, project progress, unresolved work, and conclusions.",
                        },
                        "keywords": {
                            "type": "array",
                            "items": {"type": "string"},
                            "minItems": 5,
                            "description": "At least five keywords for later retrieval.",
                        },
                        "mode": {
                            "type": "string",
                            "enum": ["append", "replace"],
                            "description": "Append to the same date by default.",
                        },
                        "consume_source": {
                            "type": "boolean",
                            "description": "When true, clear datememory.json after a successful write.",
                        },
                        "brief": {
                            "type": "string",
                            "description": "Short human-facing purpose shown in the receipt.",
                        },
                    },
                    "required": ["date", "diary", "keywords", "brief"],
                    "additionalProperties": False,
                },
                handler=_execute_memory_add_tool,
            )
            self._tools["memory_check"] = ToolDefinition(
                name="memory_check",
                description=(
                    "Search permanent diary memory by at least five keywords. If the best hit rate is "
                    "80% or higher, returns the best diary detail; otherwise returns only date summaries."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "keywords": {
                            "type": "array",
                            "items": {"type": "string"},
                            "minItems": 5,
                            "description": "At least five keywords. Fewer keywords are rejected.",
                        },
                        "limit": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": 8,
                            "description": "Maximum result count. Defaults to 5.",
                        },
                        "brief": {
                            "type": "string",
                            "description": "Short human-facing purpose shown in the receipt.",
                        },
                    },
                    "required": ["keywords", "brief"],
                    "additionalProperties": False,
                },
                handler=_execute_memory_check_tool,
            )
            self._tools["memorycheak"] = ToolDefinition(
                name="memorycheak",
                description="Backward-compatible alias for memory_check.",
                input_schema=self._tools["memory_check"].input_schema,
                handler=_execute_memory_check_tool,
            )
            self._tools["memory_read"] = ToolDefinition(
                name="memory_read",
                description=(
                    "Read permanent diary memory by exact dates only. Dates must be YYYY-MM-DD; "
                    "do not use fuzzy ranges or months."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "dates": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Exact dates in YYYY-MM-DD format.",
                        },
                        "brief": {
                            "type": "string",
                            "description": "Short human-facing purpose shown in the receipt.",
                        },
                    },
                    "required": ["dates", "brief"],
                    "additionalProperties": False,
                },
                handler=_execute_memory_read_tool,
            )
            self._tools["memory_status"] = ToolDefinition(
                name="memory_status",
                description="Inspect projectling permanent memory paths, datememory size, and SQLite diary counts.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "action": {
                            "type": "string",
                            "enum": ["status", "clear_datememory"],
                            "description": "Use clear_datememory only after memory_add succeeded.",
                        },
                        "brief": {
                            "type": "string",
                            "description": "Short human-facing purpose shown in the receipt.",
                        },
                    },
                    "required": ["action", "brief"],
                    "additionalProperties": False,
                },
                handler=_execute_memory_status_tool,
            )
            self._tools["tool_manage"] = ToolDefinition(
                name="tool_manage",
                description=(
                    "Inspect and manage the tool box layer. "
                    "Use action=list to view summaries and visibility state, action=inspect to reveal selected tool details, "
                    "action=expand/collapse to change a tool's schema visibility, and action=reset to restore all tools. "
                    "tool_manage itself stays pinned and always visible."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "action": {
                            "type": "string",
                            "enum": ["list", "inspect", "expand", "collapse", "expand_all", "collapse_all", "reset"],
                            "description": "Tool box action. Defaults to list.",
                        },
                        "tool": {
                            "type": "string",
                            "description": "Compatibility alias for one tool name.",
                        },
                        "tools": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Tool names to inspect, expand, or collapse.",
                        },
                        "include_schema": {
                            "type": "boolean",
                            "description": "Whether inspect output includes detailed schema text. Defaults to true.",
                        },
                        "brief": {
                            "type": "string",
                            "description": "Short human-facing purpose shown in the receipt.",
                        },
                    },
                    "additionalProperties": False,
                },
                handler=_execute_tool_manage_tool,
            )
        if include_compact:
            self._tools["compact_context"] = ToolDefinition(
                name="compact_context",
                description=(
                    "Compact the current role's external persona context into a durable summary. "
                    "Use only when explicitly asked by the system during context maintenance."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "summary": {
                            "type": "string",
                            "description": "A compact but detailed memory summary to persist.",
                        },
                        "preserved_details": {
                            "type": "string",
                            "description": "Optional important names, paths, decisions, preferences, and unresolved tasks.",
                        },
                    },
                    "required": ["summary"],
                    "additionalProperties": False,
                },
                handler=_execute_compact_context_tool,
            )
        self.toolbox = ToolBox(config, self._tools)

    def register(self, tool: ToolDefinition) -> None:
        self._tools[tool.name] = tool
        self.toolbox.sync_defaults(self._tools)

    def schemas(self, *, include_hidden: bool = False) -> list[dict[str, Any]]:
        self.toolbox.reload(self._tools)
        schemas: list[dict[str, Any]] = []
        priority = {"link": 0, "update_plan": 1, "model_mode": 2, "contextmanage": 3, "persona_link": 4, "context_manage": 5}
        ordered_names = sorted(self._tools, key=lambda name: (priority.get(name, 10), name))
        for name in ordered_names:
            tool = self._tools[name]
            if not include_hidden and not self.toolbox.is_expanded(name):
                continue
            schemas.append(tool.schema())
        return schemas

    def execute_tool_call(self, tool_call: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        function = tool_call.get("function") or {}
        name = str(function.get("name") or "")
        call_id = str(tool_call.get("id") or "")
        self.toolbox.reload(self._tools)
        tool_context = ToolContext(
            cwd=context.cwd,
            home=context.home,
            config=context.config,
            event_callback=context.event_callback,
            active_role=context.active_role,
            active_liaison=context.active_liaison,
            execution_role=context.execution_role,
            persona_path=context.persona_path,
            liaison_path=context.liaison_path,
            dualstar_path=context.dualstar_path,
            toolbox=self.toolbox,
        )

        try:
            raw_arguments = function.get("arguments") or "{}"
            arguments = raw_arguments if isinstance(raw_arguments, dict) else json.loads(raw_arguments)
        except json.JSONDecodeError as exc:
            if name == "apply_patch" and isinstance(raw_arguments, str) and _looks_like_patch(raw_arguments):
                arguments = {"patch": raw_arguments, "brief": "应用补丁"}
            else:
                payload = {
                    "status": "error",
                    "tool": name or "unknown",
                    "message": f"工具参数不是合法 JSON: {exc}",
                }
                return {
                    "role": "tool",
                    "tool_call_id": call_id,
                    "name": name or "unknown",
                    "content": json.dumps(payload, ensure_ascii=False),
                }

        if not isinstance(arguments, dict):
            payload = {
                "status": "error",
                "tool": name or "unknown",
                "message": "工具参数必须是 JSON object。",
            }
            return {
                "role": "tool",
                "tool_call_id": call_id,
                "name": name or "unknown",
                "content": json.dumps(payload, ensure_ascii=False),
            }

        tool = self._tools.get(name)
        if tool is None:
            known = ", ".join(sorted(self._tools)) or "none"
            payload = {
                "status": "error",
                "tool": name or "unknown",
                "message": f"未知工具。当前支持：{known}。",
            }
            return {
                "role": "tool",
                "tool_call_id": call_id,
                "name": name or "unknown",
                "content": json.dumps(payload, ensure_ascii=False),
            }

        try:
            result = tool.handler(arguments, tool_context)
        except Exception as exc:
            payload = {
                "status": "error",
                "tool": tool.name,
                "brief": str(arguments.get("brief") or "").strip() if isinstance(arguments, dict) else "",
                "message": str(exc),
            }
        else:
            actor_payload = _tool_actor_payload(tool_context)
            if actor_payload and isinstance(result, dict):
                result = dict(result)
                for actor_key, actor_value in actor_payload.items():
                    result.setdefault(actor_key, actor_value)
            if isinstance(result, dict) and isinstance(arguments, dict) and arguments.get("brief") and not result.get("brief"):
                result = dict(result)
                result["brief"] = str(arguments.get("brief") or "").strip()
            inline_budget = None if tool.name == "context" else _extract_inline_context_budget(arguments)
            if inline_budget is not None and isinstance(result, dict):
                percent, level, turns_remaining = inline_budget
                result = dict(result)
                result.update(
                    {
                        "context_budget_percent": percent,
                        "context_budget_level": level,
                        "context_budget_bar": _context_budget_bar(percent),
                        "context_budget_text": f"{_context_budget_bar(percent)} ≈{percent}%",
                        "context_budget_turns": turns_remaining,
                    }
                )
                save_context_budget(
                    context.config,
                    percent=percent,
                    level=level,
                    turns_remaining=turns_remaining,
                    reason=str(arguments.get("reason") or "").strip(),
                    brief=str(arguments.get("brief") or "").strip() or tool.name,
                    message=(
                        f"已从 {tool.name} 工具调用中继承上下文可见度约 {percent}%"
                        + (f" ×{turns_remaining}" if turns_remaining > 1 else "")
                        + "。"
                    ),
                )
            payload = _compact_tool_result_for_model(result)

        return {
            "role": "tool",
            "tool_call_id": call_id,
            "name": tool.name,
            "content": json.dumps(payload, ensure_ascii=False),
        }


__all__ = [
    "DEFAULT_MEMORY_MAX_BYTES",
    "ToolContext",
    "ToolDefinition",
    "ToolRegistry",
    "append_chat_turns",
    "append_context_entry",
    "clear_context_entries",
    "clear_datememory_payload",
    "consume_context_budget",
    "confirm_pending_command",
    "context_entries_path_for_config",
    "context_entries_status",
    "datememory_path_for_config",
    "ensure_memory_layout",
    "load_context_budget",
    "load_context_entries",
    "load_datememory_payload",
    "memory_add_record",
    "memory_db_path_for_config",
    "memory_dir_for_config",
    "memory_max_bytes_for_config",
    "memory_pressure_message",
    "memory_status",
    "reject_pending_command",
    "render_context_entries_text",
    "render_datememory_text",
    "show_pending_command",
    "save_context_budget",
]
