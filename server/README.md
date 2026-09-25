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
uv run python scripts/invite_stats.py           # 邀约接受率统计（01 §3.5 / 02 D6 口径）
```

## 配置文件（config/）

M0 族（speed_table/models/agents/world/event_types/payload_whitelist/health_thresholds）之外，
波次 2a 新增（02 文档偏差表 D13 登记）：`needs.yaml`（01 §3.1 六需求衰减/满足镜像）、
`relations.yaml`（01 §3.2 关系矩阵 + 自然回归 / §3.4 意图冷却 / §3.5 willingness 镜像）、
`goals.yaml`（01 §3.3 周目标库 36 条，已入 00 §2 清单）、`topics.yaml`（01 §7 话题库 60 条）。
另：`ddl/memories_update_grant.sql` 为 T-MEM-03 归档治理按列授权 `UPDATE (archived)`（已灌主库）。

## 波次 2a 已接线的内核件（M1 记忆+关系线）

- 管道挂接：`Pipeline(retrieve=make_retrieve_hook(...), reflect=reflector.hook)`（step2/step6）。
- `adjudication_loop(after_tick=...)`：每裁决点跑全员需求衰减（T-REL-01）→ 聚合事件 flush
  （`state.needs_delta`/`relation.changed` 每 tick ≤2 条，04 §6.5）→ 23:00 每日兜底反思（T-MEM-02）。
- batch 段钩子（唯一挂载点 T-TIME-03）：`memory.merge`（T-MEM-03 摘要合并）、
  `kernel.calendar`（周界：目标刷新 T-REL-03 + 关系周回归 T-REL-02；日界：挫败值日恢复）。
- `hygiene_loop` 协程入 TaskGroup（日界归档，04 §2.2）。
- 邀约/话题（T-REL-05/06）交付为引擎 + 函数接口（`worldsim/invite/`、`worldsim/relations/topics.py`），
  管道动作侧接线归波次 2b（T-ADJ-03/04）。

## 波次 2b 已接线的内核件（M1 收口）

- LOD 调度（T-LOD-01~04）：`scheduler/lod.py`（三层统一接口 + `LodScheduler.collect_due` 时钟兜底写回
  `agents.next_due_sim`）；`scheduler/rotation.py`（路径二事件驱动升格 ≤16+LRU 挤出 + 路径一基尼轮换
  席位互换 + 升星追赶反思）；`scheduler/residence.py`（校外 NPC 驻留规则引擎，零 LLM，after_tick 评估）。
- 19 动作校验器（T-ADJ-03）：`adjudicator/validators.py` 收编 step5 统一校验段（通用规则 5 条 +
  逐动作前置校验 + 结算总线 + `debts` 写入/核销），经 `Pipeline(validate=, settler=)` 注入。
- 对话（T-ADJ-04）：`adjudicator/dialogue.py` 整段单事件落库、`lines[].at_offset_s` 写死、话题注入
  （T-REL-06 select/兜底/trigger_point 强制切入）、降速读取点 `ThrottleState.dialogue_daily_cap`。
- grade（T-ADJ-07）：`adjudicator/grade.py` 为 `ui.grade` 初值唯一写入处（落库前信号预申报口径 D30），
  `effective_grade()` = 初值 ⊕ 最新复核（M3 复核事件消费位）。
- 回放（T-ADJ-08）：`WSIM_REPLAY_MODE=replay uv run python scripts/replay_check.py --sim-day N`——
  seed 确定性基线 + 事件流重建（needs/mood/relations/position）逐项对账，零 LLM 调用。
- 快照（T-ADJ-09）：`snapshot/dump.py`（batch 钩子 `kernel.snapshot`，每模拟日落 `var/snapshot/`）+
  `snapshot/canonical.py`（state/events 两侧 canonical+digest 唯一实现，07 文档消费方同 import）+
  `scripts/canonical.py --sim-day N [--expect sha256:…]` 对账 CLI。

## 内核主循环（M1：04 §2.2 装配；唯一裁决协程写库）

```bash
cd server
uv run python -m worldsim.main --sim-hours 1     # 试跑模式：推进 1 模拟小时后优雅停机（退出码 0）
uv run python -m worldsim.main                   # 持续模式：按变速表 paced 跑至 SIGINT/SIGTERM
uv run python -m worldsim.main --sim-hours 2 --ratio 6   # 试跑 + 覆盖压缩比
uv run python -m worldsim.main --sim-hours 24 --llm routed   # M2 真接入：models.yaml 路由（智谱免费档 + 本地 bge-m3）+ RPM 桶/降级链/熔断
```

- 装配：连接池 → `TimeEngine`(clock.anchor 锚点，seed 占位首启重锚) → `AdjudicationQueue` →
  `LLMGateway`（`--llm mock` 默认注入 `MockProvider`；`--llm routed` 走 `ModelRouter` 真路由 +
  `FailoverBreaker` 撞墙降级链 + SIGHUP 热更，T-LLM-04/05/06）→ `Pipeline`（六步骨架，
  think/move 轻动作闭环）→ `asyncio.TaskGroup`（clock.run / adjudication_loop 唯一写协程 / watch）。
- LLM 网关 M2 模块：`llm_gateway/{router,clients,breaker,ledger,parse,prompts}.py` +
  `providers/{mock,zhipu,local_embed}.py`；模板族 `config/prompts/*.yaml`（12 族）、
  输出 schema `config/schemas/*.json`、安全词表 `config/safety/wordlist.txt`；
  计量 SQL `scripts/llm_cost.sql`；占位扫描 `scripts/check_config_fill.py`。
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
