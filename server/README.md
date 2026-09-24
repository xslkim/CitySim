# WorldSim server

Python 3.12（uv 管理）内核项目：模拟内核 + LLM 网关 + 摄入 API（M6）+ 观察端 API（M4）。
包结构按 `docs/design/04-服务器详细设计.md` §2.1，M0 仅骨架（各包仅 `__init__.py`），实现随里程碑填入。

## 约定（docs/tasks/00-总览与里程碑.md §1 A14）

- 一切 Python 执行走 `cd server && uv run ...`，禁止裸 `python`/`python3`。
- 环境变量见仓库根 `.env`（唯一清单 = 00 §2 附表）；配置文件见 `config/`。
- 数据库：`server/scripts/db_init.sh {init|start|stop|status|reset}`（PG 16.15 源码装于 `~/pgsql`，socket `/tmp` + `127.0.0.1:5432`）。

## 常用命令

```bash
cd server
uv sync                              # 安装依赖（含 dev 组）
uv run pytest                        # 全量测试（importmode=importlib）
uv run python scripts/gen_event_types.py        # 派生注册表下游镜像（T-CFG-05）
uv run python scripts/gen_event_types.py --check # 漂移检查（纯消费方 04 T-WA-10）
```

## 内核主循环（M1：04 §2.2 装配；唯一裁决协程写库）

```bash
cd server
uv run python -m worldsim.main --sim-hours 1     # 试跑模式：推进 1 模拟小时后优雅停机（退出码 0）
uv run python -m worldsim.main                   # 持续模式：按变速表 paced 跑至 SIGINT/SIGTERM
uv run python -m worldsim.main --sim-hours 2 --ratio 6   # 试跑 + 覆盖压缩比
```

- 装配：连接池 → `TimeEngine`(clock.anchor 锚点，seed 占位首启重锚) → `AdjudicationQueue` →
  `LLMGateway`（M1 注入 `MockProvider`，`default_provider='mock'`）→ `Pipeline`（六步骨架，
  think/move 轻动作闭环）→ `asyncio.TaskGroup`（clock.run / adjudication_loop 唯一写协程 / watch）。
- 试跑模式（`--sim-hours`）：unthrottled 尽快递 tick（仍 sim 网格锚定、逐 tick 串行落库），
  不做段切换与 batch 段自动进入；持续模式全量生效（段切换/batch 段/SIGHUP 热更）。
- env：`WSIM_PG_DSN`（必填；根 `.env` 兜底加载，仅补缺 WSIM_*）/ `WSIM_SPEED_TABLE` /
  `WSIM_MODELS_CONFIG` / `WSIM_WORLD_CONFIG` / `WSIM_REPLAY_MODE`（=replay 时本入口拒跑，退出码 2）。
- rng_seed = 本 tick 序号（events.rng_seed 规则骰子口径）；同 rng_seed 重跑同批 tick 产出逐字节一致
  （mock 确定性回归见 `tests/adjudicator/test_pipeline.py::test_pipeline_injected_mock_deterministic`）。

## 数据库（M0b：Schema v1 已冻结，tag `schema-v1`）

```bash
# 冷启动序列（09 §8 口径的 M0 段）
server/scripts/db_init.sh reset
psql "$WSIM_PG_DSN" -v ON_ERROR_STOP=1 -f server/ddl/schema_v1.sql    # 11 表 + append-only + 周分区
psql "$WSIM_PG_DSN" -v ON_ERROR_STOP=1 -f server/ddl/obs_views_v1.sql # obs 白名单视图（M0 最小集）
psql "$WSIM_PG_DSN" -v ON_ERROR_STOP=1 -f server/ddl/seed_8.sql       # 8 人小世界（seed_40.sql 供 M6）

uv run python scripts/seed.py --ids A01..A08 --out ddl/seed_8.sql   # 确定性再生成（同输入同输出）
uv run python scripts/seed.py --ids A01..A40 --out ddl/seed_40.sql
bash server/scripts/schema_freeze_check.sh                          # 06 §4.5 差集检查（契约变更必跑）
```

- `ddl/schema_v1.sql`：04 §5.2 逐字口径（events append-only 触发器 + REVOKE 双保险；agents id
  TEXT `'A01'~'A40'`；memories `vector(1024)` hnsw；不设事件类型 CHECK）；pg_partman 周分区
  （premake=4，`partman.run_maintenance_proc()` 滚动建区，运维归 08）。
- `ddl/obs_views_v1.sql`：obs schema + `obs.filter_payload()`（全项目唯一）+ `obs.events` /
  `obs.memory_projection` 视图 + `obs.payload_key_whitelist`（种子为生成物，勿手改）；obs_ro 只读。
- world_state 初始键：`clock.anchor`（2026-10-12 周一 00:00+08 冷启动占位，内核首启重锚）、
  `economy.stocks`（3 标的初值）、`economy.salary`（每人月薪抽定，M3 payroll 读取）。
- 测试库：pytest fixture 自动建/毁 `worldsim_test` / `worldsim_seed8` / `worldsim_seed40`（socket trust）。
