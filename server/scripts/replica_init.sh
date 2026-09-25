#!/usr/bin/env bash
# T-SYN-06：副本库 worldsim_replica 创建唯一入口（07 文档 R1 §A.1：01 db_init.sh 不预留副本）。
# 幂等：建角色（worldsim_ingest / obs_ro 占位复用）→ 建库 → 跑 ddl/replica_v1.sql → ddl/replica_grants.sql
#       →（存在时）ddl/replica_digest_log.sql（T-SYN-09 D5 内部表）。
# 用法：bash server/scripts/replica_init.sh
set -euo pipefail

PG_HOME="$HOME/pgsql"
DB_NAME=worldsim_replica
INGEST_ROLE=worldsim_ingest
OBS_ROLE=obs_ro
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="${WSIM_ENV_FILE:-$(cd "$SCRIPT_DIR/../.." && pwd)/.env}"

export PATH="$PG_HOME/bin:$PATH"

log() { printf '[replica_init] %s\n' "$*"; }
die() { log "ERROR: $*"; exit 1; }

pg_isready -h /tmp -d postgres >/dev/null 2>&1 || die "PG 未运行（先 bash server/scripts/db_init.sh start）"

# 摄入写账号密码：从 .env 的 WSIM_REPLICA_PG_DSN 解析（密码不入 git；缺省 = 随机占位，开发期 socket trust 可连）
parse_ingest_password() {
  INGEST_PASSWORD=""
  if [ -f "$ENV_FILE" ]; then
    local dsn
    dsn="$(grep -E '^WSIM_REPLICA_PG_DSN=' "$ENV_FILE" | tail -1 | cut -d= -f2- || true)"
    if [ -n "$dsn" ]; then
      INGEST_PASSWORD="$(printf '%s' "$dsn" | sed -E 's#^[a-zA-Z]+://[^:]+:([^@]+)@.*#\1#')"
      [ "$INGEST_PASSWORD" = "$dsn" ] && INGEST_PASSWORD=""
    fi
  fi
  if [ -z "$INGEST_PASSWORD" ]; then
    INGEST_PASSWORD="ingest_disabled_$(head -c 12 /dev/urandom | od -An -tx1 | tr -d ' \n')"
    log "WSIM_REPLICA_PG_DSN 未配置密码 → $INGEST_ROLE 随机占位密码（开发期 socket trust 可连）"
  fi
}

ensure_role() { # ensure_role <name> <password>
  local exists
  exists="$(psql -h /tmp -d postgres -tAc "SELECT 1 FROM pg_roles WHERE rolname='$1'")"
  if [ "$exists" = "1" ]; then
    log "role $1 已存在（跳过；密码不再覆写）"
  else
    psql -h /tmp -d postgres -v rname="$1" -v rpwd="$2" >/dev/null <<'SQL'
CREATE ROLE :"rname" LOGIN PASSWORD :'rpwd'
SQL
    log "role $1 created（LOGIN）"
  fi
}

parse_ingest_password
ensure_role "$INGEST_ROLE" "$INGEST_PASSWORD"
# obs_ro 由 db_init.sh 创建（T-ENV-03）；缺失时随机占位兜底
if [ "$(psql -h /tmp -d postgres -tAc "SELECT 1 FROM pg_roles WHERE rolname='$OBS_ROLE'")" != "1" ]; then
  ensure_role "$OBS_ROLE" "obs_ro_disabled_$(head -c 12 /dev/urandom | od -An -tx1 | tr -d ' \n')"
fi

if [ "$(psql -h /tmp -d postgres -tAc "SELECT 1 FROM pg_database WHERE datname='$DB_NAME'")" = "1" ]; then
  log "database $DB_NAME 已存在（跳过 createdb，DDL 幂等重跑）"
else
  createdb -h /tmp -O "$INGEST_ROLE" "$DB_NAME"
  log "database $DB_NAME created（owner=$INGEST_ROLE）"
fi

psql -h /tmp -d "$DB_NAME" -v ON_ERROR_STOP=1 -f "$SCRIPT_DIR/../ddl/replica_v1.sql" >/dev/null
log "replica_v1.sql applied"
psql -h /tmp -d "$DB_NAME" -v ON_ERROR_STOP=1 -f "$SCRIPT_DIR/../ddl/replica_grants.sql" >/dev/null
log "replica_grants.sql applied（$INGEST_ROLE 写 / $OBS_ROLE 清单制只读 8 项）"
if [ -f "$SCRIPT_DIR/../ddl/replica_digest_log.sql" ]; then
  psql -h /tmp -d "$DB_NAME" -v ON_ERROR_STOP=1 -f "$SCRIPT_DIR/../ddl/replica_digest_log.sql" >/dev/null
  log "replica_digest_log.sql applied（T-SYN-09 D5 内部表，不授权 obs_ro）"
fi
if [ -f "$SCRIPT_DIR/../ddl/replica_obs_views.sql" ]; then
  psql -h /tmp -d "$DB_NAME" -v ON_ERROR_STOP=1 -f "$SCRIPT_DIR/../ddl/replica_obs_views.sql" >/dev/null
  log "replica_obs_views.sql applied（T-SYN-10 切换桥：obs schema 同名透传视图，07 D8）"
fi

psql -h /tmp -d "$DB_NAME" -c '\dt'
log "done"
