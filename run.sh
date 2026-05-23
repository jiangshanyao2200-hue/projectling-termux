#!/data/data/com.termux/files/usr/bin/bash
set -euo pipefail

ROOT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
AITERMUX_HOME="${AITERMUX_HOME:-$(CDPATH= cd -- "$ROOT_DIR/.." && pwd)}"
AIDEBUG_DIR="${AITERMUX_AIDEBUG_DIR:-$AITERMUX_HOME/projectling/aidebug}"
AIDEBUG_LOG_DIR="$AIDEBUG_DIR/logs"
PROJECTLING_AIDEBUG_LOG="$AIDEBUG_LOG_DIR/projectling.log"
PROJECTLING_RUNTIME_DIR="${AITERMUX_PROJECTLING_RUNTIME_DIR:-$AIDEBUG_DIR/state/projectling}"
PROJECTLING_PID_FILE="$PROJECTLING_RUNTIME_DIR/projectling.pid"
PROJECTLING_LOG_CLEAN_STAMP="$PROJECTLING_RUNTIME_DIR/log-cleanup.stamp"
PROJECTLING_CHILD_PID=""

projectling_now_seconds() {
  date +%s 2>/dev/null || echo 0
}

projectling_shrink_file_tail_if_over_kb() {
  local path="$1"
  local max_kb="${2:-1024}"
  local keep_kb="${3:-$max_kb}"
  [ -f "$path" ] || return 0
  case "$max_kb" in ''|*[!0-9]*) return 0 ;; esac
  case "$keep_kb" in ''|*[!0-9]*) return 0 ;; esac
  [ "$max_kb" -gt 0 ] || return 0
  [ "$keep_kb" -gt 0 ] || return 0

  local max_bytes keep_bytes size_bytes tmp
  max_bytes=$((max_kb * 1024))
  keep_bytes=$((keep_kb * 1024))
  size_bytes="$(wc -c <"$path" 2>/dev/null || true)"
  case "$size_bytes" in ''|*[!0-9]*) return 0 ;; esac
  [ "$size_bytes" -gt "$max_bytes" ] || return 0
  [ "$keep_bytes" -le "$max_bytes" ] || keep_bytes="$max_bytes"

  mkdir -p "$AIDEBUG_LOG_DIR" >/dev/null 2>&1 || true
  tmp="$AIDEBUG_LOG_DIR/.trim.$$.$RANDOM"
  if tail -c "$keep_bytes" "$path" >"$tmp" 2>/dev/null; then
    mv -f "$tmp" "$path" 2>/dev/null || rm -f "$tmp" 2>/dev/null || true
  else
    rm -f "$tmp" 2>/dev/null || true
  fi
}

projectling_find_delete_old_files() {
  local dir="$1"
  local days="${2:-7}"
  shift 2 || true
  [ -d "$dir" ] || return 0
  case "$days" in ''|*[!0-9]*) return 0 ;; esac
  [ "$days" -gt 0 ] || return 0
  find "$dir" -type f -mtime +"$days" "$@" -delete 2>/dev/null || true
}

projectling_log_housekeeping_due() {
  local interval now last
  interval="${AITERMUX_LOG_CLEAN_INTERVAL_SECONDS:-3600}"
  case "$interval" in ''|*[!0-9]*) interval=3600 ;; esac
  [ "$interval" -gt 0 ] || return 0
  mkdir -p "$PROJECTLING_RUNTIME_DIR" >/dev/null 2>&1 || true
  now="$(projectling_now_seconds)"
  last="$(sed -n '1p' "$PROJECTLING_LOG_CLEAN_STAMP" 2>/dev/null | tr -cd '0-9' || true)"
  case "$now" in ''|*[!0-9]*) return 0 ;; esac
  case "$last" in ''|*[!0-9]*) return 0 ;; esac
  [ $((now - last)) -ge "$interval" ]
}

projectling_mark_log_housekeeping() {
  mkdir -p "$PROJECTLING_RUNTIME_DIR" >/dev/null 2>&1 || true
  projectling_now_seconds >"$PROJECTLING_LOG_CLEAN_STAMP" 2>/dev/null || true
}

