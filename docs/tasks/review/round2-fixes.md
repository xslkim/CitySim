# 任务文档评审 · 第 2 轮（2026-09-23）修复指令

> R2 三视角报告已并入本文并裁定。修复口径同 R1：**fixer 只改自己负责的文档**；设计侧回登由仲裁者执行（§C）。引用 R1 裁定写作 `R1 §A.x`，引用 00 适配表写作 `00 §1 Ax`——两者编号独立。

## A. 全局裁定

1. **A 编号双套**：00 §1 表头加注"本表编号 A1~A17 与 round1-fixes §A 独立，引用须带前缀"；03/04/05/07/08 头部修订说明中的裸 `(A5)/(A9/A10)/(A20)` 等一律补前缀 `R1 §A.`。数字引用纪律（R1 §A.20）正式落入 00 §7 DoD 第 6 条。
2. **阈值配置载体唯一 = `config/models.yaml` 的 `thresholds:` 段**（01 T-CFG-02）：熔断倍数、干预率上限、反思阈值等都从它读；**不进 env**。03 T-LLM-08 设计依据删 `WSIM_COST_ALARM_RATIO/WSIM_COST_THROTTLE_RATIO`；04 T-DIR-03 删 `WSIM_INTERVENTION_RATE_CAP` env 读法改 thresholds。04 §1.6 设计侧残留两 env 名由仲裁者标注作废。00 §2 附表不补登这三项。
3. **pytest 测试文件 basename 全库唯一**（入 00 §7 DoD）。改名表（唯一权威）：
   - 01 `tests/test_speed_table.py` → `tests/config/test_speed_table_config.py`
   - 04 T-WA-01 `tests/world_agent/test_world_config.py` → `test_world_config_wa.py`
   - 05 T-WEB-01 `tests/observe/test_obs_views.py` → `test_obs_views_derived.py`
   - 07 T-SYN-09 一对 → `tests/sync/test_digest_push.py` + `tests/ingest/test_digest_ingest.py`
   - 07 `tests/sync/test_catchup.py` → `test_sync_catchup.py`（02 侧不动）
   - 07 `tests/derived/test_grade.py` → `test_grade_derived.py`（02 侧不动）
   - 01 T-ENV-04 补：pyproject `[tool.pytest.ini_options] importmode="importlib"`。
4. **M0 E7 闸门修正**：M0 不做 bge-m3 真加载（无依赖无代码）；09 M0 E7 改为静态一致性检查（`models.yaml` embed 配置 = `local`/bge-m3/1024 且与 DDL `vector(1024)` 一致，pytest 对拍）。**bge-m3 真加载冒烟并入 09 M2 E1 段**（T-LLM-03 验收已有同款）。01 R2 行文的"09 M1 E7"错挂改 M0/M2 口径。
5. **env 取值三方对齐**（唯一清单 = 00 §2 附表）：`WSIM_EMBED_BACKEND = local|cloud`（默认 local）；`WSIM_REPLAY_MODE = off|replay`（默认 off）。01 T-ENV-05 行 129 回改；01 §7 疑点 5/6 销项。
6. **M6 sync env 追加动作归 07 T-SYN-03**（交付物补：根 `.env.example` 追加 `WSIM_REPLICA_PG_DSN/WSIM_INGEST_BIND/WSIM_SYNC_FRAME_DUMP/WSIM_DRILL_OUTAGE_S/WSIM_CLOUD_INGEST_HTTPS` + 开发期 loopback 值的 `WSIM_CLOUD_INGEST_URL/WSIM_INGEST_TOKEN`）；00 §2 附表创建任务列改 T-SYN-03，部署期行改"开发期 loopback 由 T-SYN-03 追加"（仲裁者已改）。
7. **canonical/digest 与快照白名单子集构造提前到 M1**，唯一实现归 02 T-ADJ-09（`worldsim/snapshot/canonical.py`）；07 T-SYN-01/02 改消费方（T-SYN-02 写明快照构造复用 T-ADJ-09 生成函数，禁第二实现）。
8. **vLLM provider 与 GPU 预算表统一 defer 到上线部署期**（非 M6）：00 §1 A3 改注（仲裁者已改）；01/03/07 defer 表对齐措辞；09 M6 不列 vLLM 条款（保持现状即可）。
9. **willingness（01 §3.5 意愿分）供给侧归 02 T-REL-05**（邀约链计算点），T-ADJ-04 注入 prompt 变量既有、03 T-LLM-09 消费既有——02 补计算实现要点。
10. **校外 NPC 驻留调度**：02 新增 **T-LOD-04 背景 NPC 驻留规则引擎**（01 §1.6：18:30~8:00/周末移位 `home.<id>`、赴约 30~45min 折算、校外邀约地点限制；规则驱动零 LLM；05 T-WEB-13 offsite 角标依赖本任务）。
11. **04 §13 W2 最小观察原型取消**（登记 00 §1 A18，仲裁者已加）：其调试职能由 M4 观察端提前覆盖（M1~M3 调试走 SQL/日志/replay_check）。
12. **"待 00 §1 登记"收口**：各文档偏差表中状态为"待 00 §1 登记"的条目，全局批量改"已登记（本文偏差表）"——00 §1 只收全局条目，模块级工程默认的登记处即本文偏差表（00 §1 表头加注，仲裁者已加）。
13. **`portraits.ts` 生成链**：06 T-ART-02 交付物补"同脚本派生 `web/src/lib/portraits.ts`（`--check` 覆盖）"。
14. **`smoke.sh m3` 段**：08 T-OPS-05 补实现要点与验收条"接收 04 T-DIR-06 的 m3 段需求（封装 `tests/integration/test_m3_sim_week.py` 为 smoke 子命令）"；04 T-DIR-06 验收 5 的 `bash scripts/smoke.sh m3` 改 `bash server/scripts/smoke.sh m3`。
15. **09 §8 step④ zod 校验执行口径**：05 T-WEB-09 交付 `web/scripts/proto_check.mjs`（读 stdin JSON 做 zod 校验，`pnpm proto:check` 注册）；smoke step④ = curl 出响应管道给它；08 T-OPS-05 对应步骤引用。
16. **09 §8 step⑤ 截图 URL 写死**（仲裁者已改 09）：admin `http://127.0.0.1:8080/map`、lite `/lite/home`、stream `/stream/?token=<dev token>`；前置步"05 T-WEB-02 tokens_cli 签发 dev token"。
17. **观测端 obs_refresh 常驻段**：start_local.sh 形态 = PG + 内核 + obs_refresh 常驻 + obs-api + web dev server；05 修订说明③与 09 §8 step① 形态描述同步（仲裁者已改 09）。
18. **debt 方向断言统一**：debts 表语义写明"债权人 a_id → 债务人 b_id"（01 §2.2 韩彻欠林晚 ¥4,000 即 a=林晚 b=韩彻）；01 T-DB-04 两处行文对齐。

