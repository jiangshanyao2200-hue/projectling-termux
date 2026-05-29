from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
from typing import Any
import unicodedata


HOME = Path(os.environ.get("HOME", "/data/data/com.termux/files/home"))
AITERMUX_HOME = Path(os.environ.get("AITERMUX_HOME", str(HOME / "AItermux"))).expanduser()
AIDEBUG_DIR = Path(os.environ.get("AITERMUX_AIDEBUG_DIR", str(AITERMUX_HOME / "projectling" / "aidebug"))).expanduser()
PROJECTLING_DIR = AITERMUX_HOME / "projectling"
PROJECTLING_RUN = PROJECTLING_DIR / "run.sh"
LOG_DIR = AIDEBUG_DIR / "logs"
NOTE_DIR = AIDEBUG_DIR / "notes"
STATE_DIR = AIDEBUG_DIR / "state" / "projectling-auto"
ROUND_DIR = STATE_DIR / "rounds"
STATE_DIR.mkdir(parents=True, exist_ok=True)
ROUND_DIR.mkdir(parents=True, exist_ok=True)
LOG_DIR.mkdir(parents=True, exist_ok=True)
NOTE_DIR.mkdir(parents=True, exist_ok=True)
AUTO_SESSION_PREFIX = "aidebug-auto-"
ANSI_RE = re.compile(r"\x1b\[[0-9;:]*[A-Za-z]")
NOTE_PATH = NOTE_DIR / "projectling-auto.md"
ISSUES_PATH = LOG_DIR / "projectling-auto-issues.jsonl"

sys.path.insert(0, str(PROJECTLING_DIR))

from projectling import ProjectLingEngine, load_config, persona_path_for_role  # noqa: E402
from tooling import ToolContext, ToolRegistry  # noqa: E402


def timestamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def write_log(component: str, message: str) -> None:
    line = f"{timestamp()} {component} {message}\n"
    with (LOG_DIR / "projectling-auto.log").open("a", encoding="utf-8") as handle:
        handle.write(line)


def compact_round_payload(payload: dict[str, Any], detail_path: Path) -> dict[str, Any]:
    def nested(source: dict[str, Any], *keys: str) -> Any:
        current: Any = source
        for key in keys:
            if not isinstance(current, dict):
                return None
            current = current.get(key)
        return current

    terminal = payload.get("terminal_smoke") if isinstance(payload.get("terminal_smoke"), dict) else {}
    terminal_info = terminal.get("info") if isinstance(terminal.get("info"), dict) else {}
    web = payload.get("web_smoke") if isinstance(payload.get("web_smoke"), dict) else None
    web_result = web.get("result") if isinstance(web, dict) and isinstance(web.get("result"), dict) else {}
    live = payload.get("live_chat_smoke") if isinstance(payload.get("live_chat_smoke"), dict) else None
    return {
        "round": payload.get("round"),
        "started_at": payload.get("started_at"),
        "ok": bool(payload.get("ok")),
        "elapsed_seconds": payload.get("elapsed_seconds"),
        "detail_path": str(detail_path),
        "findings": payload.get("findings") or [],
        "doctor_rc": payload.get("doctor_rc"),
        "tools": nested(payload, "schema_check", "names"),
        "ui": {
            "ok": nested(payload, "ui_smoke", "ok"),
            "touching_lines": nested(payload, "ui_smoke", "touching_lines"),
            "too_wide_lines": nested(payload, "ui_smoke", "too_wide_lines"),
        },
        "command": {
            "ok": nested(payload, "command_smoke", "ok"),
            "stdout_chars": nested(payload, "command_smoke", "stdout_chars"),
            "has_head": nested(payload, "command_smoke", "has_head"),
            "has_tail": nested(payload, "command_smoke", "has_tail"),
            "safety_ok": nested(payload, "command_safety", "ok"),
        },
        "apply_patch": {
            "ok": nested(payload, "patch_smoke", "ok"),
            "security_ok": nested(payload, "patch_security", "ok"),
        },
        "terminal": {
            "ok": terminal.get("ok"),
            "session_name": terminal.get("session_name"),
            "log_path": terminal.get("log_path"),
            "log_lines": terminal_info.get("log_lines"),
            "log_bytes": terminal_info.get("log_bytes"),
            "has_start": terminal.get("log_has_start"),
            "has_send": terminal.get("log_has_send"),
        },
        "aidebug": {
            "slice_ok": nested(payload, "aidebug_slice_smoke", "ok"),
            "security_ok": nested(payload, "aidebug_security", "ok"),
        },
        "compact_context": {
            "ok": nested(payload, "compact_smoke", "ok"),
            "chars": nested(payload, "compact_smoke", "chars"),
        },
        "web_search": {
            "ok": web.get("ok") if isinstance(web, dict) else None,
            "validation_ok": nested(payload, "web_validation", "ok"),
            "result_count": web_result.get("result_count"),
        },
        "live_chat": None
        if live is None
        else {
            "ok": live.get("ok"),
            "rounds": live.get("rounds"),
            "tool_names": live.get("tool_names"),
            "usage": live.get("usage"),
            "context_restored": live.get("context_restored"),
        },
    }


