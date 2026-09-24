#!/usr/bin/env bash
# T-ENV-03：实例初始化与角色创建（幂等）。子命令：init / start / stop / status / reset
# 口径：数据目录 ~/pgsql/data（00 §1 A1）；unix socket（默认 /tmp）+ 127.0.0.1:5432 双通道；
# 认证 = socket trust + 127.0.0.1 scram-sha-256（01 文档 §6 D2）；时区 Asia/Shanghai（§6 D4）。
# 只建 worldsim 主库；副本库 worldsim_replica 唯一归 T-SYN-06（M6），本脚本不预留。
set -euo pipefail

PG_HOME="$HOME/pgsql"
PGDATA="$PG_HOME/data"
PGLOG="$PG_HOME/pgsql.log"
PGPORT=5432
DB_NAME=worldsim
APP_ROLE=worldsim
OBS_ROLE=obs_ro
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="${WSIM_ENV_FILE:-$(cd "$SCRIPT_DIR/../.." && pwd)/.env}"

export PATH="$PG_HOME/bin:$PATH"

log() { printf '[db_init] %s\n' "$*"; }
die() { log "ERROR: $*"; exit 1; }

[ -x "$PG_HOME/bin/postgres" ] || die "$PG_HOME 无 PostgreSQL，先跑 server/scripts/pg_build.sh（T-ENV-01/02）"

parse_app_password() { # 从 .env 的 WSIM_PG_DSN 解析应用角色密码（密码不入 git）
  [ -f "$ENV_FILE" ] || die ".env 不存在：$ENV_FILE（先完成 T-ENV-05）"
  local dsn
  dsn="$(grep -E '^WSIM_PG_DSN=' "$ENV_FILE" | tail -1 | cut -d= -f2-)"
  [ -n "$dsn" ] || die "$ENV_FILE 缺 WSIM_PG_DSN"
  APP_PASSWORD="$(printf '%s' "$dsn" | sed -E 's#^[a-zA-Z]+://[^:]+:([^@]+)@.*#\1#')"
  [ -n "$APP_PASSWORD" ] && [ "$APP_PASSWORD" != "$dsn" ] || die "WSIM_PG_DSN 未含密码（期望 postgresql://worldsim:<pwd>@127.0.0.1:5432/worldsim）"
}

is_running() { pg_ctl -D "$PGDATA" status >/dev/null 2>&1; }

do_start() {
  [ -f "$PGDATA/PG_VERSION" ] || die "未初始化：先跑 $0 init"
  if is_running; then log "already running（跳过 start）"; return 0; fi
  pg_ctl -D "$PGDATA" -l "$PGLOG" -w -t 60 start >/dev/null
  log "started（port=$PGPORT，socket=/tmp，日志 $PGLOG）"
}

do_stop() {
  if ! is_running; then log "not running（跳过 stop）"; return 0; fi
  pg_ctl -D "$PGDATA" -m fast -w stop >/dev/null
  log "stopped"
}

do_status() {
  if is_running; then pg_ctl -D "$PGDATA" status; else log "not running"; return 3; fi
}

