#!/bin/sh
set -eu

ROOT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
cd "$ROOT_DIR"

if [ -x "$ROOT_DIR/venv/bin/python" ]; then
    PYTHON="$ROOT_DIR/venv/bin/python"
elif command -v python3 >/dev/null 2>&1; then
    PYTHON=$(command -v python3)
else
    printf '%s\n' '错误：找不到 Python 3，请先创建 venv 并安装 requirements.txt。' >&2
    exit 1
fi

if ! "$PYTHON" -c 'import uvicorn' >/dev/null 2>&1; then
    printf '%s\n' '错误：当前 Python 环境缺少 uvicorn，请先执行：' >&2
    printf '%s\n' '  python3 -m venv venv && venv/bin/pip install -r requirements.txt' >&2
    exit 1
fi

read -r HOST PORT <<EOF
$($PYTHON -c 'from config import CFG; print(CFG.host, CFG.port)')
EOF

RUNTIME_ID=$(printf '%s' "$ROOT_DIR" | cksum | cut -d ' ' -f 1)
PID_FILE="${TMPDIR:-/tmp}/muse2api-${RUNTIME_ID}.pid"
LOCK_DIR="${TMPDIR:-/tmp}/muse2api-${RUNTIME_ID}.lock"
LOG_FILE="${TMPDIR:-/tmp}/muse2api-${RUNTIME_ID}.log"
HEALTH_URL="http://127.0.0.1:${PORT}/healthz"

if ! mkdir "$LOCK_DIR" 2>/dev/null; then
    if [ -f "$LOCK_DIR/pid" ] && kill -0 "$(cat "$LOCK_DIR/pid")" 2>/dev/null; then
        printf '%s\n' '已有另一个启动脚本正在执行，已退出。' >&2
        exit 0
    fi
    rm -rf "$LOCK_DIR"
    mkdir "$LOCK_DIR"
fi
printf '%s\n' "$$" > "$LOCK_DIR/pid"
trap 'rm -rf "$LOCK_DIR"' EXIT INT TERM

if command -v curl >/dev/null 2>&1 && curl -fsS --max-time 2 "$HEALTH_URL" >/dev/null 2>&1; then
    printf '%s\n' "服务已经启动：http://127.0.0.1:${PORT}"
    exit 0
fi

if [ -f "$PID_FILE" ]; then
    PID=$(cat "$PID_FILE")
    case "$PID" in
        ''|*[!0-9]*) PID='';;
    esac
    if [ -n "$PID" ] && kill -0 "$PID" 2>/dev/null; then
        printf '%s\n' "服务进程已经存在（PID: $PID），已跳过重复启动。"
        exit 0
    fi
    rm -f "$PID_FILE"
fi

nohup "$PYTHON" -m uvicorn app:app --host "$HOST" --port "$PORT" \
    >"$LOG_FILE" 2>&1 < /dev/null &
PID=$!
printf '%s\n' "$PID" > "$PID_FILE"

sleep 1
if ! kill -0 "$PID" 2>/dev/null; then
    printf '%s\n' "启动失败，日志：$LOG_FILE" >&2
    exit 1
fi

printf '%s\n' "服务已启动：http://127.0.0.1:${PORT}"
printf '%s\n' "PID：$PID"
printf '%s\n' "日志：$LOG_FILE"
