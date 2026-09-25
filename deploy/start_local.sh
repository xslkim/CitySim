#!/usr/bin/env bash
# 本机一键起全栈（05 T-WEB-20 唯一创建者；09 §8 step① 形态：PG + 内核 + obs_refresh 常驻 + obs-api + web dev server）
# M6 由 08 T-OPS-05 增补 ingest/worker 起停段（本文件只增不重构）。
# 用法：bash deploy/start_local.sh [--sim-hours N]（默认内核长跑；N 给定时跑完即停内核，其余常驻）
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT/server"

PG_BIN="$HOME/pgsql/bin"
OBS_PORT="${WSIM_OBS_PORT:-8080}"
WEB_PORT=5173   # 钉死（05 T-WEB-08 / 09 §8 step⑤）
LOG_DIR="$REPO_ROOT/var/logs"
mkdir -p "$LOG_DIR"

# env（不打印内容）
set -a; . "$REPO_ROOT/.env"; set +a

log() { printf '[start_local] %s\n' "$*"; }
ready() { # ready <name> <url>
  local name="$1" url="$2"
  for _ in $(seq 1 60); do
    if curl -sf -o /dev/null "$url"; then log "$name ready"; return 0; fi
    sleep 1
  done
  log "FAIL: $name 未就绪（$url）"; return 1
}

# 1) PG
if ! "$PG_BIN/pg_isready" -h /tmp -d worldsim >/dev/null 2>&1; then
  log "启动 PG…"
  "$PG_BIN/pg_ctl" -D "$HOME/pgsql/data" -l "$LOG_DIR/pg.log" start
  "$PG_BIN/pg_isready" -h /tmp -d worldsim
else
  log "PG already running"
fi

# 2) 内核（mock provider 默认；真跑 GLM 加 --llm routed，00 §1 A9）
if ! pgrep -f "worldsim.main" >/dev/null; then
  log "启动内核（mock）…"
  HF_HUB_OFFLINE=1 PYTHONUNBUFFERED=1 nohup uv run python -m worldsim.main \
    ${SIM_HOURS:+--sim-hours $SIM_HOURS} ${WSIM_RATIO:+--ratio $WSIM_RATIO} \
    >>"$LOG_DIR/kernel.log" 2>&1 &
else
  log "内核 already running"
fi

# 3) obs_refresh 常驻（新模拟日快照到达即重算三实体表；轮询 var/snapshot 目录）
if ! pgrep -f "obs_refresh_loop" >/dev/null; then
  log "启动 obs_refresh 常驻…"
  nohup bash -c 'while true; do
    cd "'"$REPO_ROOT"'/server" && uv run python scripts/obs_refresh.py --all >>"'"$LOG_DIR"'/obs_refresh.log" 2>&1
    sleep 30
  done' >/dev/null 2>&1 &
  echo $! > "$LOG_DIR/obs_refresh_loop.pid"
else
  log "obs_refresh already running"
fi

# 4) obs-api（03 §8.1）
if ! pgrep -f "uvicorn worldsim.observe.app" >/dev/null; then
  log "启动 obs-api :$OBS_PORT…"
  nohup uv run uvicorn worldsim.observe.app:app --port "$OBS_PORT" >>"$LOG_DIR/obs-api.log" 2>&1 &
else
  log "obs-api already running"
fi
# obs-api 就绪检查（无 token 401 也算进程就绪）
for _ in $(seq 1 60); do
  code=$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$OBS_PORT/api/usage" || true)
  [ "$code" = "401" ] || [ "$code" = "200" ] || [ "$code" = "422" ] && { log "obs-api 就绪（HTTP $code，无 token 401 为预期）"; break; }
  sleep 1
done

# 5) web dev server（vite，端口钉死 5173，09 §8 step⑤）
if ! pgrep -f "vite" >/dev/null; then
  log "启动 web dev server :$WEB_PORT…"
  cd "$REPO_ROOT/web"
  nohup pnpm dev >>"$LOG_DIR/web.log" 2>&1 &
  cd "$REPO_ROOT/server"
else
  log "web dev server already running"
fi
ready "web dev server" "http://127.0.0.1:$WEB_PORT/map"

# 6) 摄入 API（08 T-OPS-05 增补段，R1 §A.9；绑定 WSIM_INGEST_BIND，07 T-SYN-04 登记）
INGEST_BIND="${WSIM_INGEST_BIND:-127.0.0.1:9100}"
if ! pgrep -f "uvicorn worldsim.ingest" >/dev/null; then
  log "启动摄入 API $INGEST_BIND…"
  nohup uv run uvicorn worldsim.ingest:app --host "${INGEST_BIND%:*}" --port "${INGEST_BIND##*:}" \
    >>"$LOG_DIR/ingest.log" 2>&1 &
  echo $! > "$LOG_DIR/ingest.pid"
else
  log "摄入 API already running"
fi
for _ in $(seq 1 60); do
  code=$(curl -s -o /dev/null -w '%{http_code}' "http://${INGEST_BIND}/v1/health" || true)
  [ "$code" = "401" ] || [ "$code" = "200" ] && { log "摄入 API 就绪（HTTP $code）"; break; }
  sleep 1
done

# 7) 派生 worker（07 T-SYN-07/08 登记起停参数：uv run python -m worldsim.ingest.derived.worker）
if ! pgrep -f "worldsim.ingest.derived.worker" >/dev/null; then
  log "启动派生 worker…"
  nohup uv run python -m worldsim.ingest.derived.worker >>"$LOG_DIR/derived_worker.log" 2>&1 &
  echo $! > "$LOG_DIR/derived_worker.pid"
else
  log "派生 worker already running"
fi

# 8) stream 直播页就绪检查（M5 遗留 B11 需求条目；obs-api 三挂载托管，06 T-ART-03）
stream_ready=0
for _ in $(seq 1 30); do
  if curl -sf -o /dev/null "http://127.0.0.1:$OBS_PORT/stream/"; then stream_ready=1; break; fi
  sleep 1
done
if [ "$stream_ready" = "1" ]; then
  log "stream 直播页就绪（http://127.0.0.1:$OBS_PORT/stream/）"
else
  log "WARN: stream 直播页未就绪（obs-api /stream/ 挂载检查，06 T-ART-03）"
fi

log "全栈就绪：admin http://127.0.0.1:$WEB_PORT/map · lite http://127.0.0.1:$WEB_PORT/lite/home · obs-api :$OBS_PORT"
log "stream 直播页：http://127.0.0.1:$OBS_PORT/stream/?token=<dev token>（token 签发：cd server && uv run python -m worldsim.observe.tokens_cli issue --label <name>）"
log "摄入 API：http://$INGEST_BIND/v1/health（Bearer 鉴权，04 §9.1）· 派生 worker 常驻"
log "停止：pkill -f 'worldsim.main|obs_refresh_loop|uvicorn worldsim.observe|uvicorn worldsim.ingest|worldsim.ingest.derived.worker|vite'；另见 README"
