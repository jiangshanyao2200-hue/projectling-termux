from __future__ import annotations

import argparse
from dataclasses import replace
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from typing import Any


HOME = Path(os.environ.get("HOME", "/data/data/com.termux/files/home"))
AITERMUX_HOME = Path(os.environ.get("AITERMUX_HOME", str(HOME / "AItermux"))).expanduser()
AIDEBUG_DIR = Path(os.environ.get("AITERMUX_AIDEBUG_DIR", str(AITERMUX_HOME / "projectling" / "aidebug"))).expanduser()
LOG_DIR = AIDEBUG_DIR / "logs"
NOTE_DIR = AIDEBUG_DIR / "notes"
PROJECTLING_DIR = AITERMUX_HOME / "projectling"
PROJECTLING_RUN = PROJECTLING_DIR / "run.sh"
HEALTH_JSON = LOG_DIR / "aidebug-health.json"
HEALTH_JSONL = LOG_DIR / "aidebug-health.jsonl"
HEALTH_MD = NOTE_DIR / "aidebug-health.md"
HEALTH_SANDBOX_DIR = AIDEBUG_DIR / "tmp" / "health-sandbox"

sys.path.insert(0, str(PROJECTLING_DIR))
try:  # pragma: no cover - fallback import guard
    from projectling import ProjectLingEngine, load_config
    from tooling import ToolContext, ToolRegistry
except Exception:  # pragma: no cover - import fallback for partial setups
    ProjectLingEngine = None  # type: ignore[assignment]
    ToolContext = None  # type: ignore[assignment]
    ToolRegistry = None  # type: ignore[assignment]
    load_config = None  # type: ignore[assignment]


def timestamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def run_cmd(command: list[str], *, cwd: Path, timeout: int = 30) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["AITERMUX_HOME"] = str(AITERMUX_HOME)
    env["AITERMUX_AIDEBUG_DIR"] = str(AIDEBUG_DIR)
    return subprocess.run(command, cwd=str(cwd), env=env, text=True, capture_output=True, timeout=timeout)


def file_meta(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"exists": False, "path": str(path)}
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
        lines = text.count("\n")
    except OSError:
        lines = 0
    stat = path.stat()
    return {
        "exists": True,
        "path": str(path),
        "bytes": stat.st_size,
        "lines": lines,
        "mtime": int(stat.st_mtime),
        "age_seconds": max(0, int(time.time() - stat.st_mtime)),
    }


def item(name: str, score: int, status: str, evidence: list[str], next_action: str = "") -> dict[str, Any]:
    return {
        "name": name,
        "score": max(0, min(100, int(score))),
        "status": status,
        "evidence": evidence,
        "next_action": next_action,
    }


def _projectling_available() -> bool:
    return all(value is not None for value in (ProjectLingEngine, ToolContext, ToolRegistry, load_config))


def _sandbox_config() -> Any | None:
    if load_config is None:
        return None
    config = load_config()
    HEALTH_SANDBOX_DIR.mkdir(parents=True, exist_ok=True)
    sandbox_runtime = HEALTH_SANDBOX_DIR / "runtime"
    sandbox_runtime.mkdir(parents=True, exist_ok=True)
    return replace(config, runtime_dir=sandbox_runtime)


def _execute_tool(name: str, arguments: dict[str, Any], *, cwd: Path | None = None) -> dict[str, Any] | None:
    if not _projectling_available():
        return None
    config = _sandbox_config()
    if config is None:
        return None
    registry = ToolRegistry(config)  # type: ignore[operator]
    tool_context = ToolContext(
        cwd=(cwd or HEALTH_SANDBOX_DIR).expanduser(),
        home=HOME,
        config=config,
    )  # type: ignore[operator]
    call = {"id": "health", "function": {"name": name, "arguments": json.dumps(arguments, ensure_ascii=False)}}
    result = registry.execute_tool_call(call, tool_context)
    try:
        return json.loads(str(result.get("content") or "{}"))
    except json.JSONDecodeError:
        return {"status": "error", "message": "tool payload not json"}


