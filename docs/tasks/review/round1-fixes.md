# 任务文档评审 · 第 1 轮（2026-09-23）修复指令

> 三位评审员（契约一致性 / 完整性依赖 / 可执行性冲突）报告已并入本文，仲裁者裁定如下。**所有 fixer 只改自己负责的文档，全局口径以本文 §A 为准。**
> 源文档：`docs/tasks/00~09`。设计侧回登项由仲裁者直接执行（§C），fixer 不动设计文档。

## A. 全局裁定（所有文档对齐）

1. **主库名 = `worldsim`**，副本库 = `worldsim_replica`。任何 `worldsim_main` 字样改掉。副本库创建唯一归 T-SYN-06（M6），db_init.sh 不预留副本。
2. **白名单/注册表单源生成链**：`server/config/event_types.yaml` 是唯一手维护源（转写 06 §1.2 全量：类型/trigger/source/键标注/economy 标记）；`server/scripts/gen_event_types.py`（M0，归 01 T-CFG-05）派生：economy 审计清单、`server/config/payload_whitelist.yaml`（sync 用）、obs DB 表白名单种子数据、`--check` 对拍模式。**禁止任何第二份手维护镜像**；测试从 yaml 读值，不写死字面量（含类型计数——动态计数，不写 57）。
3. **obs 视图两层文件**：M0 最小集 `server/ddl/obs_views_v1.sql`（obs schema + `obs.filter_payload()` + `obs.events` + `obs.memory_projection` 视图 + `obs.payload_key_whitelist` 表，归 01 T-DB-03）；M4 增量 `server/ddl/obs_derived_v1.sql`（relation_change_log 视图 / `event_grade_view` / 四实体表，归 05 T-WEB-01，复用 M0 函数，禁止再造 `obs_sanitize_payload`/`obs_payload_whitelist.yaml`）。
4. **主库 health_daily**：唯一权威实现归 08 T-AUD-08（metrics.py），DDL 落独立增量文件 `server/ddl/health_daily_v1.sql`（**不动 schema_v1.sql**，保 M0 冻结语义）；05 的 `obs.health_daily` 改为该表上的 VIEW，删掉 obs_refresh.py 自算口径；07 副本侧复算保留但验收加"与 08 实现同数据集对拍"。
5. **成本熔断**：判级器 + ThrottleState + `system.llm.throttle` 事件唯一归 03 T-LLM-08（`llm_gateway/breaker.py`）；08 T-OPS-02 重写为"降速动作消费接线"（04 §8.4 五条动作的读取点：反思阈值、对话场次、director 干预减半等）+ 告警接 T-OPS-01 + 24h 未恢复转 T-OPS-03 clock.pause，删判级器与事件重复实现，测试路径统一 `server/tests/llm/test_cost_breaker.py`。
6. **grade 初值打分**：唯一归 02 T-ADJ-07（`adjudicator/grade.py`，管道 step5 同事务）；04 T-DIR-04 改为"阈值配置与联调验证"，删初值实现。
7. **网关接口与 mock provider**：归 03 T-LLM-01/02（M1 段）；02 T-ADJ-02 删 provider 交付物、改依赖声明；02 D1 改注"mock 形态以 03 为准"。
8. **world.yaml 段划界**：M0（01 T-CFG-03）持 locations/company/stocks/economy 常量；04 T-WA-01 只增作息/节假日/触发规则段，写明"不重建文件，向既有文件追加段落"；测试各归各目录但不同名。
9. **start_local.sh**：唯一创建者 = 05 T-WEB-20（M4，形态=内核+obs-api+web dev server）；08 T-OPS-05 只增补 ingest/worker 起停段；06 文档依赖表改"M4（05 文档）"。
10. **smoke.sh**：唯一创建者 = 08 T-OPS-05（M6，按 09 §8 实现）；05 T-WEB-20 用自己的端到端检查脚本，不引用 smoke.sh；04 T-DIR-06 的"smoke.sh 增 m3 段"改为"向 T-OPS-05 提需求条目"。
11. **models.yaml 键名**以 01 T-CFG-02（M0 冻结方）为准：`providers / task_routes / generation_params / thresholds`；03 T-LLM-04 对齐回改。
12. **task_type 枚举 = 9 项含 `world_copy`**（以 04 §8.1 路由表为准；04 §5.2 注释由仲裁者回改）；03 T-LLM-01 不得拒绝 world_copy。
13. **env 命名**：回放开关统一 `WSIM_REPLAY_MODE`（08/09 回改）；新增 env 一律登记 00 §2 附表（仲裁者已建总表，fixer 在各自偏差表保留条目即可）；`.env.example` 根文件 M0 创建（01 T-ENV-05），M2/M4/M6 各文档"追加条目"对象统一是根 `.env.example`；`deploy/worldsim.env.example` 唯一归 08 T-OPS-04。
14. **调用约定**：一切 Python 执行 = `cd server && uv run python scripts/xxx.py`；脚本路径统一 `server/scripts/` 前缀；禁止裸 `python`/`python3`。
15. **pnpm**：00 §3 已补钉版（corepack 启用 pnpm 9.x）；05 T-WEB-08 加 `corepack enable && corepack prepare pnpm@9 --activate` 步骤。
16. **embedding 定案（P0-1 消解）**：智谱账号实测付费模型与 embedding-3 均"余额不足"，**免费档 glm-4-flash / glm-4.5-flash 可用**。裁定：① embedding 开发期 = **本地 bge-m3**（sentence-transformers 封装 provider，dim=1024 与 04 §5.2 `vector(1024)` 一致，GPU 错峰使用；CPU 可兜底）；智谱 embedding-3 留 models.yaml 备选档，充值后切换；② LLM 路由开发期全档走 glm-4.5-flash（明星）/glm-4-flash（次要/背景/摘要），付费档（glm-4.5-air 等）留配置注释，充值后升档；③ zhipu provider 要点补：glm-4.5-flash 是 reasoning 模型，`max_tokens` 须留足（reasoning_content 会耗配额），只取 `content` 字段。M0 不再以"实测 embedding-3"为 tag 前置，09 E7 改为"bge-m3 本地加载冒烟 + 维度断言 1024"。
17. **叶蓁签名色 = `#FADB40`（杏黄）**：唯一权威 = `config/agents.yaml`（01 §2.2 人设 + 02 §4 色池）；仲裁者已回改 ui/mobile/stream-normal.html 原型；`manifest.json`（T-ART-02）与 `portraits.ts`（T-WEB-11）均为 agents.yaml 的**构建产物**（生成脚本归 T-ART-02），禁止手改。程一诺 `#EB6999` 撞苏蔓问题由 T-CFG-06 校验器重分配。
18. **systemd**：开发期只交模板 + `systemd-analyze verify`；09 M6 E8 改"kill 进程后 start_local.sh 用户态恢复演练"，真重启项 defer 上线。
19. **《09》定位**：09 是闸门文档不持任务；它引用的一切脚本/测试/任务 ID 必须真实存在于 01~08。
20. **数字引用纪律**：06 §3 登记表内数字（干预率/熔断倍数/对话轮数/接受率/还款期限/门禁线）行文一律"见 <持有方>"，配置/DDL/测试镜像从 yaml 读值。

