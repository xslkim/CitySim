#!/usr/bin/env bash
# T-SYN-10 观察端 DSN 切副本（00 §1 A5：仅 WSIM_OBS_PG_DSN 一行配置变更，代码零变更）。
# 步骤：D8 前置检查（副本 obs 视图层存在）→ obs_ro 密码对齐（开发期 loopback scram 要件）
#       → .env 一行值变更 → 重启 obs-api → 03 §5.1 接口清单回归 → pg_stat_activity 证据。
# 用法：bash server/scripts/switch_obs_dsn.sh [--revert]
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
ENV_FILE="$REPO_ROOT/.env"
PG_BIN="$HOME/pgsql/bin"
OBS_PORT="${WSIM_OBS_PORT:-8080}"
REPLICA_DB=worldsim_replica
LOG_DIR="$REPO_ROOT/var/logs"

log() { printf '[switch_obs_dsn] %s\n' "$*"; }
die() { log "ERROR: $*"; exit 1; }

current_dsn() { grep -E '^WSIM_OBS_PG_DSN=' "$ENV_FILE" | tail -1 | cut -d= -f2-; }

set_dsn() { # set_dsn <value>（恰一行值变更：注释与键名不动）
  local value="$1"
  if grep -qE '^WSIM_OBS_PG_DSN=' "$ENV_FILE"; then
    sed -i -E "s#^WSIM_OBS_PG_DSN=.*#WSIM_OBS_PG_DSN=$value#" "$ENV_FILE"
  else
    printf 'WSIM_OBS_PG_DSN=%s\n' "$value" >> "$ENV_FILE"
  fi
}

if [ "${1:-}" = "--revert" ]; then
  set_dsn ""
  pkill -f "uvicorn worldsim.observe.app" 2>/dev/null || true
  log "已回退 WSIM_OBS_PG_DSN 为空（回退主库 obs 白名单层），并停 obs-api（start_local.sh 重启生效）"
  exit 0
fi

[ -f "$ENV_FILE" ] || die ".env 不存在"
BEFORE="$(current_dsn)"

# 1) D8 前置：副本 obs 视图层命名约定（obs.events 等八对象 == M4 主库白名单视图名）
missing=$("$PG_BIN/psql" -h /tmp -d "$REPLICA_DB" -tAc "
  SELECT string_agg(x, ' ') FROM (VALUES ('events'),('memory_projection'),('relation_change_log'),
    ('relation_daily'),('health_daily'),('world_state_snapshot'),('ripple_edge'),('event_grade_view'))
    AS v(x) WHERE to_regclass('obs.' || x) IS NULL" 2>/dev/null || echo "__db_missing__")
[ -z "$missing" ] || die "副本 obs 视图层缺: $missing（先跑 bash server/scripts/replica_init.sh）"
log "D8 前置检查通过：副本 obs 八对象齐全"

# 2) obs_ro 密码对齐（开发期 127.0.0.1 scram 要件；密码仅落 .env，不打印）
OBS_PWD="$(head -c 18 /dev/urandom | od -An -tx1 | tr -d ' \n')"
"$PG_BIN/psql" -h /tmp -d postgres -v pwd="$OBS_PWD" >/dev/null <<'SQL'
ALTER ROLE obs_ro PASSWORD :'pwd'
SQL
NEW_DSN="postgresql://obs_ro:${OBS_PWD}@127.0.0.1:5432/${REPLICA_DB}"

# 3) .env 恰一行值变更
set_dsn "$NEW_DSN"
AFTER="$(current_dsn)"
[ "$BEFORE" != "$AFTER" ] || die "DSN 未变化"
log ".env 变更行数：$(diff <(git -C "$REPO_ROOT" show HEAD:.env 2>/dev/null || echo '') "$ENV_FILE" 2>/dev/null | grep -c '^[<>]' || true)（.env gitignored，前后值比对为准）"
log "WSIM_OBS_PG_DSN: '${BEFORE:-<空>}' → 'postgresql://obs_ro:***@127.0.0.1:5432/${REPLICA_DB}'"

# 4) 重启 obs-api
pkill -f "uvicorn worldsim.observe.app" 2>/dev/null || true
sleep 1
cd "$REPO_ROOT/server"
set -a; . "$ENV_FILE"; set +a
nohup uv run uvicorn worldsim.observe.app:app --port "$OBS_PORT" >>"$LOG_DIR/obs-api.log" 2>&1 &
for _ in $(seq 1 60); do
  code=$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$OBS_PORT/api/usage" || true)
  [ "$code" = "401" ] || [ "$code" = "200" ] && break
  sleep 1
done

# 5) 回归冒烟（03 §5.1 清单）：签发 dev token → 各端点 200 且 meta.watermark_tick 存在
TOKEN="$(uv run python -m worldsim.observe.tokens_cli issue --label switch-regression | grep -oE 'dev_[A-Za-z0-9_-]+' | head -1)"
[ -n "$TOKEN" ] || die "token 签发失败"
fail=0
for ep in /api/snapshot "/api/events?limit=1" "/api/events?grade=A&limit=1" /api/agents \
          /api/relations/snapshots /api/health; do
  body="$(curl -sf "http://127.0.0.1:$OBS_PORT$ep" -H "Authorization: Bearer $TOKEN")" \
    || { log "FAIL $ep（非 200）"; fail=1; continue; }
  printf '%s' "$body" | grep -q '"watermark_tick"' || { log "FAIL $ep（缺 meta.watermark_tick）"; fail=1; continue; }
  log "PASS $ep"
done
# 涟漪端点需真实事件 id：取副本最新一条事件
RID="$("$PG_BIN/psql" -h /tmp -d "$REPLICA_DB" -tAc "SELECT max(seq) FROM events" 2>/dev/null || echo '')"
if [ -n "$RID" ] && [ "$RID" != "" ]; then
  body="$(curl -sf "http://127.0.0.1:$OBS_PORT/api/ripple/$RID" -H "Authorization: Bearer $TOKEN")" \
    && printf '%s' "$body" | grep -q '"watermark_tick"' \
    && log "PASS /api/ripple/$RID" || { log "FAIL /api/ripple/$RID"; fail=1; }
fi

# 6) 证据：obs-api 连接用户/库 + obs_ro 写拒绝
"$PG_BIN/psql" -h /tmp -d postgres -tAc \
  "SELECT usename, datname FROM pg_stat_activity WHERE datname='$REPLICA_DB' AND usename='obs_ro' LIMIT 3"
"$PG_BIN/psql" "postgresql://obs_ro:${OBS_PWD}@127.0.0.1:5432/${REPLICA_DB}" -c \
  "INSERT INTO events (seq,tick,sim_time,wall_time,type,source,trigger,payload,visibility)
   VALUES (-1,0,now(),now(),'agent.move','system','system','{}','internal')" 2>&1 \
  | grep -q "permission denied" && log "obs_ro INSERT 被拒（双通道只读证据，05 §5）" || die "obs_ro 未被拒写"

[ "$fail" = "0" ] || die "回归有 FAIL"
log "切换完成且回归全绿"