def _health_summary(payload: dict[str, Any] | None) -> str:
    if not payload:
        return "unavailable"
    summary = str(payload.get("summary") or "").strip()
    if summary:
        return summary
    message = str(payload.get("message") or "").strip()
    if message:
        return message
    return str(payload.get("status") or "unknown")


def _load_health_history(limit: int = 12) -> list[dict[str, Any]]:
    if not HEALTH_JSONL.exists():
        return []
    try:
        lines = HEALTH_JSONL.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    history: list[dict[str, Any]] = []
    for raw in lines[-max(1, int(limit)) :]:
        try:
            payload = json.loads(raw)
        except Exception:
            continue
        if isinstance(payload, dict):
            history.append(payload)
    return history


def _health_history_summary(history: list[dict[str, Any]]) -> dict[str, Any]:
    points: list[dict[str, Any]] = []
    status_counts: dict[str, int] = {}
    for payload in history:
        raw_score = payload.get("overall_score")
        try:
            score = round(float(raw_score), 1)
        except (TypeError, ValueError):
            continue
        status = str(payload.get("overall_status") or "unknown")
        status_counts[status] = status_counts.get(status, 0) + 1
        points.append(
            {
                "generated_at": str(payload.get("generated_at") or ""),
                "overall_score": score,
                "overall_status": status,
            }
        )

    recent = points[-5:]
    recent_scores = [float(point["overall_score"]) for point in recent]
    summary: dict[str, Any] = {
        "run_count": len(points),
        "recent_count": len(recent),
        "status_counts": status_counts,
        "recent": recent,
    }
    if not recent_scores:
        return summary

    latest = recent_scores[-1]
    previous = recent_scores[-2] if len(recent_scores) >= 2 else None
    delta = round(latest - previous, 1) if previous is not None else None
    if delta is None:
        trend = "insufficient"
    elif delta > 2:
        trend = "up"
    elif delta < -2:
        trend = "down"
    else:
        trend = "flat"
    summary.update(
        {
            "latest_score": latest,
            "latest_status": recent[-1].get("overall_status"),
            "latest_generated_at": recent[-1].get("generated_at"),
            "previous_score": previous,
            "delta": delta,
            "trend": trend,
            "recent_average": round(sum(recent_scores) / len(recent_scores), 1),
            "recent_min": min(recent_scores),
            "recent_max": max(recent_scores),
        }
    )
    return summary


def status_from_score(score: int) -> str:
    if score >= 85:
        return "ok"
    if score >= 60:
        return "warn"
    return "fail"


def check_layout() -> dict[str, Any]:
    required = [
        AIDEBUG_DIR,
        LOG_DIR,
        NOTE_DIR,
        AIDEBUG_DIR / "projectling" / "terminal output",
        PROJECTLING_DIR,
        PROJECTLING_RUN,
    ]
    missing = [str(path) for path in required if not path.exists()]
    score = 100 if not missing else max(20, 100 - len(missing) * 20)
    return item(
        "aidebug_layout",
        score,
        status_from_score(score),
        [f"missing={len(missing)}", *[f"missing {path}" for path in missing[:5]]],
        "创建缺失目录或检查 AITERMUX_HOME/AITERMUX_AIDEBUG_DIR。" if missing else "",
    )


def check_logs() -> dict[str, Any]:
    paths = [LOG_DIR / "startup.log", LOG_DIR / "motd.log", LOG_DIR / "zshrc.log", LOG_DIR / "projectling.log"]
    metas = [file_meta(path) for path in paths]
    missing = [meta for meta in metas if not meta.get("exists")]
    stale = [meta for meta in metas if meta.get("exists") and int(meta.get("age_seconds") or 0) > 7 * 86400]
    score = 100 - len(missing) * 25 - len(stale) * 10
    evidence = [
        f"{Path(str(meta['path'])).name}: exists={meta.get('exists')} lines={meta.get('lines', 0)} age={meta.get('age_seconds', '-')}"
        for meta in metas
    ]
    return item(
        "runtime_logs",
        score,
        status_from_score(score),
        evidence,
        "运行 motd/zshrc/projectling smoke，刷新过期或缺失日志。" if score < 85 else "",
    )


