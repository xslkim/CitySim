#!/usr/bin/env bash
# T-OPS-06 主库备份（04 §12.1：pg_dump custom 每真实日 04:10；本地留 7 天；云端 30 天 defer 上线）。
# 路径写死 ~/pgsql/backups（与 09 §7 E10 同口径，不新增 env，R2 §A.8）。
# 用法：bash deploy/backup.sh [主库名（默认 worldsim）]
set -euo pipefail

PG_BIN="$HOME/pgsql/bin"
BACKUP_DIR="$HOME/pgsql/backups"
DB="${1:-worldsim}"
KEEP_DAYS=7

mkdir -p "$BACKUP_DIR"
ts="$(date +%Y%m%d-%H%M%S)"
out="$BACKUP_DIR/${DB}-${ts}.dump"

"$PG_BIN/pg_isready" -h /tmp -d "$DB" >/dev/null
"$PG_BIN/pg_dump" --format=custom --file="$out" -h /tmp "$DB"
"$PG_BIN/pg_restore" --list "$out" >/dev/null   # 可读性自检（验收 1）
find "$BACKUP_DIR" -name "${DB}-*.dump" -mtime +"$KEEP_DAYS" -delete
printf '[backup] %s → %s（%s，本地保留 %d 天）\n' "$DB" "$out" \
  "$(du -h "$out" | cut -f1)" "$KEEP_DAYS"