## B. 各文档修复清单

### 01（fixer-01）
- T-ENV-05：`WSIM_EMBED_BACKEND` 取值改 `local|cloud`（A5）；§7 疑点 5/6 销项。
- T-ENV-04：pyproject 补 `importmode="importlib"`（A3）。
- T-CFG-01 测试改名 `tests/config/test_speed_table_config.py`（A3）。
- T-DB-03 行文"四实体表"改"三实体表 + `health_daily` VIEW"（M4 增量口径）。
- T-DB-04 debts 方向按 A18 对齐（验收断言与 §2.2 一致）。
- R2 行"M1 E7"错挂改 M0/M2 口径（A4）。
- 偏差表"待 00 §1 登记"批量改"已登记（本文偏差表）"（A12）。
- defer 表 vLLM 行改"上线部署期"（A8）。

### 02（fixer-02）
- 新增 T-LOD-04（A10，00 §6 模板写全，设计依据 01 §1.6）。
- T-REL-05 补 willingness 计算点（A9，01 §3.5）；接受率统计 SQL 定名 `server/scripts/invite_stats.py`。
- T-ADJ-09 交付物补 `worldsim/snapshot/canonical.py`（canonical+digest 唯一实现，A7）；偏差表补 `snapshot.*` 键族条目；D1 mock 行对齐。
- T-ADJ-02/T-MEM-02 等"待 00 §1 登记"批量改（A12）。
- 验收命令三处改 `cd server && uv run python ...`（02:89/406/438 区域，-c 内路径去 `server/` 前缀）。
- D11 补一句"72h = 72 模拟小时"口径说明。
- `40~60% 先验`行文去值留引用（"见 06 §3，持有方源方案 §4.7"）。
- 72h 冷却等数值确认引 01 §7（行文不写死处不动）。

### 03（fixer-03）
- 头部修订说明裸 A 编号补 `R1 §A.` 前缀（A1）。
- T-LLM-08 设计依据删两 env 名，改"熔断倍数载体 = models.yaml `thresholds:` 段（01 T-CFG-02）；04 §1.6 两 env 名已作废（设计侧回登）"（A2）。
- defer 表 vLLM 行改"上线部署期"（A8）。
- 偏差表"待 00 §1 登记"批量改（A12）。

### 04（fixer-04）
- 头部修订说明裸 A 编号补前缀（A1）。
- T-DIR-03 删 `WSIM_INTERVENTION_RATE_CAP` env 读法，改 models.yaml `thresholds:`（A2）；与审计④同数据源口径写明。
- T-DIR-01/T-DIR-03 依赖环说明："代码单向 DIR-01→DIR-03 调用，预算数据经 world_state 共享"；T-WA-04 头部依赖补 T-DIR-03。
- T-DIR-04 grade 阈值段追加 world.yaml 的划界声明（01 T-CFG-03 划界补"director.grade 阈值段归 T-DIR-04 追加"——01 侧由 fixer-01 同步）。
- T-DIR-06 验收 5 路径补 `server/` 前缀（A14）。
- D-21 销项（04 §5.3 已统一 `WSIM_REPLAY_MODE`）。
- 偏差表"待 00 §1 登记"批量改（A12）。