def check_projectling_doctor() -> dict[str, Any]:
    try:
        completed = run_cmd([str(PROJECTLING_RUN), "doctor"], cwd=AITERMUX_HOME, timeout=20)
    except Exception as exc:
        return item("projectling_doctor", 0, "fail", [f"exception={exc}"], "修复 run.sh 或 Python 运行环境。")
    evidence = [f"rc={completed.returncode}"]
    score = 100 if completed.returncode == 0 else 30
    if completed.returncode == 0:
        try:
            data = json.loads(completed.stdout)
        except json.JSONDecodeError:
            return item("projectling_doctor", 60, "warn", evidence + ["stdout_json=invalid"], "修复 doctor JSON 输出。")
        evidence.extend(
            [
                f"model={data.get('model')}",
                f"api_key={bool(data.get('api_key_configured'))}",
                f"tools={bool(data.get('allow_tools'))}",
                f"context={data.get('shared_context_chars')}/{data.get('role_context_chars')}",
            ]
        )
        if not data.get("api_key_configured"):
            score -= 20
        if not data.get("allow_tools"):
            score -= 20
    else:
        evidence.append((completed.stderr or completed.stdout)[-400:])
    return item("projectling_doctor", score, status_from_score(score), evidence, "执行 ./projectling/run.sh doctor 查看详情。" if score < 85 else "")


def check_tool_schema() -> dict[str, Any]:
    expected = {
        "command",
        "terminal",
        "aidebug",
        "apply_patch",
        "web_search",
        "context_manage",
        "memory_add",
        "memory_check",
        "memorycheak",
        "memory_read",
    }
    try:
        completed = run_cmd([str(PROJECTLING_RUN), "show-tools", "--json"], cwd=AITERMUX_HOME, timeout=20)
    except Exception as exc:
        return item("tool_schema", 0, "fail", [f"exception={exc}"], "修复 show-tools。")
    if completed.returncode != 0:
        return item("tool_schema", 20, "fail", [f"rc={completed.returncode}", completed.stderr[-300:]], "修复工具注册。")
    try:
        schemas = json.loads(completed.stdout)
    except json.JSONDecodeError:
        return item("tool_schema", 50, "warn", ["schema_json=invalid"], "修复 show-tools JSON。")
    names = {str((schema.get("function") or {}).get("name") or "") for schema in schemas if isinstance(schema, dict)}
    missing = sorted(expected - names)
    score = 100 if not missing else max(30, 100 - 15 * len(missing))
    return item("tool_schema", score, status_from_score(score), [f"names={sorted(names)}", f"missing={missing}"], "补齐缺失工具 schema。" if missing else "")


def _doctor_json() -> tuple[dict[str, Any] | None, str]:
    try:
        completed = run_cmd([str(PROJECTLING_RUN), "doctor"], cwd=AITERMUX_HOME, timeout=20)
    except Exception as exc:
        return None, f"exception={exc}"
    if completed.returncode != 0:
        return None, f"rc={completed.returncode} stderr={(completed.stderr or completed.stdout)[-240:]}"
    try:
        return json.loads(completed.stdout), ""
    except json.JSONDecodeError:
        return None, "doctor_json=invalid"


def check_memory_layout() -> dict[str, Any]:
    data, error = _doctor_json()
    if data is None:
        return item("memory_layout", 0, "fail", [error], "修复 doctor 后再检查 memory。")
    memory = data.get("memory") or {}
    required = [
        Path(str(memory.get("memory_dir") or "")),
        Path(str(memory.get("datememory_path") or "")),
        Path(str(memory.get("memory_db_path") or "")),
    ]
    missing = [str(path) for path in required if not str(path) or not path.exists()]
    score = 100 if not missing else max(40, 100 - len(missing) * 25)
    evidence = [
        f"context_mode={data.get('context_mode')}",
        f"datememory_bytes={memory.get('datememory_bytes')}",
        f"memory_db_diaries={memory.get('memory_db_diaries')}",
        f"missing={missing}",
    ]
    return item("memory_layout", score, status_from_score(score), evidence, "运行 ./projectling/run.sh doctor 初始化 memory。" if missing else "")


