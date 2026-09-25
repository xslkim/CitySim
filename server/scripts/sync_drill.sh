#!/usr/bin/env bash
# T-SYN-11 断网追平演练编排（04 §13 W1）。全程零人工干预，证据落 server/scripts/sync_drill_evidence/。
# 断网时长默认 WSIM_DRILL_OUTAGE_S=3600；加速演练：WSIM_DRILL_OUTAGE_S=90 bash …
# 用法：bash server/scripts/sync_drill.sh [--outage-s N]
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$REPO_ROOT/server"
PG_BIN="$HOME/pgsql/bin"

"$PG_BIN/pg_isready" -h /tmp -d postgres >/dev/null 2>&1 || {
  echo "[sync_drill] PG 未运行" >&2; exit 1; }

OUTAGE=()
if [ "${1:-}" = "--outage-s" ] && [ -n "${2:-}" ]; then
  OUTAGE=(--outage-s "$2")
elif [ -n "${WSIM_DRILL_OUTAGE_S:-}" ]; then
  OUTAGE=(--outage-s "$WSIM_DRILL_OUTAGE_S")
fi

cleanup() { # 不留孤儿进程
  pkill -f "uvicorn worldsim.ingest:app.*9177" 2>/dev/null || true
}
trap cleanup EXIT

uv run python scripts/sync_drill.py "${OUTAGE[@]}"
