#!/usr/bin/env bash
# T-ENV-01 / T-ENV-02：PostgreSQL 16 + pgvector 0.8 + pg_partman 无 root 源码编译装入 ~/pgsql
# 钉版变量实测回填见 docs/tasks/01-脚手架与数据库.md §5 R6。
# 幂等：目标版本已装则跳过对应阶段并输出 "already installed"。全程无 root/sudo。
set -euo pipefail

PG_VERSION=16.15
PGVECTOR_VERSION=0.8.6
PG_PARTMAN_VERSION=5.5.0

PREFIX="$HOME/pgsql"
BUILD_DIR="$PREFIX/build"
SRC_DIR="$BUILD_DIR/src"
# 日志直接落 $BUILD_DIR/*.log（验收口径 grep -iE 'bison|flex' ~/pgsql/build/*.log）

PG_MIRRORS=(
  "https://mirrors.aliyun.com/postgresql/source/v${PG_VERSION}"
  "https://ftp.postgresql.org/pub/source/v${PG_VERSION}"
)
GH_PREFIXES=(
  "https://github.com"
  "https://mirror.ghproxy.com/https://github.com"
)

log() { printf '[pg_build] %s\n' "$*"; }

fetch() { # fetch <output> <url>...
  local out="$1"; shift
  if [ -s "$out" ]; then log "reuse $out"; return 0; fi
  local url
  for url in "$@"; do
    log "download $url"
    if curl -fL --connect-timeout 15 --max-time 900 -o "$out.part" "$url"; then
      mv "$out.part" "$out"; return 0
    fi
    log "mirror failed: $url"
  done
  log "ERROR: all mirrors failed for $out"; return 1
}

phase1_pg() {
  if [ -x "$PREFIX/bin/postgres" ] && "$PREFIX/bin/postgres" --version | grep -qE "^postgres \(PostgreSQL\) ${PG_VERSION}\b"; then
    log "PostgreSQL ${PG_VERSION} already installed"
    return 0
  fi
  mkdir -p "$SRC_DIR"
  local tarball="$SRC_DIR/postgresql-${PG_VERSION}.tar.bz2"
  local urls=()
  local m; for m in "${PG_MIRRORS[@]}"; do urls+=("${m}/postgresql-${PG_VERSION}.tar.bz2"); done
  fetch "$tarball" "${urls[@]}"

  local src="$SRC_DIR/postgresql-${PG_VERSION}"
  rm -rf "$src"
  tar -xjf "$tarball" -C "$SRC_DIR"

  cd "$src"
  log "configure (prefix=$PREFIX, --without-readline --without-icu)"
  # --without-readline：本机无 readline-dev（任务书 T-ENV-01 口径）；
  # --without-icu：本机无 root 装不了 libicu-dev（偏差：configure 默认 --with-icu；
  # 影响仅是 ICU collation 不可用，initdb locale C.UTF-8 走 libc 不受影响）。
  ./configure --prefix="$PREFIX" --without-readline --without-icu > "$BUILD_DIR/pg_configure.log" 2>&1
  log "make -j$(nproc)"
  make -j"$(nproc)" > "$BUILD_DIR/pg_make.log" 2>&1
  make install > "$BUILD_DIR/pg_install.log" 2>&1
  log "PostgreSQL installed: $("$PREFIX/bin/postgres" --version)"
}

build_ext() { # build_ext <name> <version> <tarball-url...>
  local name="$1" ver="$2"; shift 2
  local tarball="$SRC_DIR/${name}-${ver}.tar.gz"
  fetch "$tarball" "$@"
  local src="$SRC_DIR/${name}-${ver}"
  rm -rf "$src"
  tar -xzf "$tarball" -C "$SRC_DIR"
  cd "$src"
  log "make ${name} ${ver} (PG_CONFIG=$PREFIX/bin/pg_config)"
  make PG_CONFIG="$PREFIX/bin/pg_config" > "$BUILD_DIR/${name}_make.log" 2>&1
  make PG_CONFIG="$PREFIX/bin/pg_config" install > "$BUILD_DIR/${name}_install.log" 2>&1
}

gh_tarballs() { # gh_tarballs <repo> <tag>
  local p; for p in "${GH_PREFIXES[@]}"; do echo "${p}/$1/archive/refs/tags/$2.tar.gz"; done
}

phase2_ext() {
  if [ -f "$PREFIX/share/extension/vector.control" ] && grep -q "^default_version = '${PGVECTOR_VERSION}'" "$PREFIX/share/extension/vector.control"; then
    log "pgvector ${PGVECTOR_VERSION} already installed"
  else
    mkdir -p "$SRC_DIR"
    # shellcheck disable=SC2046
    build_ext "pgvector" "$PGVECTOR_VERSION" $(gh_tarballs pgvector/pgvector "v${PGVECTOR_VERSION}")
    log "pgvector installed: $(grep '^default_version' "$PREFIX/share/extension/vector.control")"
  fi

  if [ -f "$PREFIX/share/extension/pg_partman.control" ] && grep -q "^default_version = '${PG_PARTMAN_VERSION}'" "$PREFIX/share/extension/pg_partman.control"; then
    log "pg_partman ${PG_PARTMAN_VERSION} already installed"
  else
    mkdir -p "$SRC_DIR"
    # shellcheck disable=SC2046
    build_ext "pg_partman" "$PG_PARTMAN_VERSION" $(gh_tarballs pgpartman/pg_partman "v${PG_PARTMAN_VERSION}")
    log "pg_partman installed: $(grep '^default_version' "$PREFIX/share/extension/pg_partman.control")"
  fi
}

log "== phase 1: PostgreSQL ${PG_VERSION} -> ${PREFIX}"
phase1_pg
log "== phase 2: pgvector ${PGVECTOR_VERSION} + pg_partman ${PG_PARTMAN_VERSION}"
phase2_ext
log "done. 验证 SQL：CREATE EXTENSION vector; CREATE EXTENSION pg_partman;"