def check_context_mode_config() -> dict[str, Any]:
    data, error = _doctor_json()
    if data is None:
        return item("context_mode_config", 0, "fail", [error], "修复 doctor。")
    mode = str(data.get("context_mode") or "")
    ok = mode in {"role", "fused"}
    evidence = [
        f"context_mode={mode}",
        f"shared_context_chars={data.get('shared_context_chars')}",
        f"role_context_chars={data.get('role_context_chars')}",
    ]
    return item(
        "context_mode_config",
        100 if ok else 45,
        "ok" if ok else "warn",
        evidence,
        "设置 PROJECTLING_CONTEXT_MODE=role 或 fused。" if not ok else "",
    )


def check_route_alignment() -> dict[str, Any]:
    if not _projectling_available():
        return item("route_alignment", 0, "fail", ["projectling imports unavailable"], "检查 projectling.py/tooling.py 导入链。")
    try:
        engine = ProjectLingEngine(load_config())  # type: ignore[operator]
    except Exception as exc:
        return item("route_alignment", 0, "fail", [f"engine_init={exc}"], "修复 projectling 初始化。")

    scenarios = [
        (
            "strict_short",
            "只回复：OK。不要解释。",
            "strict_short_reply",
            "deepseek-chat",
            False,
        ),
        (
            "execution",
            "请使用 command 运行 pwd，然后只输出当前路径。",
            "execution_or_format",
            "deepseek-chat",
            False,
        ),
        (
            "casual_chat",
            "你好呀",
            "casual_chat",
            "deepseek-chat",
            False,
        ),
        (
            "analysis",
            "综合判断这个项目如何优化，列计划。",
            "analysis",
            str(load_config().model if load_config else "").strip() or "deepseek-reasoner",
            None,
        ),
    ]

    evidence: list[str] = []
    score = 100
    for label, prompt, expected_category, expected_model, expected_thinking in scenarios:
        route = engine.preview_route(prompt, allow_tools=True)
        actual_category = str(route.get("category") or "")
        actual_model = str(route.get("model") or "")
        actual_thinking = route.get("thinking_enabled")
        ok = actual_category == expected_category and actual_model == expected_model
        if expected_thinking is not None:
            ok = ok and bool(actual_thinking) == bool(expected_thinking)
        if not ok:
            score -= 25
        evidence.append(
            f"{label}: category={actual_category} model={actual_model} thinking={actual_thinking} expected={expected_category}/{expected_model}"
        )
    score = max(25, score)
    return item(
        "route_alignment",
        score,
        status_from_score(score),
        evidence,
        "修正 projectling 路由决策或短答提示策略。" if score < 85 else "",
    )


def check_persona_split() -> dict[str, Any]:
    data, error = _doctor_json()
    if data is None:
        return item("persona_split", 0, "fail", [error], "修复 doctor 后再检查 persona 显示。")
    main_zh = str(data.get("persona_display_zh") or "").strip()
    main_en = str(data.get("persona_display_en") or "").strip()
    liaison_zh = str(data.get("persona_liaison_display_zh") or "").strip()
    liaison_en = str(data.get("persona_liaison_display_en") or "").strip()
    liaison_label = str(data.get("persona_liaison") or "").strip()
    persona_locked = bool(data.get("persona_locked"))
    liaison_locked = bool(data.get("liaison_locked"))
    split_ok = bool(main_zh and main_en and liaison_zh and liaison_en and (main_zh != liaison_zh or main_en != liaison_en))
    evidence = [
        f"main={main_zh} / {main_en}",
        f"liaison={liaison_zh} / {liaison_en}",
        f"persona_liaison={liaison_label}",
        f"locks=main:{persona_locked} liaison:{liaison_locked}",
    ]
    score = 100 if split_ok else 55
    return item(
        "persona_split",
        score,
        status_from_score(score),
        evidence,
        "检查 persona 绑定是否仍然被融合，或确认辅导位是否可见。" if not split_ok else "",
    )


