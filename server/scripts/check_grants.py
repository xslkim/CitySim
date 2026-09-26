#!/usr/bin/env python3
"""增量 DDL 授权登记与一致性校验（T-ITER2-02，round2 #2①④）。

背景：schema_v1.sql 之外的增量 DDL 文件各带 GRANT 语句（health_daily_v1.sql 的表级
SELECT/INSERT/UPDATE、memories_update_grant.sql 的列级 UPDATE(archived)）。reset/seed 重放
（db_init.sh reset → schema_v1.sql → seed_8.sql）不会自动带上这些增量文件——历史上因此两次
日界崩溃（round1 health_daily、round2 memories），内核 MTBF ≈ 1 模拟日。

本脚本持有**唯一授权登记表**（GRANT_REGISTRY，代码内登记 = 设计文档口径）；三种用法：
  --check        纯校验：information_schema 实际授权 vs 登记表，输出 diff，不一致退出码 1（CI/启动自检）
  --apply        幂等补授：按登记表重放 GRANT 语句后复核，仍不一致退出码 1（reset/seed 流程末尾/启动自愈）
  （缺省）       先 --check，有 diff 自动 --apply 再复核（人工随手跑的自愈形态）

连接：缺省经 unix socket（/tmp）以本机 OS 用户（superuser）连 worldsim 库——GRANT 需要属主/
superuser 权限，不能用 worldsim 应用角色执行；--dsn 可覆盖（如指向测试库）。
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from dataclasses import dataclass

PG_BIN = os.path.expanduser("~/pgsql/bin")
SOCKET_DIR = "/tmp"
APP_ROLE = "worldsim"


@dataclass(frozen=True)
class GrantSpec:
    """一条登记授权：表级（column=None）或列级授权。"""

    source: str          # 增量 DDL 文件（出处登记；本脚本不解析文件，GRANT 由登记表生成）
    table: str
    privileges: tuple[str, ...]
    column: str | None = None

    def grant_sql(self) -> str:
        col = f" ({self.column})" if self.column else ""
        return f'GRANT {", ".join(self.privileges)}{col} ON {self.table} TO {APP_ROLE};'


# 唯一登记表：新增增量 DDL 授权必须先在此登记（= 设计文档口径），再执行 --apply。
# （round2 教训：第 3 张漏授权表出现时应在此报错级缺位——check 对"登记表内每项"逐项核对。）
GRANT_REGISTRY: tuple[GrantSpec, ...] = (
    GrantSpec("ddl/health_daily_v1.sql", "health_daily", ("SELECT", "INSERT", "UPDATE")),
    GrantSpec("ddl/memories_update_grant.sql", "memories", ("UPDATE",), column="archived"),
)


def _psql(db: str, sql: str) -> str:
    out = subprocess.run(
        [f"{PG_BIN}/psql", "-h", SOCKET_DIR, "-d", db, "-v", "ON_ERROR_STOP=1", "-tAc", sql],
        capture_output=True, text=True, shell=False,
    )
    if out.returncode != 0:
        raise RuntimeError(f"psql 失败：{out.stderr.strip()}\nSQL: {sql}")
    return out.stdout


def actual_grants(db: str) -> set[GrantSpec]:
    """information_schema 实际授权 → 与登记表同形集合（只看登记表相关表，避免基线噪声）。"""
    actual: set[GrantSpec] = set()
    tables = sorted({s.table for s in GRANT_REGISTRY})
    table_csv = ",".join(f"'{t}'" for t in tables)
    rows = _psql(
        db,
        "SELECT table_name, privilege_type FROM information_schema.role_table_grants "
        f"WHERE grantee='{APP_ROLE}' AND table_schema='public' AND table_name IN ({table_csv})",
    )
    for line in rows.splitlines():
        if not line.strip():
            continue
        table, priv = (x.strip() for x in line.split("|"))
        actual.add(GrantSpec("", table, (priv,)))
    rows = _psql(
        db,
        "SELECT table_name, column_name, privilege_type FROM information_schema.column_privileges "
        f"WHERE grantee='{APP_ROLE}' AND table_schema='public' AND table_name IN ({table_csv})",
    )
    for line in rows.splitlines():
        if not line.strip():
            continue
        table, col, priv = (x.strip() for x in line.split("|"))
        actual.add(GrantSpec("", table, (priv,), column=col))
    return actual


def compute_diff(expected: tuple[GrantSpec, ...], actual: set[GrantSpec]) -> tuple[list[GrantSpec], list[GrantSpec]]:
    """(缺失, 多余)：按 (table, column, privilege) 粒度核对。"""
    actual_keys = {(a.table, a.column, p) for a in actual for p in a.privileges}
    missing = [GrantSpec(s.source, s.table, (p,), s.column)
               for s in expected for p in s.privileges
               if (s.table, s.column, p) not in actual_keys]
    expected_keys = {(s.table, s.column, p) for s in expected for p in s.privileges}
    extra = [GrantSpec("", a.table, (p,), a.column)
             for a in actual for p in a.privileges
             if (a.table, a.column, p) not in expected_keys]
    return missing, extra


def apply_grants(db: str) -> None:
    sql = "\n".join(s.grant_sql() for s in GRANT_REGISTRY)
    _psql(db, sql)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="增量 DDL 授权一致性校验/补授（T-ITER2-02）")
    ap.add_argument("--db", default="worldsim", help="数据库名（缺省 worldsim；测试库传 worldsim_test 等）")
    ap.add_argument("--check", action="store_true", help="纯校验不补授（CI/启动自检口径）")
    ap.add_argument("--apply", action="store_true", help="幂等补授后复核（reset/seed 流程口径；与缺省形态等价）")
    ap.add_argument("--verbose", action="store_true", help="打印基线授权提示行（缺省静默，防启动日志噪音）")
    args = ap.parse_args(argv)

    def report(missing: list[GrantSpec], extra: list[GrantSpec]) -> None:
        for s in missing:
            print(f"[check_grants] 缺失：{s.table}{'(' + s.column + ')' if s.column else ''} "
                  f"{','.join(s.privileges)}（出处 {s.source}）")
        if args.verbose:
            for s in extra:
                print(f"[check_grants] 提示（不影响通过）：{s.table}{'(' + s.column + ')' if s.column else ''} "
                      f"{','.join(s.privileges)} 为 schema_v1.sql 基线授权，不在增量登记表内")

    missing, extra = compute_diff(GRANT_REGISTRY, actual_grants(args.db))
    if args.check:  # 纯校验（CI/启动自检口径）：登记项缺失即退出码 1
        report(missing, extra)
        if not missing:
            print(f"[check_grants] OK：{len(GRANT_REGISTRY)} 项登记授权全部在位（diff=0）")
            return 0
        return 1
    # --apply 或缺省自愈形态：幂等补授登记项后复核（基线授权不参与判定）
    if missing:
        report(missing, extra)
        print("[check_grants] 自动补授登记表 GRANT …")
        apply_grants(args.db)
    missing, _ = compute_diff(GRANT_REGISTRY, actual_grants(args.db))
    if not missing:
        print("[check_grants] OK：登记授权一致（diff=0）")
        return 0
    report(missing, [])
    print("[check_grants] 补授后仍缺失，授权问题须在 seed 阶段解决，拒绝带伤启动")
    return 1


if __name__ == "__main__":
    sys.exit(main())