## B. 设计侧契约回登（仲裁者已执行，fixer 知悉即可）

- 06 §2 增 `social.refuse.request_ref` 行（裸 seq 数字字符串）；00 §4 红线 3 同步。
- 06 §1.2 补登 `economy.bill.utility.overdue`（镜像 rent 链：agent_id/amount_cents/overdue_cents/period，economy 审计）——04 文档 T-WA-04 欠费链路改用它，`economy.settle` 回归兜底定位。
- 06 §1.2 `world.perf_review` 行注 `delta_salary` = BIGINT 分、月薪差额绝对值（按 01 §1.5 百分比档×当前月薪）。
- 06 §1.2 `time.day_summary` 行注"生产方 = world_agent 日历日界钩子"；02 D8 销项指 T-WA-02。
- 01 §6.1 `promotion.window` 模板删 `defense_at` 键（答辩排期经 `world.announce` body 承载）；04 D-01 转正。
- 01 §6.1 春节事件组三子事件标注"阶段 1 defer，以摘要记忆口径呈现"；04 D-05 转正。
- 04 §3.3 注释补"8:00 打卡为内部结算、不产事件"；04 §5.2 llm_calls 注释补 `world_copy`；04 §1.6 `WSIM_GLM_API_KEY` → `WSIM_ZHIPU_API_KEY`。
- 03 §5.1 grade 过滤参数、§5.2 subscribe grades、§3.4 今日热涟漪三处 `ui.grade` 口径改"最新生效 grade（event_grade_view 等价物）"；03 §5.1 快照 economy 形态以 05 `stocks[]` 为准回改示例。
- 05 §3.6 快照白名单增 `sim.compression_ratio` 与 `agents[].activity`（消解 05 文档 D14 的 M6 断供）。
- 源方案"附：确认项记录"补登本轮契约变更。

## C. 缺口新任务（已裁定归属）

