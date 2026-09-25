# WorldSim 备份/恢复 runbook（08 T-OPS-06；04 §12.2 主库宕机/损坏处置链）

> 备份：`deploy/backup.sh`（pg_dump custom → `~/pgsql/backups`，本地留 7 天，每日 04:10 timer）。
> RPO ≤1 真实日（04 §12.2）。云端 30 天推送 defer 上线部署期。

## 演练一：dump 恢复（主库损坏场景）

```bash
# 0) 停内核（与摄入/派生 worker），防写入
pkill -f 'worldsim.main'; pkill -f 'worldsim.ingest.derived.worker'

# 1) 恢复到最后一次备份（生产=原名库；演练用侧库）
~/pgsql/bin/pg_restore -h /tmp -d <目标库> --clean --if-exists ~/pgsql/backups/worldsim-<ts>.dump
#   注：分区表/扩展依赖（vector/pg_partman）随 dump 恢复；目标库须先有扩展（ddl 灌库同序）

# 2) 快照+事件流重放补齐（备份点之后时段；重放期零 LLM 调用，04 §5.3）
cd server && WSIM_REPLAY_MODE=replay uv run python scripts/replay_check.py --sim-day <N>

# 3) digest 校验（07 工具；副本在世时）
uv run python scripts/digest_check.py

# 4) 审计全绿
uv run python scripts/audit_run.py

# 5) 重启全栈
bash deploy/start_local.sh
```

## 演练二：副本回捞（主库丢失最近 N 个事件）

主库 append-only 触发器阻止 DELETE/UPDATE（04 §5.2）；"丢失"场景仅限灾难性数据页损坏/误删分区。
回捞路径（人工执行，标注校验点）：

```bash
# 1) 副本只读导出缺口段（obs_ro 无权写，导出用 superuser/摄入账号只读 SELECT）
~/pgsql/bin/psql -h /tmp -d worldsim_replica -c \
  "\copy (SELECT * FROM events WHERE seq > <主库 max(seq)>) TO '/tmp/repl_events.csv' CSV"

# 2) 主库侧回灌（superuser；payload 已是同步规范形——剥除过 internal 键，回灌后主库该段
#    不再有 text_raw 等内部键，属已知有损，仅限灾难恢复）
#    ※ events append-only 触发器需临时 bypass：SET session_replication_role='replica';
~/pgsql/bin/psql -h /tmp -d worldsim -c "SET session_replication_role='replica';
  \copy events FROM '/tmp/repl_events.csv' CSV"

# 3) 校验点：MAX(seq) 两端一致 + 审计①余额守恒绿 + digest 复检
uv run python scripts/digest_check.py && uv run python scripts/audit_run.py --only balance_conservation
```

## 演练记录（实操留痕，T-OPS-06 验收 1~3）

**演练一（dump 恢复，2026-09-26 03:17）**：`bash deploy/backup.sh worldsim` →
`worldsim-20260926-031738.dump`（13MB，pg_restore --list 可读）；恢复至侧库
`worldsim_bk_restore`（先建 vector/pg_partman 扩展）耗时 **3.3s**（2468 事件小库；R6 回填：
RPO ≤1 真实日在当前数据量级可达成，大库需按量复测）；恢复后 `events` count/max(seq)
2468/2468 与主库一致，`audit_run.py --dsn <侧库>` 六项全绿 exit=0。

**演练二（副本回捞，2026-09-26 04:2x，scratch 对 worldsim_drill/_replica）**：
断网演练完成后两端一致（max(seq)=35320）；模拟"主库丢失最近 100 事件"
（scratch 库 superuser `SET session_replication_role='replica'` 绕过 append-only 触发器删除）
→ 副本 `\copy` 导出缺口段（100 行）→ 主库同会话 SET+copy 回灌 → 两端
max(seq)=35320 恢复一致，审计①余额守恒绿。
已知口径注记：① 回灌段 payload 为同步规范形（无 text_raw 等内部键，灾难恢复的有损边界）；
② 演练库事件为合成负载（绕过结算总线直插），审计⑥缓存对账在该库红 3867 条属
合成数据伪差（非产品缺陷——内核写入路径全自动结算，06 项在真跑库绿）。
