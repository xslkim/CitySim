#!/usr/bin/env bash
# ==============================================================================
# WorldSim 全链路冷启动冒烟（08 T-OPS-05 唯一创建者；09 §8 七步逐字实现；R1 §A.10）
#
# 用法：
#   bash server/scripts/smoke.sh           # 七步全量（冷启动会 reset 主库！需 WSIM_SMOKE_ALLOW_RESET=1）
#   bash server/scripts/smoke.sh m3        # m3 段：封装 tests/integration/test_m3_sim_week.py（R2 §A.14）
#   bash server/scripts/smoke.sh e9        # 用户态恢复演练（09 §7 E9，A16）：kill 内核 → start_local 恢复
#
# 破坏性声明：step① 冷启动序列会 drop/重建 worldsim 与 worldsim_replica（09 §8 "冷启动"语义）。
# 护栏：未显式 WSIM_SMOKE_ALLOW_RESET=1 时拒绝执行七步全量。
# 报告：var/logs/smoke_report.txt（各项 PASS/FAIL + 关键计数；全 PASS 才算交付）。
# ==============================================================================
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
SERVER_DIR="$REPO_ROOT/server"
PG_BIN="$HOME/pgsql/bin"
OBS_PORT="${WSIM_OBS_PORT:-8080}"
WEB_PORT=5173
INGEST_BIND="${WSIM_INGEST_BIND:-127.0.0.1:9100}"
CHROMIUM="${WSIM_CHROMIUM:-/home/xsl/.local/bin/chromium}"
REPORT="$REPO_ROOT/var/logs/smoke_report.txt"
SHOT_DIR="$REPO_ROOT/var/shots/smoke"
KERNEL_MIN_WALL_S="${WSIM_SMOKE_KERNEL_S:-600}"   # step② ≥10 真实分钟（09 §8 口径）

mkdir -p "$REPO_ROOT/var/logs" "$SHOT_DIR"
: > "$REPORT"

log() { printf '[smoke] %s\n' "$*"; }
record() { # record <step> <PASS|FAIL|WARN> <detail>
  printf '%s\t%s\t%s\n' "$1" "$2" "$3" | tee -a "$REPORT"
  [ "$2" != "FAIL" ] || FAILED=1
}
FAILED=0

psql_main() { "$PG_BIN/psql" -h /tmp -d worldsim -v ON_ERROR_STOP=1 -tAc "$1"; }

# ---------------------------------------------------------------- m3 子命令
if [ "${1:-}" = "m3" ]; then
  log "m3 段：tests/integration/test_m3_sim_week.py（04 T-DIR-06 需求接收，R2 §A.14）"
  if (cd "$SERVER_DIR" && uv run pytest tests/integration/test_m3_sim_week.py -q); then
    record "m3" "PASS" "test_m3_sim_week.py 全绿"
  else
    record "m3" "FAIL" "test_m3_sim_week.py 有红"
  fi
  exit "$FAILED"
fi

# ---------------------------------------------------------------- e9 子命令（用户态恢复演练）
if [ "${1:-}" = "e9" ]; then
  log "E9 用户态恢复演练：kill 内核 → start_local.sh 恢复全栈（09 §7 E9，A16）"
  set -a; . "$REPO_ROOT/.env"; set +a
  before=$(psql_main "SELECT count(*) FROM events WHERE type IN ('time.paused','time.resumed','time.catchup.start','time.catchup.end')")
  pkill -f "\.venv/bin/python.*worldsim\.main" || true   # 锚定解释器，防误杀包装进程
  sleep 3
  bash "$REPO_ROOT/deploy/start_local.sh"
  sleep 45   # 等内核恢复与追平段（04 §3.2 短停机档）
  after_rows=$(psql_main "SELECT type || '=' || count(*)::text FROM events WHERE type IN ('time.paused','time.resumed','time.catchup.start','time.catchup.end') GROUP BY type ORDER BY type")
  after=$(psql_main "SELECT count(*) FROM events WHERE type IN ('time.paused','time.resumed','time.catchup.start','time.catchup.end')")
  if [ "$after" -gt "$before" ] && printf '%s' "$after_rows" | grep -q "time.paused" \
       && printf '%s' "$after_rows" | grep -q "time.resumed"; then
    record "e9" "PASS" "恢复后序列：$(printf '%s' "$after_rows" | tr '\n' ' ')"
  else
    record "e9" "FAIL" "序列不完整：$after_rows"
  fi
  exit "$FAILED"