projectling_log_housekeeping() {
  projectling_log_housekeeping_due || return 0
  mkdir -p "$AIDEBUG_LOG_DIR" "$PROJECTLING_RUNTIME_DIR" >/dev/null 2>&1 || true

  projectling_shrink_file_tail_if_over_kb \
    "$AIDEBUG_LOG_DIR/startup.log" \
    "${AITERMUX_STARTUP_LOG_MAX_KB:-1024}" \
    "${AITERMUX_STARTUP_LOG_KEEP_KB:-512}" || true
  projectling_shrink_file_tail_if_over_kb \
    "$PROJECTLING_AIDEBUG_LOG" \
    "${AITERMUX_PROJECTLING_LOG_MAX_KB:-512}" \
    "${AITERMUX_PROJECTLING_LOG_KEEP_KB:-256}" || true
  for component_log in "$AIDEBUG_LOG_DIR"/motd.log "$AIDEBUG_LOG_DIR"/zshrc.log "$AIDEBUG_LOG_DIR"/bootstrap.log "$AIDEBUG_LOG_DIR"/events.log; do
    projectling_shrink_file_tail_if_over_kb \
      "$component_log" \
      "${AITERMUX_COMPONENT_LOG_MAX_KB:-512}" \
      "${AITERMUX_COMPONENT_LOG_KEEP_KB:-256}" || true
  done
  for jsonl_log in "$AIDEBUG_LOG_DIR"/*.jsonl; do
    [ -e "$jsonl_log" ] || continue
    projectling_shrink_file_tail_if_over_kb \
      "$jsonl_log" \
      "${AITERMUX_JSONL_LOG_MAX_KB:-1024}" \
      "${AITERMUX_JSONL_LOG_KEEP_KB:-512}" || true
  done

  projectling_find_delete_old_files "$AIDEBUG_DIR/tmp" "${AITERMUX_TMP_LOG_KEEP_DAYS:-7}" || true
  projectling_find_delete_old_files "$AIDEBUG_DIR/projectling/terminal output" "${AITERMUX_TERMINAL_LOG_KEEP_DAYS:-14}" \
    \( -name '*.log' -o -name '*.out' -o -name '*.err' -o -name '*.txt' -o -name '*.typescript' -o -name '*.launch.sh' \) || true
  projectling_find_delete_old_files "$AIDEBUG_DIR/state/projectling-auto/rounds" "${AITERMUX_STATE_LOG_KEEP_DAYS:-14}" || true
  find "$AIDEBUG_DIR" -type d -name __pycache__ -prune -exec rm -rf {} + 2>/dev/null || true
  find "$AIDEBUG_DIR" -type f -mtime +"${AITERMUX_TMP_LOG_KEEP_DAYS:-7}" \
    \( -name '.tmp.*' -o -name '*.tmp' -o -name '*.bak' \) -delete 2>/dev/null || true

  projectling_mark_log_housekeeping
}

projectling_debug_log() {
  local ts msg
  msg="$*"
  mkdir -p "$AIDEBUG_LOG_DIR" >/dev/null 2>&1 || true
  ts="$(date -u '+%Y-%m-%dT%H:%M:%SZ' 2>/dev/null || date '+%F %T' 2>/dev/null || echo unknown)"
  printf '%s projectling %s\n' "$ts" "$msg" >>"$AIDEBUG_LOG_DIR/startup.log" 2>/dev/null || true
  printf '%s %s\n' "$ts" "$msg" >>"$PROJECTLING_AIDEBUG_LOG" 2>/dev/null || true
}

projectling_single_instance_enabled() {
  case "${PROJECTLING_SINGLE_INSTANCE:-auto}" in
    0|false|False|FALSE|no|No|NO|off|Off|OFF)
      return 1
      ;;
    1|true|True|TRUE|yes|Yes|YES|on|On|ON)
      return 0
      ;;
  esac

  case "${1:-}" in
    chat|shell-dispatch)
      return 0
      ;;
    *)
      return 1
      ;;
  esac
}

projectling_pid_command() {
  local pid
  pid="${1:-}"
  if [ -z "$pid" ]; then
    return 1
  fi
  ps -p "$pid" -o args= 2>/dev/null || true
}

projectling_pid_is_self() {
  local pid args
  pid="${1:-}"
  case "$pid" in
    ''|*[!0-9]*)
      return 1
      ;;
  esac
  args="$(projectling_pid_command "$pid")"
  case "$args" in
    *"$ROOT_DIR/core.py"*|*"projectling/core.py"*)
      return 0
      ;;
    *)
      return 1
      ;;
  esac
}

projectling_stop_pid() {
  local pid i
  pid="${1:-}"
  case "$pid" in
    ''|*[!0-9]*)
      return 0
      ;;
  esac
  if ! kill -0 "$pid" 2>/dev/null; then
    return 0
  fi
  kill -TERM "$pid" 2>/dev/null || true
  for i in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15; do
    if ! kill -0 "$pid" 2>/dev/null; then
      return 0
    fi
    sleep 0.1
  done
  kill -KILL "$pid" 2>/dev/null || true
}

projectling_prepare_single_instance() {
  local old_pid
  mkdir -p "$PROJECTLING_RUNTIME_DIR" >/dev/null 2>&1 || true
  old_pid="$(sed -n '1p' "$PROJECTLING_PID_FILE" 2>/dev/null | tr -cd '0-9' || true)"
  if [ -n "$old_pid" ]; then
    if projectling_pid_is_self "$old_pid"; then
      projectling_debug_log "runner_stop_old pid=$old_pid args=$*"
      projectling_stop_pid "$old_pid"
    else
      projectling_debug_log "runner_stale_pidfile pid=$old_pid args=$*"
    fi
  fi
  rm -f "$PROJECTLING_PID_FILE" 2>/dev/null || true
}

projectling_cleanup_single_instance() {
  local current_pid
  current_pid="$(sed -n '1p' "$PROJECTLING_PID_FILE" 2>/dev/null | tr -cd '0-9' || true)"
  if [ -n "${PROJECTLING_CHILD_PID:-}" ] && [ "$current_pid" = "$PROJECTLING_CHILD_PID" ]; then
    rm -f "$PROJECTLING_PID_FILE" 2>/dev/null || true
  fi
}

projectling_signal_exit() {
  local signal_name rc
  signal_name="${1:-TERM}"
  rc=143
  if [ "$signal_name" = "INT" ]; then
    rc=130
  elif [ "$signal_name" = "HUP" ]; then
    rc=129
  fi
  projectling_debug_log "runner_signal signal=$signal_name child=${PROJECTLING_CHILD_PID:-none} args=$*"
  if [ -n "${PROJECTLING_CHILD_PID:-}" ]; then
    projectling_stop_pid "$PROJECTLING_CHILD_PID"
  fi
  projectling_cleanup_single_instance
  exit "$rc"
}

if command -v python >/dev/null 2>&1; then
  PYTHON_BIN="$(command -v python)"
elif command -v python3 >/dev/null 2>&1; then
  PYTHON_BIN="$(command -v python3)"
else
  projectling_debug_log "runner_missing_python args=$*"
  echo "[projectling] 未找到 python。" >&2
  exit 127
fi

projectling_log_housekeeping || true
projectling_debug_log "runner_start python=$PYTHON_BIN cwd=$PWD args=$*"

if projectling_single_instance_enabled "${1:-}"; then
  projectling_prepare_single_instance "$@"
  trap 'projectling_signal_exit INT' INT
  trap 'projectling_signal_exit TERM' TERM
  trap 'projectling_signal_exit HUP' HUP
  set +e
  "$PYTHON_BIN" "$ROOT_DIR/core.py" "$@" &
  PROJECTLING_CHILD_PID="$!"
  printf '%s\n' "$PROJECTLING_CHILD_PID" >"$PROJECTLING_PID_FILE" 2>/dev/null || true
  wait "$PROJECTLING_CHILD_PID"
  rc=$?
  set -e
  projectling_cleanup_single_instance
else
  set +e
  "$PYTHON_BIN" "$ROOT_DIR/core.py" "$@"
  rc=$?
  set -e
fi
projectling_debug_log "runner_exit rc=$rc args=$*"
exit "$rc"