def check_command_guard() -> dict[str, Any]:
    payload = _execute_tool(
        "command",
        {"command": "rm -rf /", "brief": "检查高危命令门禁"},
        cwd=HEALTH_SANDBOX_DIR,
    )
    if payload is None:
        return item("command_guard", 0, "fail", ["tool execution unavailable"], "检查 projectling/tooling imports。")
    status = str(payload.get("status") or "")
    confirm = str(payload.get("confirm_command") or "")
    evidence = [
        f"status={status}",
        f"confirm={confirm}",
        f"reason={payload.get('reason')}",
    ]
    ok = status == "pending_confirmation" and confirm == "yes"
    score = 100 if ok else 30
    return item(
        "command_guard",
        score,
        status_from_score(score),
        evidence,
        "修复 command 高危门禁。" if not ok else "",
    )


def check_context_budget_runtime() -> dict[str, Any]:
    payload = _execute_tool(
        "context",
        {"percent": 35, "turns": 2, "brief": "检查上下文预算"},
        cwd=HEALTH_SANDBOX_DIR,
    )
    if payload is None:
        return item("context_budget_runtime", 0, "fail", ["tool execution unavailable"], "检查 projectling/tooling imports。")
    status = str(payload.get("status") or "")
    percent = payload.get("percent")
    turns = payload.get("turns_remaining")
    summary = _health_summary(payload)
    evidence = [f"status={status}", f"percent={percent}", f"turns={turns}", f"summary={summary}"]
    ok = status == "ok" and int(percent or 0) == 35 and int(turns or 0) == 2
    score = 100 if ok else 35
    return item(
        "context_budget_runtime",
        score,
        status_from_score(score),
        evidence,
        "修复 context 工具的预算写入或摘要回执。" if not ok else "",
    )


def check_tool_fact_cards() -> dict[str, Any]:
    if not _projectling_available():
        return item("tool_fact_cards", 0, "fail", ["projectling imports unavailable"], "检查 projectling.py/tooling.py 导入链。")
    checks: list[tuple[str, dict[str, Any]]] = []
    checks.append(
        (
            "command",
            _execute_tool(
                "command",
                {"command": "pwd", "brief": "查看路径", "context_percent": 35},
                cwd=HEALTH_SANDBOX_DIR,
            )
            or {},
        )
    )
    checks.append(
        (
            "tool_manage",
            _execute_tool("tool_manage", {"action": "list", "brief": "列出工具箱状态"}, cwd=HEALTH_SANDBOX_DIR)
            or {},
        )
    )
    checks.append(
        (
            "apply_patch",
            _execute_tool(
                "apply_patch",
                {
                    "patch": "*** Begin Patch\n*** Add File: tmp_health_probe.txt\n+probe\n*** End Patch\n",
                    "brief": "检查补丁摘要",
                    "check_only": True,
                },
                cwd=HEALTH_SANDBOX_DIR,
            )
            or {},
        )
    )

    evidence: list[str] = []
    score = 100
    for name, payload in checks:
        status = str(payload.get("status") or "")
        summary = str(payload.get("summary") or "")
        kind = str(payload.get("kind") or "")
        evidence.append(f"{name}: status={status} kind={kind} summary={summary}")
        if status != "ok" or not summary:
            score -= 25

    score = max(25, score)
    return item(
        "tool_fact_cards",
        score,
        status_from_score(score),
        evidence,
        "补齐工具 result.summary / kind / 输出摘要卡。" if score < 85 else "",
    )