fi

# ---------------------------------------------------------------- 七步全量
[ "${WSIM_SMOKE_ALLOW_RESET:-0}" = "1" ] || {
  echo "冷启动会 reset 主库/副本库（09 §8 语义）。确认执行：WSIM_SMOKE_ALLOW_RESET=1 bash $0" >&2
  exit 64; }

SMOKE_T0=$SECONDS
log "step① 冷启动序列：db_init reset → schema_v1 → seed_8 → 增量 DDL → replica_init → start_local"
set -a; . "$REPO_ROOT/.env"; set +a
{
  bash "$SERVER_DIR/scripts/db_init.sh" reset
  psql_main '' >/dev/null 2>&1 || true
  "$PG_BIN/psql" -h /tmp -d worldsim -v ON_ERROR_STOP=1 -f "$SERVER_DIR/ddl/schema_v1.sql" >/dev/null
  "$PG_BIN/psql" -h /tmp -d worldsim -v ON_ERROR_STOP=1 -f "$SERVER_DIR/ddl/seed_8.sql" >/dev/null
  "$PG_BIN/psql" -h /tmp -d worldsim -v ON_ERROR_STOP=1 -f "$SERVER_DIR/ddl/health_daily_v1.sql" >/dev/null
  "$PG_BIN/psql" -h /tmp -d worldsim -v ON_ERROR_STOP=1 -f "$SERVER_DIR/ddl/obs_views_v1.sql" >/dev/null
  "$PG_BIN/psql" -h /tmp -d worldsim -v ON_ERROR_STOP=1 -f "$SERVER_DIR/ddl/obs_derived_v1.sql" >/dev/null
  "$PG_BIN/psql" -h /tmp -d postgres -c "DROP DATABASE IF EXISTS worldsim_replica WITH (FORCE)" >/dev/null
  bash "$SERVER_DIR/scripts/replica_init.sh" >/dev/null
  # 内核加速档（09 §8 "加速档"冒烟工程口径）：试跑 unthrottled（--sim-hours 长跑；
  # paced 档段 ratio 恒被变速表覆写，--ratio 在 paced 无效，08 D15）
  SIM_HOURS="${WSIM_SMOKE_SIM_HOURS:-720}" bash "$REPO_ROOT/deploy/start_local.sh"
} >>"$REPO_ROOT/var/logs/smoke_step1.log" 2>&1 \
  && record "step1_cold_start" "PASS" "PG+内核+obs_refresh+摄入 API+派生 worker+obs-api+web dev 全栈就绪" \
  || record "step1_cold_start" "FAIL" "见 var/logs/smoke_step1.log"

