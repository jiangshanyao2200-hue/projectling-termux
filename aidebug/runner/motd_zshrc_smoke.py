from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
from typing import Any


HOME = Path(os.environ.get("HOME", "/data/data/com.termux/files/home"))
AITERMUX_HOME = Path(os.environ.get("AITERMUX_HOME", str(HOME / "AItermux"))).expanduser()
AIDEBUG_DIR = Path(os.environ.get("AITERMUX_AIDEBUG_DIR", str(AITERMUX_HOME / "projectling" / "aidebug"))).expanduser()
LOG_DIR = AIDEBUG_DIR / "logs"
NOTE_DIR = AIDEBUG_DIR / "notes"
TMP_DIR = AIDEBUG_DIR / "tmp"
MOTD_SH = HOME / ".termux" / "motd.sh"
ZSHRC = HOME / ".zshrc"
SMOKE_LOG = LOG_DIR / "motd-zshrc-smoke.jsonl"
NOTE_PATH = NOTE_DIR / "motd-zshrc-smoke.md"


def timestamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def ensure_layout() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    NOTE_DIR.mkdir(parents=True, exist_ok=True)
    TMP_DIR.mkdir(parents=True, exist_ok=True)


def line_count(path: Path) -> int:
    if not path.exists():
        return 0
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        return sum(1 for _ in handle)


def read_new_lines(path: Path, start_line: int) -> list[str]:
    if not path.exists():
        return []
    lines: list[str] = []
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for index, line in enumerate(handle, start=1):
            if index > start_line:
                lines.append(line.rstrip("\n"))
    return lines


def run_cmd(command: list[str], *, timeout: float = 30.0, env: dict[str, str] | None = None) -> dict[str, Any]:
    started = time.time()
    merged_env = os.environ.copy()
    if env:
        merged_env.update(env)
    try:
        completed = subprocess.run(
            command,
            cwd=str(HOME),
            env=merged_env,
            text=True,
            capture_output=True,
            timeout=timeout,
        )
        return {
            "ok": completed.returncode == 0,
            "returncode": completed.returncode,
            "elapsed_seconds": round(time.time() - started, 3),
            "stdout": completed.stdout,
            "stderr": completed.stderr,
        }
    except subprocess.TimeoutExpired as exc:
        return {
            "ok": False,
            "returncode": None,
            "elapsed_seconds": round(time.time() - started, 3),
            "stdout": exc.stdout or "",
            "stderr": exc.stderr or "",
            "error": "timeout",
        }


def tmux(command: list[str], *, timeout: float = 10.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["tmux", *command], text=True, capture_output=True, timeout=timeout)


def tmux_has_session(name: str) -> bool:
    return tmux(["has-session", "-t", name], timeout=3).returncode == 0