do_init() {
  if [ -f "$PGDATA/PG_VERSION" ]; then
    log "already initialized: $PGDATA（跳过 initdb）"
  else
    mkdir -p "$PGDATA"
    initdb -D "$PGDATA" --locale=C.UTF-8 -E UTF8 > "$PG_HOME/initdb.log" 2>&1
    log "initdb done（locale C.UTF-8，日志 $PG_HOME/initdb.log）"
  fi

  local conf="$PGDATA/postgresql.conf"
  if grep -qE '^# worldsim conf done' "$conf"; then
    log "postgresql.conf 已配置（跳过）"
  else
    sed -i \
      -e "s/^#*port = .*/port = $PGPORT/" \
      -e "s/^#*listen_addresses = .*/listen_addresses = '127.0.0.1'/" \
      -e "s/^#*timezone = .*/timezone = 'Asia\/Shanghai'/" \
      "$conf"
    printf '# worldsim conf done\n' >> "$conf"
    log "postgresql.conf: port=$PGPORT / listen_addresses=127.0.0.1 / timezone=Asia/Shanghai"
  fi

  local hba="$PGDATA/pg_hba.conf"
  if grep -q '# worldsim: loopback scram' "$hba"; then
    log "pg_hba.conf 已配置（跳过）"
  else
    sed -i -E 's#^host([[:space:]]+)all([[:space:]]+)all([[:space:]]+)127\.0\.0\.1/32([[:space:]]+)[a-z-]+#host\1all\2all\3127.0.0.1/32\4scram-sha-256#' "$hba"
    printf '# worldsim: loopback scram（unix socket 保持 trust，§6 D2）\n' >> "$hba"
    log "pg_hba.conf: 127.0.0.1/32 → scram-sha-256"
  fi

  is_running || do_start

  # 角色：worldsim（应用，密码从 .env DSN 解析）、obs_ro（只读占位，授权归 T-DB-03）
  parse_app_password
  local role_exists
  role_exists="$(psql -h /tmp -d postgres -tAc "SELECT 1 FROM pg_roles WHERE rolname='$APP_ROLE'")"
  if [ "$role_exists" = "1" ]; then
    log "role $APP_ROLE 已存在（跳过；密码以 .env 为准不再覆写）"
  else
    psql -h /tmp -d postgres -v app_pwd="$APP_PASSWORD" >/dev/null <<'SQL'
CREATE ROLE worldsim LOGIN PASSWORD :'app_pwd'
SQL
    log "role $APP_ROLE created（LOGIN）"
  fi
  role_exists="$(psql -h /tmp -d postgres -tAc "SELECT 1 FROM pg_roles WHERE rolname='$OBS_ROLE'")"
  if [ "$role_exists" = "1" ]; then
    log "role $OBS_ROLE 已存在（跳过）"
  else
    local obs_pwd="obs_ro_disabled_$(head -c 12 /dev/urandom | od -An -tx1 | tr -d ' \n')"
    psql -h /tmp -d postgres -v obs_pwd="$obs_pwd" >/dev/null <<'SQL'
CREATE ROLE obs_ro LOGIN PASSWORD :'obs_pwd'
SQL
    log "role $OBS_ROLE created（LOGIN，随机密码占位，授权归 T-DB-03）"
  fi

  create_db
  log "init complete"
}

create_db() { # 建库 + 扩展（init 与 reset 共用）
  local db_exists
  db_exists="$(psql -h /tmp -d postgres -tAc "SELECT 1 FROM pg_database WHERE datname='$DB_NAME'")"
  if [ "$db_exists" = "1" ]; then
    log "database $DB_NAME 已存在（跳过 createdb）"
  else
    createdb -h /tmp -O "$APP_ROLE" "$DB_NAME"
    log "database $DB_NAME created（owner=$APP_ROLE；不建副本库，§2 defer T-SYN-06）"
  fi
  psql -h /tmp -d "$DB_NAME" -v ON_ERROR_STOP=1 \
    -c 'CREATE SCHEMA IF NOT EXISTS partman' \
    -c 'CREATE EXTENSION IF NOT EXISTS vector' \
    -c 'CREATE EXTENSION IF NOT EXISTS pg_partman SCHEMA partman' >/dev/null
  psql -h /tmp -d "$DB_NAME" -tAc "SELECT extname || ' ' || extversion FROM pg_extension WHERE extname IN ('vector','pg_partman') ORDER BY 1" \
    | while IFS= read -r line; do log "extension: $line"; done
}

do_reset() { # reset = drop + create database worldsim（供测试反复跑 DDL）
  is_running || do_start
  dropdb -h /tmp --if-exists --force "$DB_NAME"
  log "database $DB_NAME dropped"
  createdb -h /tmp -O "$APP_ROLE" "$DB_NAME"
  log "database $DB_NAME created（owner=$APP_ROLE）"
  psql -h /tmp -d "$DB_NAME" -v ON_ERROR_STOP=1 \
    -c 'CREATE SCHEMA IF NOT EXISTS partman' \
    -c 'CREATE EXTENSION IF NOT EXISTS vector' \
    -c 'CREATE EXTENSION IF NOT EXISTS pg_partman SCHEMA partman' >/dev/null
  log "extensions ready（vector / pg_partman）"
}

case "${1:-}" in
  init)   do_init ;;
  start)  do_start ;;
  stop)   do_stop ;;
  status) do_status ;;
  reset)  do_reset ;;
  *) echo "usage: $0 {init|start|stop|status|reset}" >&2; exit 64 ;;
esac