log "step② 内核运行 ≥${KERNEL_MIN_WALL_S}s（加速档=试跑 unthrottled ${WSIM_SMOKE_SIM_HOURS:-720} sim-h，窗口跨多日界，08 D15）：events 增长 / seq 无空洞 / 类型覆盖 ≥6"
e0=$(psql_main "SELECT coalesce(max(seq),0) FROM events")
sleep "$KERNEL_MIN_WALL_S"
e1=$(psql_main "SELECT coalesce(max(seq),0) FROM events")
holes=$(psql_main "SELECT count(*) FROM (SELECT seq, lag(seq) OVER (ORDER BY seq) p FROM events) t
                   WHERE seq - p > 1 AND seq > $e0")
types=$(psql_main "SELECT count(DISTINCT type) FROM events WHERE seq > $e0")
if [ "$e1" -gt "$e0" ] && [ "$holes" = "0" ] && [ "$types" -ge 6 ]; then
  record "step2_kernel_10min" "PASS" "事件 $e0→$e1（+$((e1-e0))），seq 无空洞，类型覆盖 $types 类"
else
  record "step2_kernel_10min" "FAIL" "e0=$e0 e1=$e1 holes=$holes types=$types"
fi

log "step③ WSIM_REPLAY_MODE=replay 重放对账（零 LLM 调用）"
# 重放日取首个完整模拟日（多日跨度时第 N 日重放撞"窗口外事件"判定——replay_check 窗口
# 语义要求实跑恰为该日；首日窗口恒闭区间安全，冒烟工程口径，08 D15 补记）
DAY1=1
llm_before=$(psql_main "SELECT count(*) FROM llm_calls")
if (cd "$SERVER_DIR" && WSIM_REPLAY_MODE=replay uv run python scripts/replay_check.py \
      --sim-day "$DAY1" >>"$REPO_ROOT/var/logs/smoke_step3.log" 2>&1); then
  llm_after=$(psql_main "SELECT count(*) FROM llm_calls")
  if [ "$llm_after" = "$llm_before" ]; then
    record "step3_replay" "PASS" "sim-day $DAY1 重放对账一致，llm_calls 零新增"
  else
    record "step3_replay" "FAIL" "重放期 llm_calls +$((llm_after-llm_before))（应零调用）"
  fi
else
  record "step3_replay" "FAIL" "replay_check 非零退出（var/logs/smoke_step3.log）"
fi

log "step④ 观察端 API：dev token → 三端点 200 + proto:check + WS 增量"
TOKEN=$(cd "$SERVER_DIR" && uv run python -m worldsim.observe.tokens_cli issue --label smoke \
        | grep -oE 'dev_[A-Za-z0-9_-]+' | head -1)
step4_ok=1
if [ -z "$TOKEN" ]; then record "step4_obs_api" "FAIL" "token 签发失败"; step4_ok=0; fi
if [ "$step4_ok" = "1" ]; then
  for pair in "/api/snapshot:snapshot" "/api/events?limit=5:events" "/api/agents:agents"; do
    ep="${pair%%:*}"
    body=$(curl -sf "http://127.0.0.1:$OBS_PORT$ep" -H "Authorization: Bearer $TOKEN") \
      || { record "step4_obs_api" "FAIL" "$ep 非 200"; step4_ok=0; break; }
    if ! printf '%s' "$body" | (cd "$REPO_ROOT/web" && pnpm proto:check >/dev/null 2>&1); then
      record "step4_obs_api" "FAIL" "$ep proto:check 未过"; step4_ok=0; break
    fi
  done
fi
if [ "$step4_ok" = "1" ]; then
  if (cd "$SERVER_DIR" && T="$TOKEN" uv run python - "$OBS_PORT" <<'PY'
import asyncio, json, sys
import websockets

async def main() -> int:
    import os
    url = f"ws://127.0.0.1:{sys.argv[1]}/ws"
    async with websockets.connect(url) as ws:
        await ws.send(json.dumps({"op": "hello", "token": os.environ["T"], "client": "smoke/1.0"}))
        w = json.loads(await asyncio.wait_for(ws.recv(), 10))
        assert w["op"] == "welcome", w
        await ws.send(json.dumps({"op": "subscribe", "channels": [
            {"channel": "events", "filter": {}}]}))

        async def heartbeat() -> None:  # 03 §5.2：30s ping（90s 无心跳服务端 4408 断开）
            while True:
                await asyncio.sleep(20)
                await ws.send(json.dumps({"op": "ping"}))

        hb = asyncio.create_task(heartbeat())
        try:
            while True:
                f = json.loads(await asyncio.wait_for(ws.recv(), 180))  # 08 D15：M6 链路延迟窗口
                if f["op"] == "event":
                    print("WS 增量事件 seq=", f.get("data", {}).get("seq"))
                    return 0
                # pong/health/state_diff 等帧跳过继续等
        except asyncio.TimeoutError:
            print("WS 180s 未收增量事件（08 D15 窗口；M6 链路 = 内核→sync→ingest→副本→obs-api）")
            return 1
        finally:
            hb.cancel()

sys.exit(asyncio.run(main()))
PY
  ) >>"$REPO_ROOT/var/logs/smoke_step4.log" 2>&1; then
    record "step4_obs_api" "PASS" "三端点 200 + proto:check 绿 + WS 增量事件到达"
  else
    record "step4_obs_api" "FAIL" "WS 增量未达（var/logs/smoke_step4.log）"
  fi
fi

log "step⑤ headless chromium 三页截图（admin/lite/stream 写死 URL，09 §8）"
shot() { # shot <name> <url>
  "$CHROMIUM" --headless --disable-gpu --no-sandbox --hide-scrollbars \
    --window-size=1280,800 --screenshot="$SHOT_DIR/$1.png" \
    --virtual-time-budget=8000 "$2" >/dev/null 2>&1 || return 1
  [ -s "$SHOT_DIR/$1.png" ]
}
s5_ok=1
shot admin "http://127.0.0.1:$WEB_PORT/map" || s5_ok=0
shot lite "http://127.0.0.1:$WEB_PORT/lite/home" || s5_ok=0
shot stream "http://127.0.0.1:$OBS_PORT/stream/?token=$TOKEN" || s5_ok=0
[ "$s5_ok" = "1" ] || record "step5_screenshots" "FAIL" "chromium 截图失败（$CHROMIUM）"

# 非白屏启发式：PNG 字节数阈值（全白截图 <10KB；含事件文本/头像页面显著更大）
for p in admin lite stream; do
  sz=$(stat -c%s "$SHOT_DIR/$p.png" 2>/dev/null || echo 0)
  [ "$sz" -gt 20000 ] || { s5_ok=0; record "step5_screenshots" "FAIL" "$p.png ${sz}B 疑似白屏"; }
done
if [ "$s5_ok" = "1" ] && ! grep -q "^step5_screenshots.FAIL" "$REPORT"; then
  record "step5_screenshots" "PASS" "三页截图非白屏 → $SHOT_DIR"
fi


log "step⑥ 审计全绿 + llm_calls 计量"
if (cd "$SERVER_DIR" && uv run python scripts/audit_run.py \
      >>"$REPO_ROOT/var/logs/smoke_step6.log" 2>&1); then
  calls=$(psql_main "SELECT count(*) FROM llm_calls")
  cost=$(psql_main "SELECT coalesce(sum(cost_micro_cny),0) FROM llm_calls")
  if [ "$calls" -gt 0 ] && [ "$cost" -gt 0 ]; then
    record "step6_audit_cost" "PASS" "audit 全绿；llm_calls=$calls ¥合计=${cost} 微元"
  elif [ "$calls" -gt 0 ]; then
    # 免费档实测 ¥0（models.yaml price_status=measured_free_tier_w2，T-LLM-12；09 §8 ">0" 条款
    # 以付费档为前提——偏差登记：开发期降级为"有行且聚合 ≥0"）
    record "step6_audit_cost" "PASS" "audit 全绿；llm_calls=$calls；¥=0（免费档实测口径，降级注记）"
  else
    record "step6_audit_cost" "FAIL" "llm_calls 无行"
  fi
else
  record "step6_audit_cost" "FAIL" "audit_run 有红（var/logs/smoke_step6.log）"
fi

log "step⑦ 报告汇总"
counts=$(psql_main "SELECT (SELECT count(*) FROM events) || '/' ||
                           (SELECT count(*) FROM memories) || '/' ||
                           (SELECT count(*) FROM llm_calls)")
record "step7_report" "PASS" "events/memories/llm_calls = $counts；墙钟 $((SECONDS-SMOKE_T0))s（R8 回填）"

if [ "$FAILED" = "0" ]; then
  log "七步全 PASS（$REPORT）"
else
  log "存在 FAIL（$REPORT）"
fi
exit "$FAILED"