def tmux_kill_session(name: str) -> None:
    subprocess.run(["tmux", "kill-session", "-t", name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def capture_pane(name: str) -> str:
    completed = tmux(["capture-pane", "-p", "-t", name, "-S", "-200"], timeout=3)
    return completed.stdout if completed.returncode == 0 else ""


def run_tty_motd(timeout_seconds: float) -> dict[str, Any]:
    if not shutil.which("tmux"):
        return {"ok": False, "skipped": True, "reason": "tmux_missing"}

    session = f"aitermux_motd_smoke_{os.getpid()}"
    capture_path = TMP_DIR / f"{session}.txt"
    tmux_kill_session(session)

    command = f"env AITERMUX_MOTD_INPUT_TIMEOUT=0.1 {MOTD_SH}"
    created = tmux(["new-session", "-d", "-x", "80", "-y", "24", "-s", session, command], timeout=5)
    if created.returncode != 0:
        return {
            "ok": False,
            "session": session,
            "error": "tmux_new_session_failed",
            "stderr": created.stderr.strip(),
        }

    menu_seen = False
    pane = ""
    started = time.time()
    try:
        while time.time() - started < timeout_seconds:
            if not tmux_has_session(session):
                break
            pane = capture_pane(session)
            if "Aitermux LUNCHER" in pane and "Esc 返回 Shell" in pane:
                menu_seen = True
                break
            time.sleep(0.4)

        capture_path.write_text(pane, encoding="utf-8")

        if tmux_has_session(session):
            tmux(["send-keys", "-t", session, "Escape"], timeout=3)

        exited = False
        for _ in range(20):
            if not tmux_has_session(session):
                exited = True
                break
            time.sleep(0.2)
    finally:
        if tmux_has_session(session):
            tmux_kill_session(session)

    text_hits = {
        "title": "Aitermux LUNCHER" in pane,
        "project": "启动 PROJECT" in pane,
        "esc": "Esc 返回 Shell" in pane,
        "title_artifact": "LUNCHERR" in pane,
    }
    return {
        "ok": bool(menu_seen and exited and not text_hits["title_artifact"]),
        "session": session,
        "capture": str(capture_path),
        "menu_seen": menu_seen,
        "escape_exit": exited,
        "elapsed_seconds": round(time.time() - started, 3),
        "text_hits": text_hits,
    }


def append_note(payload: dict[str, Any]) -> None:
    lines = [
        f"## {payload['started_at']} motd/zshrc smoke",
        "",
        f"- ok={int(payload['ok'])}",
        f"- non_tty_motd={payload['non_tty_motd']['ok']} rc={payload['non_tty_motd'].get('returncode')}",
        f"- zshrc={payload['zshrc']['ok']} rc={payload['zshrc'].get('returncode')}",
        f"- tty_motd={payload['tty_motd'].get('ok')} capture={payload['tty_motd'].get('capture')}",
        f"- startup_new_lines={payload['startup']['new_line_count']} warnings={len(payload['startup']['warnings'])}",
        "",
    ]
    with NOTE_PATH.open("a", encoding="utf-8") as handle:
        handle.write("\n".join(lines))


def main() -> int:
    parser = argparse.ArgumentParser(description="Smoke test AITermux motd and zshrc startup chain.")
    parser.add_argument("--json", action="store_true", help="print the full JSON payload")
    parser.add_argument("--tty-timeout", type=float, default=18.0, help="seconds to wait for the launcher menu")
    args = parser.parse_args()

    ensure_layout()
    started_at = timestamp()
    startup_log = LOG_DIR / "startup.log"
    motd_log = LOG_DIR / "motd.log"
    zshrc_log = LOG_DIR / "zshrc.log"
    start_lines = {
        "startup": line_count(startup_log),
        "motd": line_count(motd_log),
        "zshrc": line_count(zshrc_log),
    }

    non_tty = run_cmd(
        [str(MOTD_SH)],
        timeout=10,
        env={"AITERMUX_MOTD_INPUT_TIMEOUT": "0.02"},
    )
    non_tty["stdout_chars"] = len(non_tty.pop("stdout", ""))
    non_tty["stderr_chars"] = len(non_tty.pop("stderr", ""))
    non_tty["ok"] = bool(non_tty["returncode"] == 0 and non_tty["stdout_chars"] == 0 and non_tty["stderr_chars"] == 0)

    zsh = run_cmd(
        [
            "zsh",
            "-ic",
            "print ZSHRC_SMOKE_OK; whence projectling_dispatch_input >/dev/null 2>&1 && print PROJECTLING_HOOK_OK; alias menu >/dev/null 2>&1 && print MENU_ALIAS_OK",
        ],
        timeout=15,
        env={"AITERMUX_MOTD_DISABLE": "1"},
    )
    zsh_stdout = zsh.pop("stdout", "")
    zsh["stderr_chars"] = len(zsh.pop("stderr", ""))
    zsh["stdout_lines"] = zsh_stdout.splitlines()
    expected = {"ZSHRC_SMOKE_OK", "PROJECTLING_HOOK_OK", "MENU_ALIAS_OK"}
    zsh["ok"] = bool(zsh["returncode"] == 0 and expected.issubset(set(zsh["stdout_lines"])) and zsh["stderr_chars"] == 0)

    tty = run_tty_motd(args.tty_timeout)

    startup_new = read_new_lines(startup_log, start_lines["startup"])
    motd_new = read_new_lines(motd_log, start_lines["motd"])
    zshrc_new = read_new_lines(zshrc_log, start_lines["zshrc"])
    warnings = [
        line
        for line in startup_new
        if "/dev/tty" in line or "No such device" in line or "line " in line and "motd.sh" in line
    ]

    payload: dict[str, Any] = {
        "started_at": started_at,
        "ok": bool(non_tty["ok"] and zsh["ok"] and tty.get("ok") and not warnings),
        "non_tty_motd": non_tty,
        "zshrc": zsh,
        "tty_motd": tty,
        "startup": {
            "path": str(startup_log),
            "start_line": start_lines["startup"],
            "new_line_count": len(startup_new),
            "warnings": warnings[:20],
            "tail": startup_new[-8:],
        },
        "motd_log": {
            "path": str(motd_log),
            "start_line": start_lines["motd"],
            "new_lines": motd_new[-8:],
        },
        "zshrc_log": {
            "path": str(zshrc_log),
            "start_line": start_lines["zshrc"],
            "new_lines": zshrc_new[-8:],
        },
    }

    with SMOKE_LOG.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
    append_note(payload)

    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(f"ok={int(payload['ok'])}")
        print(f"non_tty_motd={int(non_tty['ok'])} rc={non_tty.get('returncode')}")
        print(f"zshrc={int(zsh['ok'])} rc={zsh.get('returncode')} lines={','.join(zsh.get('stdout_lines', []))}")
        print(f"tty_motd={int(bool(tty.get('ok')))} capture={tty.get('capture')}")
        print(f"startup_new_lines={len(startup_new)} warnings={len(warnings)}")

    return 0 if payload["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