| 新任务 | 归 | 内容 |
|---|---|---|
| T-ENV-06 | 01 | logging 配置（WSIM_LOG_LEVEL 消费）+ 全项目日志格式 |
| T-CFG-07 | 01 | `server/config/health_thresholds.yaml`（01 §9 阈值镜像，供 05/08 消费） |
| T-DB-04 增项 | 01 | 每人月薪抽定落 `world_state`（供 M3 payroll 读取），含断言 |
| T-TIME-03 增项 | 02 | batch 段回调注册表（world_agent 日历/审计钩子的挂载点） |
| T-ADJ-09 | 02 | 每模拟日 world_state 全量快照落盘（JSON gzip；供 05/07/T-OPS-06 消费） |
| T-REL-06 | 02 | `config/topics.yaml`（01 §7 ≥60 条）+ 话题冷却 + trigger_point 强制切入 |
| T-LLM-09 增项 | 03 | 模板变量补 `topic`/`willingness`（01 §3.5 意愿分注入） |
| T-LLM-04 增项 | 03 | `check_config_fill.py` 入交付物 |
| T-AUD-01 增项 | 08 | `audit_run.py --self-test` 标志（驱动违规样例必报红） |
| T-AUD-09 | 08 | 门禁①支撑：`scripts/export_script.py`（01 §10.1 脱敏导出纯文本剧本）+ 采访评估记忆查询脚本（01 §10.2） |
| T-OPS-08 | 08 | 40 人扩充实跑 + 离线预热 2 模拟周（01 §2.5 修正回写）+ 成本 ×14 对账 |
| T-ART-03 增项 | 06 | `map_layout.json` 第三挂载（或构建期拷入 stream/） |

## D. 各文档修复清单（fixer 执行，逐条落实；P2 建议类可酌情）

### 01（fixer-01）
- T-CFG-05 收编 `gen_event_types.py`（含派生 payload_whitelist.yaml / economy 清单 / obs 白名单种子 / `--check`），验收改动态口径（A2）；类型计数不写 57。
- T-DB-03 按 A3 收窄为 M0 最小集，函数名 `obs.filter_payload` 唯一。
- T-DB-04 增月薪抽定落 world_state（C 表）。
- 新增 T-ENV-06、T-CFG-07（C 表）；T-ENV-04 验收裸 `python` 改 `uv run python`；dev 依赖补 pytest-socket/tiktoken 或在 03 文档登记自实现。
- `WSIM_ZHIPU_API_KEY` 注释改"M0 起即需真值（免费档冒烟）"；`.env.example` 清单与 00 §2 附表对齐。
- 节号错挂修正：交易时段改引 01 §4/04 §6.2；韩彻债务改引 01 §2.2；`INSERT INTO sync_state` 补偏差表一句。
- world.yaml 按 A8 写明划界；models.yaml 键名保持（A11 由 03 对齐你）。
- task_type 9 项含 world_copy（A12）。
- embedding 段落按 A16 改写（bge-m3 1024 定案，embedding-3 备选），R2 阻塞标记销项。

### 02（fixer-02）
- T-ADJ-02 删 provider 交付物改依赖 T-LLM-01/02（A7）；交付物补 `main.py` 主循环 owner（评审 P2-11）。
- T-ADJ-07 收编 grade 初值全量（A6）。
- 新增 T-ADJ-09 快照、T-REL-06 话题系统、T-TIME-03 batch 回调注册表（C 表）。
- 降速动作读取点：T-MEM-02 反思阈值、T-ADJ-04 对话场次各补一行"读取 ThrottleState（T-OPS-02 接线）"。
- D8 销项指 T-WA-02；D6 口径标注"待回登 06 §3 备查"（仲裁者已记）；D5 保留唯一。
- `social.refuse.request_ref` 形态引用改指 06 §2（已回登）。
- 依赖表补估时列；T-ADJ-03 按动作分组拆 03a/03b 或补估时说明。

### 03（fixer-03）
- models.yaml 键名对齐 01（A11）；task_type 9 项（A12）；T-LLM-01/02 标注"M1 段交付，02 文档消费"。
- T-LLM-04 交付物补 `check_config_fill.py`；T-LLM-09 变量补 topic/willingness（C 表）。
- zhipu provider 要点补 A16③（免费档型号表、reasoning_content、max_tokens 留足）；models.yaml 路由示例改 glm-4.5-flash/glm-4-flash 为主用档。
- 熔断基线未回填期行为统一写"只告警不动作"（与 08 对齐）。
- prompts/、schemas/、safety/wordlist.txt 登记偏差（00 §2 已补布局）。
- "轮数 6~8" 两处行文改"见源方案 §3.4/§4.7"；持有方错标修正（熔断线持有方=源方案 §5.2）。
- `grep -rn "sk-"` 验收加 `mkdir -p deploy` 前提或改 M6 生效。