def check_health_history_trend(history: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    summary = _health_history_summary(history if history is not None else _load_health_history())
    run_count = int(summary.get("run_count") or 0)
    recent_count = int(summary.get("recent_count") or 0)
    if run_count == 0:
        return item(
            "health_history_trend",
            75,
            "warn",
            ["runs=0", "recent=0"],
            "再运行 aidebug health，建立第一条历史基线。",
        )

    latest = summary.get("latest_score")
    previous = summary.get("previous_score")
    delta = summary.get("delta")
    recent_average = summary.get("recent_average")
    recent_min = summary.get("recent_min")
    recent_max = summary.get("recent_max")
    trend = str(summary.get("trend") or "unknown")

    if recent_count < 2:
        score = 80
    else:
        score = 100
        try:
            numeric_delta = float(delta)
            numeric_min = float(recent_min)
            numeric_average = float(recent_average)
        except (TypeError, ValueError):
            numeric_delta = 0.0
            numeric_min = 100.0
            numeric_average = 100.0
        if numeric_delta <= -10 or numeric_min < 85:
            score = 70
        elif numeric_delta <= -5 or numeric_min < 95:
            score = 85
        elif numeric_average < 98:
            score = 95

    evidence = [
        f"runs={run_count}",
        f"recent={recent_count}",
        f"latest={latest}",
        f"previous={previous}",
        f"delta={delta}",
        f"recent_avg={recent_average}",
        f"range={recent_min}..{recent_max}",
        f"trend={trend}",
    ]
    next_action = ""
    if score < 85:
        next_action = "回看最近 health JSONL，定位分数断崖或链路回归。"
    elif recent_count < 3:
        next_action = "继续运行几轮 aidebug health，让趋势判断更稳定。"
    return item("health_history_trend", score, status_from_score(score), evidence, next_action)


def check_projectling_tests() -> dict[str, Any]:
    note = AIDEBUG_DIR / "logs" / "projectling-test.md"
    meta = file_meta(note)
    score = 100 if meta.get("exists") and int(meta.get("bytes") or 0) > 500 else 55
    evidence = [f"exists={meta.get('exists')}", f"bytes={meta.get('bytes', 0)}", f"age={meta.get('age_seconds', '-')}"]
    return item("projectling_test_record", score, status_from_score(score), evidence, "补写测试记录，避免重复测试。" if score < 85 else "")


def check_auto_runner_history() -> dict[str, Any]:
    path = LOG_DIR / "projectling-auto.jsonl"
    meta = file_meta(path)
    if not meta.get("exists"):
        return item("projectling_auto_runner", 50, "warn", ["missing projectling-auto.jsonl"], "运行 aidebug projectling-auto。")
    last = ""
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        last = lines[-1] if lines else ""
        data = json.loads(last) if last else {}
    except Exception as exc:
        return item("projectling_auto_runner", 55, "warn", [f"parse_error={exc}"], "检查 projectling-auto.jsonl。")
    ok = bool(data.get("ok"))
    age = int(meta.get("age_seconds") or 0)
    score = 100 if ok else 60
    if age > 7 * 86400:
        score -= 15
    return item(
        "projectling_auto_runner",
        score,
        status_from_score(score),
        [f"last_ok={ok}", f"age={age}", f"detail={data.get('detail_path')}"],
        "运行 aidebug projectling-auto --rounds 1 做回归。" if score < 85 else "",
    )


def check_terminal_logs() -> dict[str, Any]:
    terminal_dir = AIDEBUG_DIR / "projectling" / "terminal output"
    logs = sorted(terminal_dir.glob("*.log")) if terminal_dir.exists() else []
    state = terminal_dir / "terminal-sessions.json"
    score = 100 if state.exists() else 75
    if not logs:
        score -= 20
    latest = logs[-1] if logs else None
    evidence = [f"logs={len(logs)}", f"state_exists={state.exists()}"]
    if latest:
        meta = file_meta(latest)
        evidence.append(f"latest={latest.name} lines={meta.get('lines')} bytes={meta.get('bytes')}")
    return item("terminal_logs", score, status_from_score(score), evidence, "启动 terminal smoke 检查协作终端链路。" if score < 85 else "")


def check_motd_zshrc_smoke() -> dict[str, Any]:
    path = LOG_DIR / "motd-zshrc-smoke.jsonl"
    meta = file_meta(path)
    if not meta.get("exists"):
        return item("motd_zshrc_smoke", 50, "warn", ["missing motd-zshrc-smoke.jsonl"], "运行 aidebug motd-zshrc-smoke。")
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        data = json.loads(lines[-1]) if lines else {}
    except Exception as exc:
        return item("motd_zshrc_smoke", 55, "warn", [f"parse_error={exc}"], "检查 motd-zshrc-smoke.jsonl。")
    ok = bool(data.get("ok"))
    score = 100 if ok else 60
    if int(meta.get("age_seconds") or 0) > 7 * 86400:
        score -= 15
    return item(
        "motd_zshrc_smoke",
        score,
        status_from_score(score),
        [f"last_ok={ok}", f"age={meta.get('age_seconds')}", f"summary={str(data)[:240]}"],
        "运行 aidebug motd-zshrc-smoke 复测启动 UI。" if score < 85 else "",
    )


def build_health() -> dict[str, Any]:
    history = _load_health_history(limit=12)
    checks = [
        check_layout(),
        check_logs(),
        check_projectling_doctor(),
        check_tool_schema(),
        check_route_alignment(),
        check_persona_split(),
        check_command_guard(),
        check_context_budget_runtime(),
        check_tool_fact_cards(),
        check_health_history_trend(history),
        check_memory_layout(),
        check_context_mode_config(),
        check_projectling_tests(),
        check_auto_runner_history(),
        check_terminal_logs(),
        check_motd_zshrc_smoke(),
    ]
    total = round(sum(check["score"] for check in checks) / max(1, len(checks)), 1)
    status = status_from_score(int(total))
    payload = {
        "generated_at": timestamp(),
        "aidebug_dir": str(AIDEBUG_DIR),
        "projectling_dir": str(PROJECTLING_DIR),
        "overall_score": total,
        "overall_status": status,
        "history": _health_history_summary(history),
        "checks": checks,
    }
    latest_previous = payload["history"].get("latest_score") if isinstance(payload.get("history"), dict) else None
    if latest_previous is not None:
        try:
            payload["history"]["current_delta"] = round(total - float(latest_previous), 1)
            payload["history"]["current_score"] = total
        except (TypeError, ValueError):
            pass
    return payload


def write_reports(payload: dict[str, Any]) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    NOTE_DIR.mkdir(parents=True, exist_ok=True)
    HEALTH_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    with HEALTH_JSONL.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
    lines = [
        "# AITermux Aidebug Health",
        "",
        f"- generated_at: {payload['generated_at']}",
        f"- overall: {payload['overall_status']} / {payload['overall_score']}",
        "",
        "## Checks",
    ]
    for check in payload["checks"]:
        lines.append(f"- {check['name']}: {check['status']} / {check['score']}")
        if check.get("next_action"):
            lines.append(f"  next: {check['next_action']}")
    history = payload.get("history") or {}
    if isinstance(history, dict):
        lines.extend(
            [
                "",
                "## Recent Trend",
                f"- previous_runs: {history.get('run_count', 0)}",
                f"- latest_previous: {history.get('latest_status', '-')}"
                f" / {history.get('latest_score', '-')}",
                f"- delta_vs_previous: {history.get('current_delta', '-')}",
                f"- recent_average: {history.get('recent_average', '-')}",
                f"- trend: {history.get('trend', '-')}",
            ]
        )
    HEALTH_MD.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def print_text(payload: dict[str, Any]) -> None:
    print(f"aidebug_health={payload['overall_status']} score={payload['overall_score']}")
    for check in payload["checks"]:
        print(f"{check['name']} status={check['status']} score={check['score']}")
        for evidence in check.get("evidence", [])[:3]:
            print(f"  evidence={evidence}")
        if check.get("next_action"):
            print(f"  next={check['next_action']}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="aidebug-health")
    parser.add_argument("--json", action="store_true", help="print JSON")
    args = parser.parse_args(argv)
    payload = build_health()
    write_reports(payload)
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print_text(payload)
    return 0 if payload["overall_status"] in {"ok", "warn"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