def log_json(payload: dict[str, Any]) -> None:
    safe_started = re.sub(r"[^0-9A-Za-z_-]+", "-", str(payload.get("started_at") or timestamp())).strip("-")
    detail_path = ROUND_DIR / f"{safe_started}-round-{payload.get('round', 'unknown')}.json"
    tmp = detail_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(detail_path)
    payload["detail_path"] = str(detail_path)
    compact = compact_round_payload(payload, detail_path)
    with (LOG_DIR / "projectling-auto.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(compact, ensure_ascii=False) + "\n")


def log_issue(payload: dict[str, Any]) -> None:
    with ISSUES_PATH.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False) + "\n")


def run_cmd(command: list[str], *, cwd: Path, timeout: int = 60) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["AITERMUX_HOME"] = str(AITERMUX_HOME)
    env["AITERMUX_AIDEBUG_DIR"] = str(AIDEBUG_DIR)
    return subprocess.run(
        command,
        cwd=str(cwd),
        env=env,
        text=True,
        capture_output=True,
        timeout=timeout,
    )


def display_width(text: str) -> int:
    width = 0
    for char in text:
        if unicodedata.combining(char):
            continue
        width += 2 if unicodedata.east_asian_width(char) in {"W", "F"} else 1
    return width


def cleanup_stale_auto_sessions() -> list[str]:
    if not shutil.which("tmux"):
        return []
    completed = subprocess.run(
        ["tmux", "list-sessions", "-F", "#{session_name}"],
        text=True,
        capture_output=True,
    )
    if completed.returncode != 0:
        return []
    killed: list[str] = []
    for raw_name in completed.stdout.splitlines():
        name = raw_name.strip()
        if not name.startswith(AUTO_SESSION_PREFIX):
            continue
        subprocess.run(["tmux", "kill-session", "-t", name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        killed.append(name)
    if killed:
        write_log("projectling-auto", f"cleanup killed_sessions={','.join(killed)}")
    return killed


def tool_call(registry: ToolRegistry, ctx: ToolContext, name: str, args: dict[str, Any]) -> dict[str, Any]:
    call = {
        "id": f"auto-{name}-{time.time_ns()}",
        "function": {
            "name": name,
            "arguments": json.dumps(args, ensure_ascii=False),
        },
    }
    result = registry.execute_tool_call(call, ctx)
    return json.loads(result["content"])


def smoke_projectling_ui() -> dict[str, Any]:
    rendered = projectling_cli(
        "render-motd-card",
        "--width",
        "69",
        "--max-lines",
        "12",
        "--settings-label",
        "",
        timeout=40,
    )
    snapshot = STATE_DIR / "motd-card.txt"
    snapshot.write_text(rendered.stdout, encoding="utf-8")
    plain = ANSI_RE.sub("", rendered.stdout)
    lines = plain.splitlines()
    touching_lines = [index + 1 for index, line in enumerate(lines) if line.strip() and not line.startswith("  ")]
    too_wide = [index + 1 for index, line in enumerate(lines) if display_width(line) > 80]
    return {
        "tool": "projectling-ui",
        "returncode": rendered.returncode,
        "snapshot": str(snapshot),
        "lines": len(lines),
        "touching_lines": touching_lines[:10],
        "too_wide_lines": too_wide[:10],
        "stdout_tail": rendered.stdout[-1200:],
        "stderr_tail": rendered.stderr[-800:],
        "ok": rendered.returncode == 0 and bool(lines) and not touching_lines and not too_wide,
    }


def smoke_command_receipt(registry: ToolRegistry, ctx: ToolContext) -> dict[str, Any]:
    command = (
        "python3 -c \"for i in range(2200): "
        "print(f'COMMAND_STRESS_{i:04d}_' + 'x'*40)\""
    )
    result = tool_call(
        registry,
        ctx,
        "command",
        {
            "command": command,
            "timeout_seconds": 20,
        },
    )
    stdout = str(result.get("stdout") or "")
    return {
        "tool": "command",
        "result": result,
        "stdout_chars": len(stdout),
        "has_head": "COMMAND_STRESS_0000" in stdout,
        "has_tail": "COMMAND_STRESS_2199" in stdout,
        "ok": (
            result.get("status") == "ok"
            and result.get("returncode") == 0
            and bool(result.get("stdout_truncated_for_model"))
            and len(stdout) <= 12500
            and "COMMAND_STRESS_0000" in stdout
            and "COMMAND_STRESS_2199" in stdout
        ),
    }


def smoke_command_safety(registry: ToolRegistry, ctx: ToolContext) -> dict[str, Any]:
    cases = [
        ("blocked_rm_root", "rm -rf /", {"blocked"}),
        ("blocked_python_repl", "python3", {"blocked"}),
        ("blocked_adb_shell_repl", "adb shell", {"blocked"}),
    ]
    results: list[dict[str, Any]] = []
    ok = True
    for label, command, expected in cases:
        result = tool_call(
            registry,
            ctx,
            "command",
            {
                "command": command,
                "timeout_seconds": 5,
            },
        )
        status = str(result.get("status") or "")
        case_ok = status in expected
        ok = ok and case_ok
        results.append(
            {
                "label": label,
                "command": command,
                "status": status,
                "risk": str(result.get("risk") or ""),
                "reason": str(result.get("reason") or ""),
                "ok": case_ok,
            }
        )
    return {"tool": "command", "kind": "safety", "cases": results, "ok": ok}


def smoke_apply_patch(registry: ToolRegistry, ctx: ToolContext) -> dict[str, Any]:
    scratch = STATE_DIR / "apply-patch-smoke"
    if scratch.exists():
        shutil.rmtree(scratch, ignore_errors=True)
    scratch.mkdir(parents=True, exist_ok=True)
    run_cmd(["git", "init", "-q"], cwd=scratch, timeout=20)
    run_cmd(["git", "config", "user.email", "aidebug@example.com"], cwd=scratch, timeout=20)
    run_cmd(["git", "config", "user.name", "aidebug"], cwd=scratch, timeout=20)
    (scratch / "sample.txt").write_text("hello\n", encoding="utf-8")
    patch = """diff --git a/sample.txt b/sample.txt
index 1111111..2222222 100644
--- a/sample.txt
+++ b/sample.txt
@@ -1 +1,2 @@
 hello
+world
"""
    result = tool_call(
        registry,
        ctx,
        "apply_patch",
        {
            "cwd": str(scratch),
            "patch": patch,
            "strip": 1,
        },
    )
    content = (scratch / "sample.txt").read_text(encoding="utf-8", errors="replace")
    return {
        "tool": "apply_patch",
        "result": result,
        "content": content,
        "ok": result.get("status") == "ok" and content == "hello\nworld\n",
    }


def smoke_apply_patch_security(registry: ToolRegistry, ctx: ToolContext) -> dict[str, Any]:
    scratch = STATE_DIR / "apply-patch-security"
    if scratch.exists():
        shutil.rmtree(scratch, ignore_errors=True)
    scratch.mkdir(parents=True, exist_ok=True)
    run_cmd(["git", "init", "-q"], cwd=scratch, timeout=20)
    patch = """diff --git a/../escape.txt b/../escape.txt
new file mode 100644
index 0000000..2222222
--- /dev/null
+++ b/../escape.txt
@@ -0,0 +1 @@
+escape
"""
    result = tool_call(
        registry,
        ctx,
        "apply_patch",
        {
            "cwd": str(scratch),
            "patch": patch,
            "strip": 1,
        },
    )
    escape_path = scratch.parent / "escape.txt"
    return {
        "tool": "apply_patch",
        "kind": "security",
        "result": result,
        "escape_path": str(escape_path),
        "escape_written": escape_path.exists(),
        "ok": result.get("status") == "blocked" and not escape_path.exists(),
    }


def smoke_web_search(registry: ToolRegistry, ctx: ToolContext, query: str) -> dict[str, Any] | None:
    query = query.strip()
    if not query:
        return None
    result = tool_call(
        registry,
        ctx,
        "web_search",
        {
            "query": query,
            "max_results": 3,
        },
    )
    return {
        "tool": "web_search",
        "result": result,
        "ok": result.get("status") in {"ok", "empty"},
    }


def smoke_web_search_validation(registry: ToolRegistry, ctx: ToolContext) -> dict[str, Any]:
    result = tool_call(registry, ctx, "web_search", {"query": "", "max_results": 3})
    return {
        "tool": "web_search",
        "kind": "validation",
        "result": result,
        "ok": result.get("status") == "error",
    }


def smoke_terminal(registry: ToolRegistry, ctx: ToolContext, round_id: int) -> dict[str, Any]:
    session_name = f"{AUTO_SESSION_PREFIX}{os.getpid()}-{round_id}"
    start = tool_call(
        registry,
        ctx,
        "terminal",
        {
            "action": "start",
            "session_name": session_name,
            "cwd": str(PROJECTLING_DIR),
        },
    )
    time.sleep(1.0)
    start_send = tool_call(
        registry,
        ctx,
        "terminal",
        {
            "action": "send",
            "session_name": session_name,
            "command": f"printf 'AIDEBUG_ROUND_{round_id}_START\\n'",
        },
    )
    time.sleep(0.8)
    send = tool_call(
        registry,
        ctx,
        "terminal",
        {
            "action": "send",
            "session_name": session_name,
            "command": f"printf 'AIDEBUG_ROUND_{round_id}_SEND\\n'",
        },
    )
    time.sleep(0.8)
    info = tool_call(
        registry,
        ctx,
        "terminal",
        {
            "action": "info",
            "session_name": session_name,
        },
    )
    close = tool_call(registry, ctx, "terminal", {"action": "close", "session_name": session_name})
    log_path = Path(str(info.get("log_path") or start.get("log_path") or ""))
    log_text = log_path.read_text(encoding="utf-8", errors="replace") if log_path.is_file() else ""
    return {
        "tool": "terminal",
        "session_name": session_name,
        "start": start,
        "start_send": start_send,
        "send": send,
        "info": info,
        "close": close,
        "log_path": str(log_path),
        "log_has_start": f"AIDEBUG_ROUND_{round_id}_START" in log_text,
        "log_has_send": f"AIDEBUG_ROUND_{round_id}_SEND" in log_text,
        "ok": (
            close.get("status") == "ok"
            and f"AIDEBUG_ROUND_{round_id}_START" in log_text
            and f"AIDEBUG_ROUND_{round_id}_SEND" in log_text
        ),
    }


def smoke_aidebug_read_precision(
    registry: ToolRegistry,
    ctx: ToolContext,
    terminal_smoke: dict[str, Any],
    round_id: int,
) -> dict[str, Any]:
    log_path = Path(str(terminal_smoke.get("log_path") or ""))
    try:
        relative = str(log_path.resolve().relative_to(AIDEBUG_DIR.resolve()))
    except ValueError:
        relative = ""
    info = terminal_smoke.get("info") if isinstance(terminal_smoke.get("info"), dict) else {}
    try:
        total_lines = max(1, int(info.get("log_lines") or 1))
    except (TypeError, ValueError):
        total_lines = 1
    start_line = max(1, total_lines - 12)
    if not relative:
        return {
            "tool": "aidebug",
            "action": "read-slice",
            "status": "error",
            "message": "terminal log path is outside aidebug",
            "ok": False,
        }
    result = tool_call(
        registry,
        ctx,
        "aidebug",
        {
            "action": "read",
            "path": relative,
            "mode": "slice",
            "start_line": start_line,
            "end_line": total_lines,
        },
    )
    stdout = str(result.get("stdout") or "")
    return {
        "tool": "aidebug",
        "action": "read-slice",
        "relative_path": relative,
        "start_line": start_line,
        "end_line": total_lines,
        "result": result,
        "has_send": f"AIDEBUG_ROUND_{round_id}_SEND" in stdout,
        "ok": result.get("status") == "ok" and f"AIDEBUG_ROUND_{round_id}_SEND" in stdout,
    }


def smoke_aidebug_security(registry: ToolRegistry, ctx: ToolContext) -> dict[str, Any]:
    result = tool_call(
        registry,
        ctx,
        "aidebug",
        {
            "action": "read",
            "path": "../projectling/config/env",
            "mode": "tail",
            "lines": 5,
        },
    )
    return {
        "tool": "aidebug",
        "kind": "security",
        "result": result,
        "ok": result.get("status") == "blocked",
    }


def smoke_context_compact(ctx: ToolContext) -> dict[str, Any]:
    compact_registry = ToolRegistry(ctx.config, include_command=False, include_compact=True)
    persona_path = STATE_DIR / "compact-context-smoke.txt"
    compact_ctx = ToolContext(cwd=ctx.cwd, home=ctx.home, config=ctx.config, persona_path=persona_path)
    summary = "projectling aidebug compact smoke\n" + ("保留：工具、路径、错误码、下一步。\n" * 2600)
    result = tool_call(
        compact_registry,
        compact_ctx,
        "compact_context",
        {
            "summary": summary,
            "preserved_details": "smoke-test=1 path=aidebug/state/projectling-auto",
        },
    )
    text = persona_path.read_text(encoding="utf-8", errors="replace") if persona_path.is_file() else ""
    target = int(getattr(ctx.config, "advisorling_compact_target_chars", 48000) or 48000)
    return {
        "tool": "compact_context",
        "result": result,
        "path": str(persona_path),
        "chars": len(text),
        "target": target,
        "ok": result.get("status") == "ok" and persona_path.is_file() and len(text) <= max(1000, target) + 1,
    }


def smoke_live_chat_tool_call(ctx: ToolContext) -> dict[str, Any]:
    engine = ProjectLingEngine(ctx.config)
    role, _seed = engine.current_role()
    persona_path = persona_path_for_role(ctx.config, role)
    role_state_path = ctx.config.runtime_dir / "role.json"
    original_persona = persona_path.read_bytes() if persona_path.is_file() else None
    original_role_state = role_state_path.read_bytes() if role_state_path.is_file() else None
    started = time.time()
    try:
        completed = projectling_cli(
            "chat",
            "--json",
            "--message",
            "请必须调用本地 command 工具执行：printf PROJECTLING_LIVE_TOOLCALL_SMOKE，然后只用一句话说明结果。",
            "--cwd",
            str(PROJECTLING_DIR),
            timeout=240,
        )
        parsed: dict[str, Any] = {}
        if completed.returncode == 0:
            try:
                parsed = json.loads(completed.stdout)
            except json.JSONDecodeError:
                parsed = {}
        tool_traces = parsed.get("tool_traces") if isinstance(parsed.get("tool_traces"), list) else []
        usage = {}
        raw_response = parsed.get("raw_response") if isinstance(parsed.get("raw_response"), dict) else {}
        if isinstance(raw_response.get("usage"), dict):
            usage = raw_response["usage"]
        command_stdout = ""
        tool_names: list[str] = []
        for trace in tool_traces:
            if not isinstance(trace, dict):
                continue
            tool_names.append(str(trace.get("name") or ""))
            result = trace.get("result") if isinstance(trace.get("result"), dict) else {}
            command_stdout += str(result.get("stdout") or "")
        prompt_details = usage.get("prompt_tokens_details") if isinstance(usage.get("prompt_tokens_details"), dict) else {}
        return {
            "tool": "live_chat",
            "returncode": completed.returncode,
            "elapsed_seconds": round(time.time() - started, 3),
            "stdout_chars": len(completed.stdout),
            "stderr_tail": completed.stderr[-1200:],
            "used_tools": bool(parsed.get("used_tools")),
            "rounds": int(parsed.get("rounds") or 0) if parsed else 0,
            "tool_names": tool_names,
            "text": str(parsed.get("text") or "")[:500] if parsed else "",
            "usage": {
                "prompt_tokens": usage.get("prompt_tokens"),
                "completion_tokens": usage.get("completion_tokens"),
                "total_tokens": usage.get("total_tokens"),
                "cached_tokens": prompt_details.get("cached_tokens", usage.get("prompt_cache_hit_tokens")),
            },
            "context_restored": True,
            "ok": (
                completed.returncode == 0
                and bool(parsed.get("used_tools"))
                and "command" in tool_names
                and "PROJECTLING_LIVE_TOOLCALL_SMOKE" in command_stdout
            ),
        }
    finally:
        persona_path.parent.mkdir(parents=True, exist_ok=True)
        if original_persona is None:
            try:
                persona_path.unlink()
            except FileNotFoundError:
                pass
        else:
            persona_path.write_bytes(original_persona)
        if original_role_state is None:
            try:
                role_state_path.unlink()
            except FileNotFoundError:
                pass
        else:
            role_state_path.write_bytes(original_role_state)


def collect_findings(payload: dict[str, Any]) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []

    def add(severity: str, component: str, message: str) -> None:
        findings.append(
            {
                "severity": severity,
                "component": component,
                "message": message,
                "round": payload.get("round"),
                "at": payload.get("started_at"),
            }
        )

    if not payload.get("ok"):
        add("error", "projectling-auto", "round failed; inspect projectling-auto.jsonl for component details")
    ui = payload.get("ui_smoke") if isinstance(payload.get("ui_smoke"), dict) else {}
    if ui.get("touching_lines"):
        add("warning", "ui", f"MOTD rendered text touches boundary: lines={ui.get('touching_lines')}")
    if ui.get("too_wide_lines"):
        add("warning", "ui", f"MOTD rendered lines exceed width budget: lines={ui.get('too_wide_lines')}")

    command = payload.get("command_smoke") if isinstance(payload.get("command_smoke"), dict) else {}
    if command and not command.get("ok"):
        add("error", "command", "large-output receipt did not preserve bounded head/tail output")
    safety = payload.get("command_safety") if isinstance(payload.get("command_safety"), dict) else {}
    if safety and not safety.get("ok"):
        add("error", "command", "command safety matrix failed")

    for key, component in (
        ("patch_smoke", "apply_patch"),
        ("patch_security", "apply_patch"),
        ("terminal_smoke", "terminal"),
        ("aidebug_slice_smoke", "aidebug"),
        ("aidebug_security", "aidebug"),
        ("compact_smoke", "compact_context"),
        ("web_validation", "web_search"),
    ):
        item = payload.get(key) if isinstance(payload.get(key), dict) else {}
        if item and not item.get("ok"):
            add("error", component, f"{key} failed")

    live = payload.get("live_chat_smoke") if isinstance(payload.get("live_chat_smoke"), dict) else None
    if live is not None:
        if not live.get("ok"):
            add("error", "live_chat", "DeepSeek live function-calling smoke failed")
        usage = live.get("usage") if isinstance(live.get("usage"), dict) else {}
        prompt_tokens = usage.get("prompt_tokens")
        if isinstance(prompt_tokens, int) and prompt_tokens > 6000:
            add("warning", "live_chat", f"prompt token cost is high: {prompt_tokens}")

    return findings


def write_round_notes(payload: dict[str, Any], findings: list[dict[str, Any]]) -> None:
    command = payload.get("command_smoke") if isinstance(payload.get("command_smoke"), dict) else {}
    terminal = payload.get("terminal_smoke") if isinstance(payload.get("terminal_smoke"), dict) else {}
    schema = payload.get("schema_check") if isinstance(payload.get("schema_check"), dict) else {}
    live = payload.get("live_chat_smoke") if isinstance(payload.get("live_chat_smoke"), dict) else None
    lines = [
        f"## {payload.get('started_at')} round={payload.get('round')} ok={int(bool(payload.get('ok')))}",
        "",
        f"- elapsed={payload.get('elapsed_seconds')}s",
        f"- detail={payload.get('detail_path')}",
        f"- tools={','.join(schema.get('names', []))}",
        f"- command_receipt_chars={command.get('stdout_chars')} head={command.get('has_head')} tail={command.get('has_tail')}",
        f"- terminal_log={terminal.get('log_path')} start={terminal.get('log_has_start')} send={terminal.get('log_has_send')}",
    ]
    if live is not None:
        usage = live.get("usage") if isinstance(live.get("usage"), dict) else {}
        lines.append(
            "- live_chat="
            f"ok={live.get('ok')} rounds={live.get('rounds')} tools={live.get('tool_names')} "
            f"prompt_tokens={usage.get('prompt_tokens')} cached={usage.get('cached_tokens')} "
            f"context_restored={live.get('context_restored')}"
        )
    if findings:
        lines.append("- findings:")
        for finding in findings:
            lines.append(f"  - [{finding['severity']}] {finding['component']}: {finding['message']}")
    else:
        lines.append("- findings: none")
    lines.append("")
    with NOTE_PATH.open("a", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")


def shlex_quote(text: str) -> str:
    return "'" + text.replace("'", "'\"'\"'") + "'"


def projectling_cli(command: str, *extra: str, timeout: int = 60) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["AITERMUX_HOME"] = str(AITERMUX_HOME)
    env["AITERMUX_AIDEBUG_DIR"] = str(AIDEBUG_DIR)
    return subprocess.run(
        [str(PROJECTLING_RUN), command, *extra],
        cwd=str(PROJECTLING_DIR),
        env=env,
        text=True,
        capture_output=True,
        timeout=timeout,
    )


def verify_tool_schema(stdout: str) -> dict[str, Any]:
    names = []
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError:
        return {"ok": False, "error": "show-tools output is not valid json"}
    for item in data:
        fn = item.get("function") or {}
        if isinstance(fn, dict):
            name = str(fn.get("name") or "")
            if name:
                names.append(name)
    required = {"command", "terminal", "aidebug", "apply_patch", "web_search"}
    missing = sorted(required - set(names))
    return {"ok": not missing, "names": names, "missing": missing}


def run_round(
    registry: ToolRegistry,
    ctx: ToolContext,
    round_id: int,
    *,
    web_query: str = "",
    live_chat_smoke: bool = False,
) -> dict[str, Any]:
    started = time.time()
    result: dict[str, Any] = {"round": round_id, "started_at": timestamp()}

    killed_sessions = cleanup_stale_auto_sessions()
    doctor = projectling_cli("doctor", timeout=40)
    ui_smoke = smoke_projectling_ui()
    tools = projectling_cli("show-tools", "--json", timeout=40)
    schema_check = verify_tool_schema(tools.stdout)
    aidebug_status = tool_call(registry, ctx, "aidebug", {"action": "status"})
    aidebug_tail = tool_call(
        registry,
        ctx,
        "aidebug",
        {"action": "read", "path": "logs/projectling.log", "mode": "tail", "lines": 20},
    )
    command_smoke = smoke_command_receipt(registry, ctx)
    command_safety = smoke_command_safety(registry, ctx)
    patch_smoke = smoke_apply_patch(registry, ctx)
    patch_security = smoke_apply_patch_security(registry, ctx)
    terminal_smoke = smoke_terminal(registry, ctx, round_id)
    aidebug_slice_smoke = smoke_aidebug_read_precision(registry, ctx, terminal_smoke, round_id)
    aidebug_security = smoke_aidebug_security(registry, ctx)
    compact_smoke = smoke_context_compact(ctx)
    web_validation = smoke_web_search_validation(registry, ctx)
    web_smoke = smoke_web_search(registry, ctx, web_query) if web_query else None
    live_smoke = smoke_live_chat_tool_call(ctx) if live_chat_smoke else None

    result.update(
        {
            "killed_sessions": killed_sessions,
            "doctor_rc": doctor.returncode,
            "doctor_stdout": doctor.stdout[-2000:],
            "doctor_stderr": doctor.stderr[-1000:],
            "ui_smoke": ui_smoke,
            "tools_rc": tools.returncode,
            "schema_check": schema_check,
            "aidebug_status": aidebug_status,
            "aidebug_tail": aidebug_tail,
            "command_smoke": command_smoke,
            "command_safety": command_safety,
            "patch_smoke": patch_smoke,
            "patch_security": patch_security,
            "terminal_smoke": terminal_smoke,
            "aidebug_slice_smoke": aidebug_slice_smoke,
            "aidebug_security": aidebug_security,
            "compact_smoke": compact_smoke,
            "web_validation": web_validation,
            "web_smoke": web_smoke,
            "live_chat_smoke": live_smoke,
            "elapsed_seconds": round(time.time() - started, 3),
        }
    )
    result["ok"] = (
        doctor.returncode == 0
        and tools.returncode == 0
        and ui_smoke.get("ok")
        and schema_check.get("ok")
        and aidebug_status.get("status") == "ok"
        and command_smoke.get("ok")
        and command_safety.get("ok")
        and patch_smoke.get("ok")
        and patch_security.get("ok")
        and terminal_smoke.get("ok")
        and aidebug_slice_smoke.get("ok")
        and aidebug_security.get("ok")
        and compact_smoke.get("ok")
        and web_validation.get("ok")
        and (web_smoke is None or web_smoke.get("ok"))
        and (live_smoke is None or live_smoke.get("ok"))
    )
    findings = collect_findings(result)
    result["findings"] = findings
    log_json(result)
    write_round_notes(result, findings)
    for finding in findings:
        log_issue(finding)
    write_log("projectling-auto", f"round={round_id} ok={int(bool(result['ok']))} elapsed={result['elapsed_seconds']}s")
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="aidebug projectling-auto")
    parser.add_argument("--rounds", type=int, default=1, help="number of rounds; 0 means run forever")
    parser.add_argument("--interval", type=float, default=0.0, help="sleep seconds between rounds")
    parser.add_argument("--web-query", default="", help="optional web search smoke query")
    parser.add_argument("--live-chat-smoke", action="store_true", help="also run a real DeepSeek function-calling smoke per round")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config = load_config()
    registry = ToolRegistry(config)
    ctx = ToolContext(cwd=PROJECTLING_DIR, home=HOME, config=config)
    write_log(
        "projectling-auto",
        f"start rounds={args.rounds} interval={args.interval} web={bool(args.web_query)} live={bool(args.live_chat_smoke)}",
    )
    cleanup_stale_auto_sessions()
    round_id = 0
    failures = 0
    try:
        while args.rounds == 0 or round_id < args.rounds:
            round_id += 1
            payload = run_round(
                registry,
                ctx,
                round_id,
                web_query=args.web_query,
                live_chat_smoke=bool(args.live_chat_smoke),
            )
            if not payload.get("ok"):
                failures += 1
                write_log("projectling-auto", f"round={round_id} failure_detected")
            if args.interval > 0 and (args.rounds == 0 or round_id < args.rounds):
                time.sleep(args.interval)
    except KeyboardInterrupt:
        write_log("projectling-auto", "interrupted")
        return 130
    write_log("projectling-auto", f"done rounds={round_id} failures={failures}")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