### 04（fixer-04）
- T-WA-10 改纯消费（`gen_event_types.py --check`），删生成器与同名 yaml 交付（A2）；验收 3 改动态计数。
- T-DIR-04 改"阈值配置与联调验证"（A6）；T-WA-01 写明"向既有 world.yaml 追加段落"（A8）。
- T-WA-04 欠费链路用新注册类型 `economy.bill.utility.overdue`（B 表）；`economy.settle` 回兜底。
- D-01（defense_at）/D-03（打卡）/D-05（春节）转正措辞按 B 表改写；D-11 delta_salary 按 06 新注释对齐。
- T-DIR-06 "smoke.sh m3 段"改"向 T-OPS-05 提需求"（A10）。
- T-DIR-05 补降速读取点"director 干预减半（T-OPS-02 接线）"。
- "14 模拟日"行文改"见 01 §3.2/§4"；旧名引用加"（01 旧名）"标注。

### 05（fixer-05）
- T-WEB-01 改增量 `obs_derived_v1.sql`（A3），`obs.health_daily` 改 VIEW over 08 的表、删 obs_refresh.py 自算（A4）；依赖列改"M0 + M3 数据"。
- T-WEB-20 创建 start_local.sh（A9）+ 自己的端到端检查脚本；长挂/断网演练拆独立夜班段或注明墙钟。
- T-WEB-08 补 corepack 步骤（A15）；T-WEB-11 的">2× 整段直显"登记偏差表并摘 03 §3.1 节号；at_offset_s 播放表述用"相邻差值 ÷ 倍率"。
- T-WEB-11 portraits.ts 改为构建产物（A17）。
- grade 过滤口径统一"最新生效 grade（event_grade_view 等价物）"（B 表）。
- 数值字面量守卫改从 health_thresholds.yaml 读值（A20 口径）；D14 按 B 表销项；`WSIM_OBS_*` 保留并确认已入 00 附表。

### 06（fixer-06）
- manifest.json 改构建产物（A17）；T-ART-03 增 map_layout.json 挂载（C 表）。
- 依赖表 start_local.sh 改 M4（05 文档）（A9）。
- 字幕规格口径疑点（Y3）按"直播页 390×844 为画面内字幕、02 §6.2 为切片后期字幕，两者不冲突"写清；Y4 标注待 03 协议枚举（已随 B 表 grade/快照回改缓解，保留观察）。
- 倍率固定 1.0 等工程默认保持登记即可；确认签名色引用统一指向 agents.yaml（叶蓁 #FADB40）。

### 07（fixer-07）
- T-SYN-01 白名单改"由 gen_event_types.py 派生 `server/config/payload_whitelist.yaml`"，`gen_payload_whitelist.py` 名字废弃（A2）。
- T-SYN-09 digest 闭环增 `digest_log` 表后的验收改"授权面不变 + 清单制"（评审 P2-4）。
- T-SYN-08 验收加"与 08 metrics.py 同数据集对拍"（A4）。
- T-SYN-10 `WSIM_OBS_PG_DSN` 写死（删"候选"）；确认 WSIM_CLOUD_INGEST_HTTPS 消费点写明（T-SYN-04）。
- defer 表补"涟漪五段 SQL 回归用例由 09 M6 必过项③承接（数据源 T-SYN-06/07）"。

### 08（fixer-08）
- T-OPS-02 按 A5 重写（降速接线，不重复判级器）；T-AUD-01 补 `--self-test`；新增 T-AUD-09、T-OPS-08（C 表）。
- T-AUD-08 DDL 落 `ddl/health_daily_v1.sql`（A4）；阈值改从 `config/health_thresholds.yaml` 读（T-CFG-07）。
- T-OPS-05 创建 smoke.sh（按 09 §8）+ start_local.sh 增补段（A9/A10）。
- `REPLAY_MODE` → `WSIM_REPLAY_MODE`；备份路径定 `~/pgsql/backups`；T-OPS-01 的"日志分级框架"依赖改指 T-ENV-06。
- env 验收口径改确切清单 diff（对齐 00 §2 附表）；熔断倍数行文改"见源方案 §5.2"。

### 09/00（仲裁者已改）
- 库名/脚本名/任务 ID/重试次数/÷倍率/CHECK 虚构项/必过项③/告警演练/冒烟工程口径注明/M1 E7 措辞/E8 用户态等价/step1 补 replica 段，全部已修。
- 00 §1 增 A9~A17 登记、§2 附 env 总表与 config 布局补全、§3 补 pnpm、§4 红线加 request_ref。

## E. 修复完成后自检

每个 fixer 完事后：① 自己文档内 grep 旧名/旧路径残留；② 报告改动条数与未采纳的 P2 及理由。