### 05（fixer-05）
- 头部修订说明裸 A 编号补前缀（A1）；修订说明③形态描述补 obs_refresh 常驻（A17）。
- T-WEB-01 测试改名 `test_obs_views_derived.py`（A3）。
- T-WEB-09 交付物补 `web/scripts/proto_check.mjs` + `pnpm proto:check`（A15）。
- 涟漪 hop 行文去值留引用（"深度上限见 01 §4.1"；SQL/DDL 镜像从配置读值的做法写明）。
- 偏差表"待 00 §1 登记"批量改（A12）。

### 06（fixer-06）
- T-ART-02 交付物补 `web/src/lib/portraits.ts` 派生 + `--check`（A13）。
- 依赖图 T-LTV-02 依赖改 T-ART-03（roster 依赖在 T-LTV-03 不动）。
- T-LTV-07 的 start_local.sh 增补改"向 T-WEB-20/T-OPS-05 提需求条目"（与 04 T-DIR-06 同例）。
- 偏差表"待 00 §1 登记"批量改（A12）。

### 07（fixer-07）
- T-SYN-03 交付物补 .env.example 七键追加（A6）。
- T-SYN-01/02 改 canonical/快照构造消费方（A7）。
- 测试改名：`test_digest_push.py`/`test_digest_ingest.py`/`test_sync_catchup.py`/`test_grade_derived.py`（A3）。
- 全文裸 `pytest` 补 `cd server && uv run pytest` 前缀（约 10 处）。
- defer 表 vLLM 行对齐"上线部署期"（A8）；"4 手上限"行文去值留引用。
- 偏差表"待 00 §1 登记"批量改（A12）。

### 08（fixer-08）
- 头部修订说明裸 A 编号补前缀（A1）。
- T-AUD-05 标题/依据去值（"干预率（滚动 7 模拟日，阈值见源方案 §4.8）"）；T-AUD-09 门禁线去值留引用 01 §10.2。
- 引用 03 的测试名两处改 `test_no_baseline_warn_only`。
- T-OPS-05 补 m3 段需求接收条款（A14）与 proto_check 调用（A15）。
- T-OPS-08 补"开发期迭代轮数明算（源方案 §5.2）"一句。
- T-OPS-07 验收 1 删错误前缀半句；T-OPS-01 等裸 `uv run pytest`/`uv run python` 补 `cd server &&`；裸 `scripts/audit_run.py` 补 `cd server && uv run python`。
- D12 改写"附表内、以注释段形态到位"；D13 销项（interview_query.py 已在 00 §2）。
- T-OPS-06 备份路径写死 `~/pgsql/backups`（不新增 env）。
- 偏差表"待 00 §1 登记"批量改（A12）。

### 00/09（仲裁者已改）
- 00：A3 改注 vLLM defer 上线期；新增 A18（W2 最小观察原型取消）；§1 表头编号前缀注与"待登记收口"注；§2 附表 T-SYN-03 创建列与部署期行改写；§2 scripts 清单补齐；§5 M1 行 `WSIM_REPLAY_MODE`、M6 行必过项①②③；§7 DoD 增数字纪律与测试 basename 唯一两条。
- 09：M0 E7 静态化（A4）、M0 E8 里程碑范围口径、M1 REPLAY 名、M3 E5/E6 引用修正（T-DIR-01/02/03、01 §9）、M4 E7 归 T-WEB-20、M5 E2 签名色链改 obs-api、§8 step① obs_refresh/④ zod 执行口径/⑤ URL+token、M6 E12 轮数明算。

## C. 设计侧回登（仲裁者已执行）

- 06 §1.4 末行"18 动作名"→19；06 §3 认知调用量行补注"次要层 04 §8.1 已改 ≤16 人×12=192+40 上限口径（评审二轮 N-P1-7），本行'~144'作废待 W2 实测回填"。
- 01 §6.1:582/§6.2 旧名 `promotion.window`/`perf.review` 替换契约名。
- 04 §1.6 删 `WSIM_COST_ALARM_RATIO/WSIM_COST_THROTTLE_RATIO/WSIM_INTERVENTION_RATE_CAP` 三行（阈值载体移 models.yaml thresholds 段，00 §2 附表口径）。

## D. 自检

fixer 完事后 grep 自己文档：裸 `python`/`pytest` 前缀、`worldsim_main`、旧测试名、`待 00 §1 登记` 残留；报告改动条数。
