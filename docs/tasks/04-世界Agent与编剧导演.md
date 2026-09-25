# WorldSim · 开发任务文档 04 · 世界 Agent 与编剧导演（WA/DIR）

> v1.2 · 2026-09-23。里程碑 M3（00 §5）。任务模板、契约红线、DoD 见 `docs/tasks/00-总览与里程碑.md`（§6/§4/§7）；事件类型/字段/数字唯一仲裁方 = `docs/design/06-契约登记表.md`（下称 06，本文按 v1.2 已生效口径引用）。
> 本文档不新造数字、字段名、事件类型：数值一律写"见 01 §x"引用持有方；`config/*.yaml` 中出现的数值是设计文档的**部署镜像**（04 §12.4 同口径），代码内零硬编码阈值。发现的契约缺口/工程默认全部登记在本文 §6，不回改设计文档。
> v1.1 修订（对齐 `docs/tasks/review/round1-fixes.md` 第 1 轮裁定）：T-WA-10 改纯消费 `gen_event_types.py --check`，删生成器与同名 yaml 交付（R1 §A.2：单源归 01 T-CFG-05）；T-DIR-04 改"阈值配置与联调验证"，删 grade 初值实现（R1 §A.6：唯一归 02 T-ADJ-07）；T-WA-01 写明向既有 world.yaml 追加段落、不重建（R1 §A.8）；T-WA-04 水电欠费改用 06 v1.2 已注册的 `economy.bill.utility.overdue`，`economy.settle` 回归兜底；D-01/D-03/D-05 转正、D-02/D-11 销项（06/01/04 v1.2 回登已生效）；T-DIR-06 的 smoke.sh m3 段改向 08 T-OPS-05 提需求（R1 §A.10）；T-DIR-05 补降速读取点（R1 §A.5，接线归 T-OPS-02）；还款期限等数字行文改"见持有方"（R1 §A.20）；回放开关统一 `WSIM_REPLAY_MODE`（R1 §A.13）。
> v1.2 修订（对齐 `docs/tasks/review/round2-fixes.md` 第 2 轮裁定）：v1.1 修订说明裸 A 编号补 `R1 §A.` 前缀（R2 §A.1）；T-DIR-03 干预率上限删 `WSIM_INTERVENTION_RATE_CAP` env 读法，改 `config/models.yaml` `thresholds:` 段（01 T-CFG-02 唯一载体）并写明与审计④同数据源（R2 §A.2）；§3 补 T-DIR-01/T-DIR-03 依赖环澄清——代码单向 DIR-01→DIR-03 调用，预算数据经 world_state 共享，T-WA-04 头部依赖补 T-DIR-03；T-DIR-04 补向 world.yaml 追加 `director.grade` 段的划界声明（01 T-CFG-03 已覆盖）；T-DIR-06 验收 5 路径补 `server/` 前缀（R2 §A.14）；T-WA-01 验收 1 测试改名 `tests/world_agent/test_world_config_wa.py`（R2 §A.3 改名表）；D-21 销项（04 §5.3 已统一 `WSIM_REPLAY_MODE`）；偏差表「待 00 §1 登记」批量改「已登记（本文偏差表）」（R2 §A.12）。
> v1.3 · 2026-09-24 修订（对齐 `docs/tasks/review/round3-fixes.md` F2 #9/#10）：T-DIR-01 验收 5 `::test_budget_enforcement` 移归 T-DIR-03 验收（配额耗尽后新干预的拒绝方为 T-DIR-03，构建序 DIR-01 先于 DIR-03；R3-A P1-6）；§3 内部依赖图补 T-WA-04→T-DIR-03 边（R3-A P2-1）；v1.2 修订说明复查无裸 A 编号（均有 `R2 §A.` 前缀），无需补改。

> **验收命令约定**：本文验收清单中的 `uv run ...` 命令 cwd = `server/`（前缀 `cd server &&` 依 00 §1 A14 省略书写）。

## 1. 范围

M3「世界 Agent 与编剧」全部规则化模块（04 §2.1 `world_agent/` + `world_agent/director/`）：

- **日历与经济结算**：作息/节假日判定、batch 段跨日日历结算（04 §3.3，含 8:00 打卡内部结算）、`economy.payroll`、`economy.bill.rent` / `economy.bill.utility`、欠费链路 `economy.bill.rent.overdue` / `economy.bill.utility.overdue`（06 §1.2 v1.2 已注册）→ `economy.bill.rent.notice`、`economy.settle` 兜底（01 §1.5）、债务逾期日结算（04 §6.2）、`time.day_summary`；
- **股价**：随机游走 `economy.stock.tick`（rng_seed 入库可复现，参数见 01 §1.5）、休市、`world.announce` 的 `stock_shock` 冲击结算；
- **职场日历**：`world.promotion_window`、`world.perf_review`（S~C 与涨薪档，见 01 §1.5/§6.1）、`world.overtime`（频次/时段见 01 §6.1/§1.6）、`world.team_building`；
- **规则触发传闻**：`world.layoff_rumor`（星澜科技单日跌幅超阈值 → 次日定时生成，`trigger='world'` 不占干预率；阈值/时点/效果口径见 01 §6.1）；
- **随机扰动**：`world.disturb.illness` / `.weather` / `.complaint` / `.lucky`（lucky 含金额必有 `amount_cents`，06 §1.2）；
- **编剧导演**：`config/arcs.yaml` 弧线状态机 + fail-forward（01 §6.2，六条骨架每条配 pytest）、干预框架 L0~L2（L3 永禁）+ `director.intervene` + 干预率 7 日滑窗统计（红线见源方案 §4.8/06 §3）、grade 阈值配置与联调（T-DIR-04；R1~R4 初值实现唯一归 02 T-ADJ-07，04 §6.6）+ K3 复核 `director.grade_revise`（每日上调上限见 01 §6.4）；
- **契约部署镜像消费**：`config/event_types.yaml` 唯一手维护源归 01 T-CFG-05（转写 06 §1.2，A2 单源裁定）；本模块纯消费——economy 审计域标记清单（04 §10.1 ① 的唯一数据源）从该文件读值，并经 `gen_event_types.py --check` 对拍（T-WA-10）。

## 2. 不在范围（defer 去向）

| 项 | defer 到 |
|---|---|
| 裁决管道六步本体、step5 落库钩子、`debts` 表 DDL、`borrow_money`/`repay_money` 动作、`state.needs_delta`/`relation.changed` 聚合机制本体 | M1（02-模拟内核.md，T-TIME/T-ADJ/T-REL/T-MEM；本文档只消费其接口） |
| LLM 网关、director/world_copy 路由、prompt 模板机制、安全三层过滤 | M2（03-LLM网关.md，T-LLM）；M3 内以 mock/模板文案先行，T-DIR-05 预留真接入点 |
| 每日审计 6 项 SQL 本体、五指标与两项结构指标计算、审计日报 | 08-审计健康度与运维.md（T-AUD，M3 起滚动）；本文档交付 `intervention_rate_7d()` 供其复用；`event_types.yaml` 单源归 01 T-CFG-05（A2），本文档只消费不交付 |
| 观察端 UI 文案模板/渲染（03 §5.3 已有行与待补行） | M4（05-Web后端与观察端.md，T-WEB） |
| 副本侧 `event_grade_view` 物化、digest、出站同步 | M6（07-同步与副本库.md，T-SYN） |
| 春节轻量事件组（返乡队列/家庭来电/节后重逢，01 §6.1） | 01 §6.1 v1.2 已标注「阶段 1 defer，以摘要记忆口径呈现」，无需注册事件类型；M3 不实现（偏差 D-05 转正，见 §6） |
| 房东涨租排期（01 §11.2 发生器 2） | 设计口径项目周期内永不触发，仅在 T-WA-01 留配置位，不实现调度 |
| 校外住处节点组与 NPC 晚间驻留调度（01 §1.6） | M1 调度/地点（02 文档）；本文档日历模块只读其状态 |
| 40 人人设入库、`seed_40.sql`、secondary 层扩容 | M0（seed_8）/ M6（40 人，00 §5） |

## 3. 任务依赖表

**外部依赖**（按文档/前缀引用， sibling 文档任务编号以其定稿为准）：

| 依赖方 | 本文档需要的内容 |
|---|---|
| 01-脚手架与数据库.md T-DB（M0） | `schema_v1.sql`：`events`（append-only 触发器）/`agents`/`world_state`/`debts`/`interventions` 表（04 §5.2） |
| 01 T-CFG（M0） | config 加载/校验工具；`agents.yaml` seed（含每人月薪初始值，偏差 D-06）；`config/world.yaml` 既有文件与 locations/company/stocks/economy 常量段（T-CFG-03，T-WA-01 只追加不重建，A8）；`config/event_types.yaml` 单源与 `gen_event_types.py --check`（T-CFG-05，T-WA-10 纯消费，A2） |
| 02-模拟内核.md T-TIME（M1） | batch 段 `world_agent.run_batch_calendar()` 调用点（04 §3.3）、`now_sim()`、时钟事件 |
| 02 T-ADJ（M1） | step5 同事务钩子与 tick 内聚合缓冲（04 §6.6）；grade 初值唯一实现 T-ADJ-07（`adjudicator/grade.py`，管道 step5 同事务，T-DIR-04 的联调对象，A6）；`debts` 读写；校验器消费请假/欠费标记 |
| 02 T-REL / T-MEM（M1） | G-MON-04 权重与挫败值消费欠租标记（01 §1.5/§3.3）；L2「世界观察」记忆注入接口（01 §6.3） |
| 03-LLM网关.md T-LLM（M2） | `director`/K3 路由与计量（04 §8.1/§8.6）、prompt 模板机制（T-DIR-05 真接入）；成本熔断判级器与 ThrottleState（T-LLM-08，降速读取点接线归 08 T-OPS-02） |
| 08 T-AUD（M3 滚动） | 审计 ①④ 与 `event_types.yaml`（01 T-CFG-05 单源）、本文档 `intervention_rate_7d()` 同口径对账 |

**内部依赖图**：`T-WA-01 → T-WA-02 → {T-WA-03, T-WA-05, T-WA-07, T-WA-08, T-WA-09}`；`T-WA-03 → T-WA-04`；`T-WA-05 → T-WA-06`；`T-WA-02 → T-DIR-01 → T-DIR-02`；`{T-WA-09, T-DIR-01, T-WA-04} → T-DIR-03`；`T-WA-01 → T-DIR-04 → T-DIR-05 ← T-DIR-03`（T-DIR-04 的初值实现外部依赖 02 T-ADJ-07）；`T-WA-10` 独立（依赖 01 T-CFG-05，M0 后即可）；全部 → `T-DIR-06`。

依赖环澄清（T-DIR-01/T-DIR-03 不成环）：代码单向 DIR-01→DIR-03 调用（弧线引擎经 `intervene()` 统一落库），预算数据经 world_state 共享（`intervention_budget` 配额消耗由 DIR-01 写入、DIR-03 读取校验，DIR-03 不反向调用 DIR-01 代码）。

## 4. 任务表

### T-WA-01 世界参数配置族 `world.yaml` 追加段与加载校验
- 里程碑 / 依赖: M3 ；T-CFG-03（01 文档，world.yaml 既有文件与常量段）；估时 1d
- 设计依据: 00 §2（`config/world.yaml` 位置）、01 §1.4（作息）、01 §6.1（节假日表/触发规则）、01 §1.5（经济与股价参数）、01 §1.3（部门编制）、01 §1.2（楼层价差）、04 §12.4（配置=设计部署镜像口径）、A8 段划界裁定（round1 §A.8）
- 目标: 本模块全部规则参数落唯一配置文件，代码零硬编码数字；**不重建文件，向既有文件追加段落**
- 交付物: `server/config/world.yaml` 追加段（既有文件与 locations/company/stocks/economy 常量段归 01 T-CFG-03，M0）；`server/worldsim/world_agent/config.py`（pydantic 加载+校验，覆盖全文件含既有段）
- 实现要点:
  - 消费既有段（01 T-CFG-03 持，本任务不改动）：公司编制（六部门/经理/职级，见 01 §1.3）；薪资档、房租楼层档、水电区间、通勤通讯包（见 01 §1.5）；股价参数（标的/初始价/随机游走参数/手续费，见 01 §1.5）；楼层价差（见 01 §1.2）
  - 追加段一·作息表：工作日/周末/节假日时段约束（见 01 §1.4）
  - 追加段二·节假日表：名称/天数/特殊规则（休市、工作事件停发、社交与邀约权重上调，见 01 §6.1；日期按模拟日历配置）
  - 追加段三·触发规则参数：裁员传闻阈值与效果、四类扰动概率、加班频次与时段窗口、绩效/晋升/团建日历规则（见 01 §6.1/§1.6/§11.2）
  - 房东涨租仅留配置位（开关+幅度），不实现调度（见 §2）
  - 加载校验：节假日表非空且含春节；部门编制与 01 §1.3 一致；全部区间 min≤max；校验失败拒绝启动
- 验收标准:
  1. `cd server && uv run pytest tests/world_agent/test_world_config_wa.py` 全绿：正常加载；注入缺键/区间倒置/部门编制不符即拒绝（测试归 `tests/world_agent/`，basename 全库唯一（R2 §A.3），与 01 T-CFG-03 的配置测试不同目录不同名）
  2. 用例断言节假日表条目与 01 §6.1 节假日表一一对应（测试读配置断言，数值不复制进代码）
  3. 追加纪律：本任务落地后 world.yaml 既有段（locations/company/stocks/economy 常量段）diff 为零，仅新增作息/节假日/触发规则段
  4. 评审抽查：`world_agent/` 与 `director/` 源码无字面阈值（grep 薪资/股价/概率数字零命中）
- 实测回填: 无

### T-WA-02 日历引擎 `calendar.py`：作息/节假日判定与日界结算框架
- 里程碑 / 依赖: M3 ；T-WA-01、T-TIME（02 文档）；估时 1.5d
- 设计依据: 04 §3.3（`run_batch_calendar` 跨日结算）、04 §2.1（模块位置）、01 §1.4（作息）、01 §6.1（节假日规则）、06 §1.2（`time.day_summary` payload）
- 目标: 全模块统一的模拟日历判定 + 跨日批量结算单入口
- 交付物: `server/worldsim/world_agent/calendar.py`；`server/tests/world_agent/test_calendar.py`
- 实现要点:
  - 判定 API：`is_workday / is_weekend / is_holiday(sim_date)`、当前作息时段；供调度器、校验器、股价、扰动、职场日历共用
  - 节假日生效：休市标记（T-WA-05 消费）、工作事件停发标记（T-WA-07/08 消费）、社交/邀约权重上调标记（02 文档决策侧消费；口径见 01 §6.1）
  - `run_batch_calendar()`：凌晨 batch 段由 time_engine 调用（04 §3.3），按注册回调表顺序结算跨日日历项（payroll/bill/stock/绩效等由各任务注册）；8:00 打卡为**内部出勤状态结算，不产事件**（04 §3.3 v1.2 注释口径；D-03 转正，见 §6）
  - 日界检测以 sim 日期翻转为准（batch 段与 continuous 段统一判定），恰落一条 `time.day_summary`，payload `{day}`（day=自世界纪元模拟日序号；触发时点为工程默认，偏差 D-04），`source='system'`、`trigger='system'`（06 §1.2；生产方=world_agent 日历日界钩子，06 §1.2 v1.2 注）
  - 春节轻量事件组不实现（01 §6.1 v1.2 注「阶段 1 defer，以摘要记忆口径呈现」；见 §2，D-05 转正）
- 验收标准:
  1. `test_calendar.py::test_workday_weekend_holiday`：构造日期序列，三种判定与配置节假日表一致
  2. `::test_batch_calendar_dispatches`：mock 时间引擎进 batch 段，断言注册回调按序执行、跨日排程项（如次日 9:00 类）无遗漏无重复
  3. `::test_day_summary_once_per_day`：连跑 3 模拟日，`SELECT COUNT(*) FROM events WHERE type='time.day_summary'` = 3，payload 键仅 `day`
  4. `::test_holiday_flags`：节假日当天休市/工作停发标记为真、工作日为假
- 实测回填: 无

### T-WA-03 经济日历结算：`economy.payroll` 与房租/水电账单
- 里程碑 / 依赖: M3 ；T-WA-02、T-DB（M0）、T-ADJ（M1，needs_delta 接口）；估时 1.5d
- 设计依据: 01 §6.1（触发日历与模板）、01 §1.5（薪资档/固定支出/财富映射）、06 §1.2（`economy.payroll`/`economy.bill.rent`/`economy.bill.utility` payload 与 economy 标记）、06 §1.3（`amount_cents` 口径）、04 §6.5（needs_delta 聚合）
- 目标: 三类固定日历经济事件自动结算、入账、联动需求
- 交付物: `server/worldsim/world_agent/economy.py`（结算函数，注册进 T-WA-02 回调表）；`server/tests/world_agent/test_payroll_bills.py`
- 实现要点:
  - `economy.payroll`：触发日历见 01 §6.1；金额=每人月薪（档位见 01 §1.5；每人具体值 M0 seed 时按档抽定，本任务只读，持久化位置偏差 D-06）；通勤通讯包随工资代扣（01 §1.5）；涨薪后读最新月薪（T-WA-07 写入）；payload `{agent_id, amount_cents, month}` 逐字按 06 §1.2（`text_display?` 可选）
  - `economy.bill.rent`：触发日历与 `due` 见 01 §6.1；金额按楼层档（01 §1.5）
  - `economy.bill.utility`：触发日历见 01 §6.1；金额在配置区间内随机游走（01 §1.5），骰子 seed 落 `events.rng_seed`
  - 全部系统结算：`amount_cents` 正入负出（06 §1.3）；财富/情绪需求变更随 `state.needs_delta` 落库（`cause`=本事件 seq，数值口径见 01 §3.1/§6.1）
  - 余额不足不扣负，转 T-WA-04 欠费链路
- 验收标准:
  1. `test_payroll_bills.py::test_payroll_day`：推进至发薪日，断言恰 `SELECT COUNT(*) FROM agents` 条 `economy.payroll`，`amount_cents` = seed 月薪 − 通勤包（读配置），`source='world'`、`trigger='world'`
  2. `::test_rent_bill_by_floor`：三个楼层档各抽 1 人断言账单金额与配置一致
  3. `::test_utility_amount_in_range`：金额落配置区间且同 seed 重跑序列一致
  4. SQL 对账：`SELECT SUM((payload->>'amount_cents')::bigint) FROM events WHERE type='economy.payroll'` 与 `agents.balance_cents` 增量一致（口径 04 §10.1 ①）
  5. `::test_needs_delta_linked`：每笔结算存在对应 `state.needs_delta`，`changes[].cause` 为该事件 seq 的裸数字字符串形态（06 §2）
- 实测回填: 无

### T-WA-04 欠费链路与债务逾期日结算
- 里程碑 / 依赖: M3 ；T-WA-03、T-DIR-03（退租危机 L1 落 `director.intervene` 的唯一入口，见实现要点）、T-REL（M1）、T-ADJ（M1，`debts`）；估时 1.5d
- 设计依据: 01 §1.5（欠租/欠费结算规则表）、06 §1.2（`economy.bill.rent.overdue`/`economy.bill.utility.overdue`/`economy.bill.rent.notice`/`economy.settle` payload；utility.overdue 为 v1.2 新注册，镜像 rent 链）、04 §6.2（债务逾期结算归 world_agent）、01 §3.2/§4（逾期关系行与还款期限口径，数值见持有方）、01 §3.3（挫败值）、01 §6.3（退租危机 L1 计干预率）
- 目标: 余额不足场景全链路硬规则结算：不扣负、不计复利、不死锁
- 交付物: `economy.py` 欠费函数 + `calendar.py` 每日扫描；`server/tests/world_agent/test_overdue_chain.py`
- 实现要点:
  - 账单日余额不足：实扣至余额归 0，差额挂账（不计复利，01 §1.5），落 `economy.bill.rent.overdue`，payload `{agent_id, amount_cents（实扣）, overdue_cents（差额）, period}` 逐字 06 §1.2；情绪/财富需求变更（01 §1.5）落 `state.needs_delta`
  - 挂账持久化用 `world_state` KV（DDL 无欠租列，偏差 D-06）；欠账期间的 G-MON-04 权重上调与消费降级（倍率与规则见 01 §1.5）由 T-REL/T-ADJ 消费，本任务只写状态标记
  - 水电欠费：落 `economy.bill.utility.overdue`（06 §1.2 v1.2 已注册，镜像 rent 链：payload `{agent_id, amount_cents（实扣）, overdue_cents（差额）, period}` 逐字，打 economy 审计标记；D-02 销项）；挂账结构同欠租；`economy.settle` 回归兜底定位（仅无注册类型的结算场景使用）
  - 结清：余额充足自动补扣清账（同类型负额结算，不新增 payload 键）
  - 下一账单周期未结清 → `economy.bill.rent.notice`，payload `{agent_id, overdue_cents（快照）, period}`（无新结算，06 §1.2）；情绪/挫败值变更（01 §1.5/§3.3）
  - 连续 2 个账单周期未结清（01 §1.5）：有最低价位空房 → 强制搬迁（改 `agents.room_no` + 写记忆）；无空房 → 退租危机经 T-DIR-03 以 L1 落 `director.intervene`（计干预率），交编剧弧线承接（A6，01 §6.2）
  - 债务逾期日结算（04 §6.2）：每日扫 `debts WHERE due_sim < now_sim() AND repaid_cents < amount_cents`，按 01 §3.2 逾期行逐日结算，落 `relation.changed`（`cause` 口径 04 §6.5）；还清即止
- 验收标准:
  1. `test_overdue_chain.py::test_overdue_event_payload`：构造低余额，overdue 事件三键逐字、`amount_cents`+`overdue_cents`=账单额、余额=0
  2. `::test_notice_no_new_settlement`：次周期未结清，notice 事件**无** `amount_cents` 键、`overdue_cents`=挂账快照
  3. `::test_no_negative_no_compound`：任意构造序列下 `SELECT COUNT(*) FROM agents WHERE balance_cents<0`=0，挂账额不随时间增长
  4. `::test_forced_move_or_crisis`：2 周期未结清，有空房 → `room_no` 变最低价位；无空房 → 恰一条 `director.intervene`(L1) 且计入干预率口径
  5. `::test_debt_overdue_daily`：构造逾期债务，断言每日 `relation.changed` 含对应 `changes[]` 行（数值见 01 §3.2），全额结清后停止
  6. `::test_utility_overdue`：水电欠费落 `economy.bill.utility.overdue`，三键逐字 06 §1.2 且打 economy 审计标记，挂账/结清链路同欠租；欠费链路全程不走 `economy.settle` 兜底
- 实测回填: 无

### T-WA-05 股价随机游走 `economy.stock.tick`
- 里程碑 / 依赖: M3 ；T-WA-01、T-WA-02；估时 1.5d
- 设计依据: 01 §1.5（股票系统：标的/初始价/分布参数/rng_seed/冲击/手续费）、01 §6.1（触发日历与休市）、06 §1.2（`economy.stock.tick` payload；无 `amount_cents` 不进守恒求和）、04 §2.1（economy.py 职责）、04 §5.3（rng_seed 复现语义）、04 §13 W2 实测清单④
- 目标: 每模拟日股价结算可复现、可审计、可供传闻联动
- 交付物: `economy.py` 股价函数；`server/tests/world_agent/test_stock.py`
- 实现要点:
  - 每模拟日结算时点（日历见 01 §6.1）对全部标的各落一条 `economy.stock.tick`，payload `{symbol, open, close, r, seed}` 逐字 06 §1.2（`text_display?` 可选）；**不写 `amount_cents`**
  - `r` 抽样分布与参数见 01 §1.5（配置镜像）；seed 由内核骰子序列派生，同写 `events.rng_seed` 列与 `payload.seed`；`WSIM_REPLAY_MODE` 重放从 `rng_seed` 取数（04 §5.3 已统一该名；取值口径见 00 §2 附表，D-21 销项）
  - 价格状态存 `world_state`（`stock.<symbol>`：最新价/待结算冲击）；休市判定读 T-WA-02（节假日休市见 01 §6.1；周末口径为工程默认，偏差 D-07）
  - `world.announce` 携带 `stock_shock`（幅度口径见 01 §1.5）：写入待结算冲击，下一 tick 并入 `r` 一次性生效；编剧发起路径 `trigger='director'` 计干预率（T-DIR-03）
  - `agent.trade_stock` 结算本体在 M1（T-ADJ），本任务只保证价格状态可读与手续费参数配置化（01 §1.5）
- 验收标准:
  1. `test_stock.py::test_reproducible`：固定 seed 重跑 10 模拟日，`r`/`close` 序列逐值一致；`WSIM_REPLAY_MODE` 回放下零新骰子
  2. `::test_distribution`：≥500 样本矩检验，均值/标准差落配置参数容差内（参数读 `world.yaml`，见 01 §1.5）
  3. `::test_market_closed`：节假日与周末无 `economy.stock.tick`；交易日结算时点（读配置）恰每标的 1 条
  4. `::test_shock_applied_once`：注入 `stock_shock`，断言次日 `close` 含冲击且仅生效一次
  5. SQL：`SELECT COUNT(*) FROM events WHERE type='economy.stock.tick' AND payload ? 'amount_cents'` = 0
- 实测回填: `|r|` 分布实测与传闻阈值触发频率 → 04 §13 W2 清单④（T-DIR-06 汇总产出，08 文档日报留痕）

### T-WA-06 裁员传闻规则触发 `world.layoff_rumor`
- 里程碑 / 依赖: M3 ；T-WA-05；估时 1d
- 设计依据: 01 §6.1（规则触发口径：阈值/触发时点/`trigger='world'` 不占干预率；效果口径）、06 §1.2（payload `{drop_pct, scope}`）、01 §11.2 发生器 3（联动）、04 §10.1 ④（干预率口径旁证）
- 目标: 股价异动自动产传闻，规则触发不占干预率
- 交付物: `calendar.py` 触发器；`server/tests/world_agent/test_layoff_rumor.py`
- 实现要点:
  - 每日 `economy.stock.tick` 结算后判定星澜科技 `r`；跌破阈值（见 01 §6.1，配置镜像）→ 次日触发时点（见 01 §6.1，配置镜像）落 `world.layoff_rumor`，payload `{drop_pct, scope}` 逐字 06 §1.2，`text_display` 走模板文案；`source='world'`、`trigger='world'`
  - 效果结算（口径见 01 §6.1）：全公司 gossip 意图权重上调标记（持续时长配置化，02 文档决策侧消费）；成就需求全员变更落 `state.needs_delta`（`trigger='system'`，`cause`=传闻事件 seq）
  - 与 `world.company_crisis`（编剧 L1，频次上限见 01 §6.1）为独立入口；company_crisis 编排由 T-DIR-01/T-DIR-03 承载，本任务不实现
- 验收标准:
  1. `test_layoff_rumor.py::test_trigger_threshold`：构造 `r` 在阈值两侧（阈值读配置）：过线 → 次日触发时点恰一条；未过线 → 零条
  2. `::test_not_intervention`：`SELECT COUNT(*) FROM events WHERE type='world.layoff_rumor' AND trigger='director'` = 0（不进审计④分子）
  3. `::test_effects`：`state.needs_delta` 含全员成就变更且 `cause`=该事件 seq；gossip 权重标记窗口正确
  4. `::test_daily_judgement`：连续两日过线各产一条（无冷却为按字面规则的工程口径，偏差 D-08）
- 实测回填: 规则实测触发频率（连同 T-WA-05 `|r|` 分布）→ 04 §13 W2 清单④

### T-WA-07 职场日历：`world.promotion_window` 与 `world.perf_review`
- 里程碑 / 依赖: M3 ；T-WA-02、T-WA-03（涨薪入账）；估时 1.5d
- 设计依据: 01 §6.1（触发日历与模板，v1.2 已删 `defense_at` 键）、01 §1.3（名额稀缺性与经理初评职责）、01 §1.5（涨薪档/2C 候选池/C 级情绪）、01 §1.6（竞聘答辩黄金档排期）、06 §1.2（两类型 payload 逐字，含 `delta_salary` 单位注释）
- 目标: 晋升与绩效两条职场主节拍自动排期结算
- 交付物: `calendar.py` 职场段；`server/tests/world_agent/test_career_calendar.py`
- 实现要点:
  - `world.promotion_window`：触发日历见 01 §6.1；payload `{dept, slots, candidates[]}` 逐字 06 §1.2（`defense_at` 已自 01 §6.1 模板删除，v1.2 回登，D-01 转正）；每窗口每部门名额口径见 01 §1.3；candidates 产生规则设计未定义 → 工程默认=该部门全体 P1（偏差 D-09）；答辩排期（01 §1.6）经 `world.announce` body 承载与弧线钩子（T-DIR-01 的 A2）呈现
  - `world.perf_review`：触发日历见 01 §6.1；payload `{agent_id, manager_id, grade, delta_salary?}` 逐字 06 §1.2；grade 评分公式设计未定义 → 工程默认：期间系统指标（出勤/工作事件按期/加班参与）加权后部门内分层映射 S~C，参评范围默认全员（偏差 D-10）；`manager_id`=部门经理 NPC（01 §1.3）
  - 涨薪档（S/A/B/C 对应口径见 01 §1.5）：`delta_salary` 落事件（单位与口径见 06 §1.2 v1.2 注释：BIGINT 分、月薪差额绝对值=01 §1.5 百分比档×当前月薪；D-11 销项），实际入账经下个发薪日 `economy.payroll`（06 §1.2 注释口径）；本任务更新月薪状态（持久化见 D-06）
  - C 级：情绪变更（01 §1.5/§6.1）落 `state.needs_delta`；连续 2C 进裁员候选池状态（`world_state`），供编剧弧线（A2/A6）读取
- 验收标准:
  1. `test_career_calendar.py::test_promotion_window`：推进至窗口日，每部门恰一条、`slots` 与配置一致、`payload ? 'defense_at'` 为假（键集合与 06 §1.2 一致）
  2. `::test_perf_review_calendar`：按 01 §6.1 触发日历（读配置）全员各一条，`grade ∈ {S,A,B,C}`，`manager_id` 为对应部门经理
  3. `::test_raise_lands_next_payroll`：S/A 员工 `delta_salary` 与月薪档比例口径（01 §1.5）一致，下个发薪日 `economy.payroll` 金额反映新月薪
  4. `::test_c_grade_chain`：C 级情绪变更落 `state.needs_delta`（`cause` 链接）；连续 2C 后候选池状态为真
  5. 两类型 payload 键与 `config/event_types.yaml` 注册表（01 T-CFG-05 单源，经 T-WA-10 对拍链路核验）逐行 diff 为空
- 实测回填: 无

### T-WA-08 晚间排期：`world.overtime` 与 `world.team_building`
- 里程碑 / 依赖: M3 ；T-WA-02；估时 1d
- 设计依据: 01 §6.1（触发规则与模板）、01 §1.6（黄金档排期表）、06 §1.2（两类型 payload；团建收费时 economy 标记）、03 §5.3（🌃 横幅已有行——UI 消费在 M4）
- 目标: 黄金档职场戏排期自动化、可复现
- 交付物: `calendar.py` 晚间排期器（排期表数据结构供 T-DIR-03 L1 复用）；`server/tests/world_agent/test_evening_schedule.py`
- 实现要点:
  - `world.overtime`：频次与时段口径见 01 §6.1/§1.6（配置镜像，seed 可复现）；payload `{dept, reason, participants[]}` 逐字 06 §1.2，`text_display` 走模板文案；participants 选取规则设计未定义 → 默认该部门当日未请假员工（偏差 D-12）；`reason` 用模板池，编剧/弧线可指定（T-DIR-01/T-DIR-03 传参）
  - `world.team_building`：触发日历见 01 §6.1（晚间场次见 01 §1.6）；payload `{dept|all, activity, amount_cents?}` 逐字 06 §1.2；向 agent 收费时 `amount_cents` 必有且打 economy 审计标记（06 §1.2）
  - 互斥：节假日当天不排（工作事件停发，01 §6.1）；排期冲突由排期表统一判定
- 验收标准:
  1. `test_evening_schedule.py::test_overtime_frequency`：连跑 4 模拟周，每部门每周次数落配置区间（01 §6.1）、时段落配置窗口、仅工作日
  2. `::test_overtime_payload`：payload 键集合与 06 §1.2 一致；participants 非空且均为该部门员工
  3. `::test_team_building`：推进至每月对应周六恰一条；收费配置开启时 `amount_cents` 存在且余额守恒对账（04 §10.1 ①）通过
  4. `::test_holiday_mutex`：节假日当天零条 `world.overtime`/`world.team_building`
- 实测回填: 无

### T-WA-09 随机扰动 `world.disturb.*` 四发生器
- 里程碑 / 依赖: M3 ；T-WA-02；估时 1d
- 设计依据: 01 §6.1（四类扰动概率与模板）、06 §1.2（`world.disturb.*`：trigger=world/director；lucky 含金额必有 `amount_cents` 且仅此时打 economy 标记）、04 §5.3（骰子 rng_seed）、06 §1.1（编剧发起 world.* 的 trigger 口径）
- 目标: 四类扰动每日按日历判定，骰子可复现，导演复用同一实现
- 交付物: `server/worldsim/world_agent/disturb.py`（新文件，偏差 D-13）；`server/tests/world_agent/test_disturb.py`
- 实现要点:
  - illness：每人每日概率（见 01 §6.1，配置镜像），payload `{agent_id, severity, days}`；请假标记（`world_state`）供校验器/排期豁免工作约束（01 §1.4）
  - weather：每日概率，payload `{kind, effect}`；`effect` 语义设计未定义 → 工程默认=效果码+短文案，邀约取消联动由 02 文档决策侧消费（偏差 D-14）
  - complaint：每日随机概率 + 编剧 L0 入口（后者 `trigger='director'`），payload `{floor, issue}`
  - lucky：每日概率，payload `{agent_id, kind, amount_cents}`（金额区间见 01 §6.1）；金额入账（正），财富/情绪变更（映射公式见 01 §1.5/§6.1）落 `state.needs_delta`；打 economy 审计标记（06 §1.2）
  - 全部骰子 seed 落 `events.rng_seed`；L0 导演发起走 T-DIR-03 同一实现、仅 `trigger` 不同（06 §1.1）
- 验收标准:
  1. `test_disturb.py::test_frequency`：固定 seed 连跑 60 模拟日，四类命中率与配置概率一致（统计容差断言）
  2. `::test_lucky_amount_and_audit`：lucky 事件 `amount_cents` 落配置区间、余额守恒含之、`config/event_types.yaml`（01 T-CFG-05 单源）中 lucky 标 economy（06 §1.2）
  3. `::test_illness_exemption`：illness 生效期内工作日工作约束豁免标记为真
  4. `::test_director_trigger_path`：以 `trigger='director'` 发起 complaint，事件类型仍 `world.disturb.complaint` 且计入干预率口径（06 §1.1）
- 实测回填: 四类扰动实测命中率 → 08 文档日报留痕（01 §9 调参旋钮校准输入）

### T-WA-10 事件注册表消费与对拍（`event_types.yaml` 单源消费）
- 里程碑 / 依赖: M3 ；01 T-CFG-05（M0，`event_types.yaml` 与 `gen_event_types.py` 唯一持有方）、T-DB（M0）、06 §1.2 冻结基线；估时 0.5d
- 设计依据: A2 单源裁定（round1 §A.2：`server/config/event_types.yaml` 是唯一手维护源、归 01 T-CFG-05，`gen_event_types.py` 派生链与 `--check` 亦归 01；禁止任何第二份手维护镜像）、04 §1.5（部署镜像口径）、04 §10.1 ①（`:economy_types` 注入口径）、06 §1.3（economy 清单由标记生成，禁 LIKE 前缀法）、06 §4.5（差集检查常设流程）
- 目标: 本模块全部 economy 审计/类型断言从单源 yaml 动态读值，零手维护镜像、零字面量
- 交付物: `server/tests/world_agent/test_event_types_conformance.py`（消费方对拍测试）。**本任务不交付生成器、不交付同名 yaml**（两者唯一归 01 T-CFG-05，A2；原自交付方案已撤销，偏差 D-15 更新）
- 实现要点:
  - economy 类型清单 = 从 `server/config/event_types.yaml`（01 T-CFG-05）读审计域标记 economy 的类型全集（含动作域结算类型，06 §1.3）；审计①（08 T-AUD）亦从该文件读清单，本模块不另存副本、不复制字面量进代码
  - 每次契约基线变更后跑 `gen_event_types.py --check`（脚本归 01 T-CFG-05）确认 yaml 与 06 §1.2 对拍一致；不一致时找 01 侧修复，本文档侧不改 yaml
  - 为 06 §4.5 差集检查供数：`schema-v1` 打 tag 前的类型差集由 09 文档 SMK 任务执行，单源 yaml 为比对基准
  - 测试一律从 yaml 动态读值（含类型计数——动态计数，不写死字面量）
- 验收标准:
  1. `cd server && uv run python scripts/gen_event_types.py --check` 退出 0（脚本与 yaml 均归 01 T-CFG-05，本任务纯消费）
  2. `uv run pytest tests/world_agent/test_event_types_conformance.py::test_economy_set`：从 yaml 读出的 economy 清单含 06 §1.2 全部 economy 标记类型；断言 06 §1.3 列举的动作域结算类型在列（反前缀法回归）
  3. `::test_all_types_covered`：类型数从 yaml 动态计数并与 06 §1.2 行数一致（不写死数字）；临时修改 yaml 后 `--check` 失败（负例）
- 实测回填: 无

### T-DIR-01 弧线状态机引擎与 `config/arcs.yaml` schema
- 里程碑 / 依赖: M3 ；T-WA-02、T-DB；估时 2d
- 设计依据: 01 §6.2（弧线 schema/并发上限/同模板冷却/超 max_days 强制 fail-forward）、01 §11.3（`payoff_beat` 与爽点枚举）、04 §2.1（`director/arcs.py`）、04 §5.2（`events.arc_id`）、01 §6.4（连续无 A 级自动启弧线）、04 §3.3（batch 段插队结算）
- 目标: 声明式弧线模板 + 可测试的状态机运行时
- 交付物: `server/worldsim/world_agent/director/arcs.py`；`server/config/arcs.yaml`（schema 与校验器）；`server/tests/director/test_arcs_engine.py`
- 实现要点:
  - arcs.yaml schema（pydantic）：逐字段承接 01 §6.2（`arc_id/title/stage_machine/fail_forward/intervention_budget/actors/min_days/max_days/payoff_beat`）；`payoff_beat.type` 限 01 §11.3 八类枚举
  - 运行时：每 tick 评估 hooks → enter/exit；条件用预注册 predicate 表（如 `count_events`/`relation_delta`/`need_below`），YAML 只许 `{pred, args}` 声明式引用，禁自由文本（工程默认 DSL，偏差 D-16）
  - 实例状态持久化 `world_state`（DDL 无弧线表，偏差 D-06）：当前 stage/进入时点/配额消耗；事件经 `arc_id` 关联（04 §5.2）
  - 并发上限与同模板冷却（口径见 01 §6.2）：超上限排队；冷却期内同模板拒绝重启
  - `min_days/max_days`：`<min` 不爆发、`>max` 强制 fail-forward 收尾（01 §6.2/§11.3），全程落事件
  - 弧线发起干预先扣 `intervention_budget`，统一经 T-DIR-03 落库
  - 每日检查：连续无 A 级达阈值（01 §6.4）→ 自动启动休眠弧线（优先久未推进者），并查 01 §11.2 发生器可点火者（点火走 T-DIR-03 L1）
  - batch 段协调：弧线在 batch 段排了 A 级事件时按 04 §3.3 在 batch 前插队结算（与 T-TIME 接口）
- 验收标准:
  1. `test_arcs_engine.py::test_schema_validation`：缺 `payoff_beat`/非法爽点枚举/缺 `fail_forward` 的模板拒绝加载
  2. `::test_state_machine_advance`：注入事件流驱动模板 S1→末态，hooks 逐次触发、事件带 `arc_id`
  3. `::test_fail_forward_on_max_days`：超 `max_days` 未推进自动走 `degrade_to` 分支且收尾事件落库
  4. `::test_concurrency_cap_and_cooldown`：超出并发上限排队；冷却期内同模板重启被拒（口径读配置）
  5. `::test_auto_start_on_a_drought`：构造连续无 A 级窗口，断言自动启动一条休眠弧线并落审计日志
- 实测回填: 无

### T-DIR-02 A1~A6 弧线骨架配置与逐弧线 pytest
- 里程碑 / 依赖: M3 ；T-DIR-01；估时 1.5d
- 设计依据: 01 §6.2（六条骨架表）、01 §11.3（铺垫天数区间/爆发事件/余波）、06 §1.2（`burst_event` 引用类型逐字）、00 §6（每弧线配 pytest 的可测试要求）
- 目标: 六条钦定骨架全部配置化，每条带状态机级测试
- 交付物: `server/config/arcs.yaml` 六模板；`server/tests/director/test_arc_a1.py` … `test_arc_a6.py`
- 实现要点:
  - 逐条把 01 §6.2 表格翻译成 predicate 声明（A1 暗恋链曝光 S1~S5；A2 竞聘接 T-WA-07；A4 债务接 `debts` 状态与 `social.repay_money`；A5 谣言接 gossip `cites` 链，深度口径见 01 §4.1）
  - `setup_days` 区间照 01 §11.3 写配置（部署镜像），测试从配置读区间、不复制数值
  - 每模板至少两例：主路径（铺垫→爆发→余波）；fail-forward 分支（注入阻塞条件 → `degrade_to`）
  - `burst_event` 类型名逐字 06 §1.2（如 `dialogue.confess`）
- 验收标准:
  1. `cd server && uv run pytest tests/director/ -k arc_a`：六文件 ≥12 例全绿
  2. `test_arc_a*.py::test_setup_days_window`：每模板断言 `<min` 不爆发、`>max` 强制爆发（区间读配置，口径见 01 §11.3）
  3. 六模板过 T-DIR-01 schema 校验（CI 加载断言）
- 实测回填: 无

### T-DIR-03 干预框架 `intervene.py`：L0~L2、L3 永禁、干预率滑窗
- 里程碑 / 依赖: M3 ；T-DIR-01、T-WA-09、T-MEM（M1）、T-DB；估时 1.5d
- 设计依据: 01 §6.3（分级定义/超线只许 L0/审计口径）、源方案 §4.8（KPI，06 §3 登记持有方）、06 §1.2（`director.intervene` payload；trigger=director 计干预率）、06 §1.1（编剧发起 world.* 口径）、04 §5.2（`interventions` 表）、01 T-CFG-02（干预率上限配置载体 = `config/models.yaml` `thresholds:` 段；04 §1.6 旧 env 名已作废）、00 §4 红线 13
- 目标: 全部编剧动作唯一入口：分级、可审计、有硬顶
- 交付物: `server/worldsim/world_agent/director/intervene.py`；`server/tests/director/test_intervene.py`
- 实现要点:
  - API `intervene(level, action, arc_id?, reason, params)`：L0 → 调用 WA 扰动/公告/`stock_shock` 实现；L1 → 排期原语（团建/加班/同厨时段/同项目组/值班同班，复用 T-WA-08 排期表；01 §11.2 发生器点火与 01 §9 调参手册入口）+ `world.company_crisis`（频次上限见 01 §6.1）；L2 → 「世界观察」记忆注入（含 tension 修复助推——01 §6.3 指定的唯一合法阻尼路径），经 T-MEM 接口写入
  - L3 永禁：模块不暴露任何改关系/需求数值、强制台词的 API（01 §6.3；数值结算只经 T-ADJ）
  - 落库口径：**每个干预动作恰一条 `trigger='director'` 事件**（L0=world.* 事件本体；L1/L2=`director.intervene`，payload `{level, arc_id?, reason}` 逐字 06 §1.2，`text_display?` 可选）+ 一条 `interventions` 行（`event_seq` 互指，04 §5.2）——防干预率重复计数（偏差 D-17）
  - `intervention_rate_7d()`：与审计④同 SQL 口径、同数据源（04 §10.1 ④；阈值见源方案 §4.8，上限读 `config/models.yaml` `thresholds:` 段——01 T-CFG-02 唯一载体，与审计④读同一配置源，不进 env）；超线后 L1/L2 拒绝、仅放行 L0（01 §6.3）
  - `world.layoff_rumor` 规则触发不经本模块（T-WA-06）
- 验收标准:
  1. `test_intervene.py::test_l0_l1_l2_persist`：三级各一例，事件+`interventions` 行落库且 `event_seq` 互指；`director.intervene` payload 键逐字 06 §1.2
  2. `::test_no_double_count`：单动作产生的 `trigger='director'` 事件恰一条
  3. `::test_l3_impossible`：静态扫描 `director/` 无 `UPDATE relations`/`UPDATE agents` 语句；公开 API 表面无数值改写类函数
  4. `::test_rate_cap_lockdown`：构造 7 日事件流使干预率超上限：L1/L2 抛拒绝、L0 放行；回落后解锁
  5. `::test_rate_matches_audit_sql`：本函数与 04 §10.1 ④ SQL 对同一数据集结果相等（与 T-AUD 对账）
  6. `::test_budget_enforcement`：构造弧线 `intervention_budget` 配额耗尽，该弧线新干预经本入口被拒（自 T-DIR-01 验收 5 移归：拒绝方为本任务，构建序 DIR-01 先于 DIR-03）
- 实测回填: 无

### T-DIR-04 grade 阈值配置与联调验证（R1~R4 初值实现归 02 T-ADJ-07）
- 里程碑 / 依赖: M3 ；02 T-ADJ-07（M1，grade 初值唯一实现 `adjudicator/grade.py`，管道 step5 同事务，A6）、T-WA-01（阈值段落承载与校验）；估时 1d
- 设计依据: A6 裁定（round1 §A.6：grade 初值打分唯一归 02 T-ADJ-07，本文档删初值实现）、01 §6.4（判定标准唯一持有方）、04 §6.6（工程落点：同事务/命中数定级/append-only）、06 §2（`ui.grade` 字段定义）、00 §4 红线 4
- 目标: R1~R4 阈值与命中数→等级映射全部落配置（零硬编码），并联调验证 T-ADJ-07 初值实现全事件覆盖
- 交付物: `server/config/world.yaml` director.grade 阈值段（向既有 world.yaml 追加 `director.grade` 段，不重建，R1 §A.8 同法；划界声明：01 T-CFG-03 已覆盖——其划界补「director.grade 阈值段归 T-DIR-04 追加」；schema 并入 T-WA-01 config.py 校验）；`server/tests/director/test_grade_integration.py`（联调测试）
- 实现要点:
  - 阈值配置：R1 卷入计数 / R2 关系变更 / R3 情绪摆动 / R4 后续链四规则的判定阈值与命中数→等级映射全部镜像 01 §6.4（数值见持有方，配置为部署镜像，代码零硬编码）
  - R1「gossip cites 波及者」推导口径（被引记忆 `agent_id` ∪ 其 `source_event_seq` 事件 `actors` ∪ `payload.about`）随实现归 02 T-ADJ-07（偏差 D-18）；本任务只在配置侧给阈值、在联调中锁定口径
  - 联调对象接口：T-ADJ-07 在 step5 结算后、聚合落库前消费 tick 内缓冲打分（不改变 04 §6.5 每 tick 聚合口径），`ui.grade` 随 INSERT 写入、此后该行永不 UPDATE（04 §5.2 触发器兜底）；本任务不实现打分器、不加管道钩子
  - 联调场景：mock 世界连跑，断言全事件（含 C）落库即带 `ui.grade` 初值；改配置阈值后同输入定级分布随之变化（配置生效链路）
- 验收标准:
  1. `test_grade_integration.py::test_full_coverage`：mock 世界跑 1 模拟日，`SELECT COUNT(*) FROM events WHERE ui ? 'grade'` = 当日事件总数
  2. `::test_thresholds_from_config`：断言打分器实际读本配置段——改阈值后同输入定级变化符合映射；配置缺键/区间倒置拒绝启动（T-WA-01 校验链路）
  3. `::test_written_same_txn`：INSERT 后 `ui->>'grade'` 非空；尝试 UPDATE 被触发器拒绝（04 §5.2）
  4. `::test_boundary_mapping`：命中数边界各一例，映射与 01 §6.4 一致（期望值从配置读，不写字面量）
- 实测回填: 候选 A 级日产量分布（K3 复核输入规模，联调运行产出）→ 08 文档日报留痕（04 §8.1 director 限额校准输入）

### T-DIR-05 编剧 K3 复核与 `director.grade_revise`
- 里程碑 / 依赖: M3 ；T-DIR-04、T-DIR-03、T-LLM（M2，可 mock 先行）；估时 1.5d
- 设计依据: 01 §6.4（K3 复核：每日批量 1 次/两条编辑维度/上调每日上限/下调）、04 §6.6（追加事件口径/有效 grade 定义）、06 §1.2（`director.grade_revise` payload 与 intervention 标记）、05 §3.8（副本侧 `event_grade_view`，M6 范围）、04 §5.1（`target_seq` 值形态）、04 §8.1（director 路由不降级换模型）
- 目标: 每日一次批量终审，改判只追加事件，events 永不 UPDATE
- 交付物: `server/worldsim/world_agent/director/review.py`（新文件，偏差 D-13）；K3 复核 prompt 模板（挂 M2 模板机制）；`server/tests/director/test_grade_revise.py`
- 实现要点:
  - 复核时点：每模拟日批量 1 次（工程默认=凌晨 batch 段结算后对刚结束模拟日，偏差 D-19）；输入=当日 `ui.grade` 初值候选集摘要；K3 按两条编辑维度终审（口径见 01 §6.4，爽点维度见 01 §11.3）
  - 执行：上调 B→A 每日上限（见 01 §6.4，配置项）硬截断并 WARN；下调 A→B；每条改判落 `director.grade_revise`，payload `{target_seq, new_grade, reason}` 逐字 06 §1.2，`target_seq`=裸 seq 数字字符串（04 §5.1），事件带 `arc_id`（04 §6.6）
  - `source='director'`、`trigger='director'`（计干预率，量极小口径见 06 §1.2 注释）；同落 `interventions` 行
  - 有效 grade：本机侧 helper `effective_grade(seq)` = `ui.grade` ⊕ 最新 `director.grade_revise`（04 §6.6），供 T-AUD 健康度指标与后续观察端；副本侧物化在 M6（见 §2）
  - K3 失败/超时：任务排队延后（04 §8.1），当日可跳过但 WARN 落审计日报；M3 内以 mock K3 跑通，真接入在 M2 路由就绪后切换
  - 降速读取点：成本熔断 ThrottleState 生效期，director 干预动作（含 K3 复核批次）按 04 §8.4 减半执行；判级器/ThrottleState/`system.llm.throttle` 事件唯一归 03 T-LLM-08，本任务的读取点接线归 08 T-OPS-02（A5；本任务只留读取点与行为口径）
- 验收标准:
  1. `test_grade_revise.py::test_revise_event_payload`：mock K3 输出 → 事件三键逐字、`target_seq` 匹配 `^[0-9]+$`（非 `'e<seq>'` 形态）
  2. `::test_daily_up_cap`：构造超量上调建议，当日落库上调数 ≤ 配置上限，其余 WARN 留痕
  3. `::test_no_update_on_events`：复核后原事件行 `ui.grade` 不变（append-only）
  4. `::test_effective_grade`：初值 A+revise B → 有效 B；初值 B+revise A → 有效 A；未复核 → 初值
  5. `::test_intervention_accounting`：revise 事件计入 `intervention_rate_7d()`（`trigger='director'`）
- 实测回填: 每日上调/下调实际分布 → 08 文档日报留痕（保稀缺性口径复核，01 §6.4）

### T-DIR-06 M3 集成演练：连续 7 模拟日出口验证
- 里程碑 / 依赖: M3 ；本文档全部任务、T-AUD（08 文档）、T-TIME/T-ADJ（M1）；估时 1.5d
- 设计依据: 00 §5 M3 出口（连续 7 模拟日/审计 6 项全绿/A 级 KPI）、01 §6.4（A 级 drought 自动启弧线）、04 §10.1/§10.2、04 §13 W2 清单④、04 §5.3（回放语义）
- 目标: 全模块合流，M3 出口标准可复跑验证
- 交付物: `server/tests/integration/test_m3_sim_week.py`；向 08 T-OPS-05 提交 smoke.sh `m3` 段需求条目（A10：smoke.sh 唯一创建者 = 08 T-OPS-05，M6 按 09 §8 实现；本文档不自建 smoke.sh 段）
- 实现要点:
  - mock LLM（M1 机制）连跑 7 模拟日：日历全类型按日历出现（构造覆盖发薪/账单/股价/加班/团建/扰动/绩效）
  - 审计联动：04 §10.1 六项 SQL（T-AUD）对演练库全绿；干预率 7 日滑窗低于红线（阈值见源方案 §4.8）
  - A 级 KPI：每日有效 grade='A' 达标（口径见 00 §5；有效 grade 见 04 §6.6）；构造 drought 窗口验证自动启弧线（01 §6.4）
  - `WSIM_REPLAY_MODE` 重放 7 日：股价/扰动骰子序列一致（04 §5.3 已统一该名；取值口径见 00 §2 附表，D-21 销项）
  - 产出实测回填表（见 §5）
  - smoke.sh `m3` 段需求条目内容（提交 T-OPS-05）：封装调用 `uv run pytest tests/integration/test_m3_sim_week.py`、断言实测回填表写入审计日报；M6 由 T-OPS-05 落地
- 验收标准:
  1. `cd server && uv run pytest tests/integration/test_m3_sim_week.py` 全绿（7 模拟日无人值守跑完）
  2. SQL：审计 6 项（04 §10.1）全绿，结果落审计日报
  3. SQL：A 级间隔 7 日均值达标（阈值见 01 §9；有效 grade 口径 04 §6.6）
  4. 类型覆盖断言：本模块全部事件类型在演练窗内至少出现一次（节假日/季度项按构造日历触发）
  5. smoke.sh `m3` 段需求条目已提交 08 T-OPS-05 并获接收（含调用命令/断言口径/回填落日报要求）；M6 smoke.sh 就绪后 `bash server/scripts/smoke.sh m3` 退出 0 由 08 文档验收
- 实测回填: M3 回填总出口——`|r|` 分布与传闻触发频率 → 04 §13 W2 清单④；干预率实测 → 源方案 §4.8 KPI 留痕；A 级间隔 → 01 §9 校准输入；扰动命中率 → 01 §6.1 校准输入（均落审计日报）

## 5. 风险与实测回填清单

**风险**：

| # | 风险 | 对策 |
|---|---|---|
| R1 | 日历结算漏触发/重复触发 → 审计①余额守恒红灯 | 全部日历事件走 `run_batch_calendar` 单入口 + 注册回调表（T-WA-02）；T-DIR-06 类型覆盖断言兜底 |
| R2 | K3 复核不可用 → 当日 A 级 KPI 失守 | 自动打分初值保底（02 T-ADJ-07 实现，阈值配置与联调见 T-DIR-04）；复核失败排队延后 + WARN（04 §8.1）；KPI 以有效 grade 计 |
| R3 | 扰动/股价概率实测偏离设计 → 过频淹没内容或过稀无戏 | 实测回填后按 01 §9 调参手册一次只拧一个旋钮；配置镜像零硬编码（T-WA-01） |
| R4 | 弧线状态机死锁（enter/exit 条件永假） | `max_days` 强制 fail-forward 兜底（01 §6.2）+ 每模板 pytest（T-DIR-02） |
| R5 | 干预率统计口径与审计④不一致 → 红线误判 | T-DIR-03 验收 5 与 T-AUD 同数据集对账 |
| R6 | 工程默认（D-03~D-19）与设计后续修订冲突 | 全部登记 §6，登记处即本文偏差表（00 §1 只收全局条目）；设计修订时逐条复核 |

**实测回填清单**：

| 回填项 | 产生任务 | 登记位置 |
|---|---|---|
| 股价 `\|r\|` 分布与传闻阈值触发频率 | T-WA-05/T-WA-06/T-DIR-06 | 04 §13 W2 清单④；审计日报 |
| 干预率 7 日滑窗实测 | T-DIR-03/T-DIR-06 | 源方案 §4.8 KPI 留痕；审计日报 |
| A 级间隔实测（有效 grade 口径） | T-DIR-05/T-DIR-06 | 01 §9 校准输入（阈值只改 01） |
| 四类扰动实测命中率 | T-WA-09/T-DIR-06 | 01 §6.1 校准输入；审计日报 |
| 候选 A 级日产量分布 | T-DIR-04（联调运行产出；初值实现归 02 T-ADJ-07） | 审计日报（04 §8.1 director 限额校准） |
| grade 复核每日上调/下调分布 | T-DIR-05 | 审计日报（01 §6.4 稀缺性复核） |

## 6. 适配与偏差

> 以下逐条登记本模块与设计/现实的偏差。凡标「已登记（本文偏差表）」者为设计未定义的工程默认值——其登记处即本表（00 §1 只收全局条目，R2 §A.12 收口口径）；涉契约者同时建议回登 06（冻结后走 06 §4 流程）。

| # | 偏差 | 处置与位置 |
|---|---|---|
| D-01 | 已转正（v1.2 回登）：01 §6.1 `promotion.window` 模板已删 `defense_at` 键，与 06 §1.2（从未登记）一致 | 答辩排期经 `world.announce` body 承载 + 弧线钩子呈现（T-WA-07）；不再是偏差，销项留档 |
| D-02 | 已销项（06 v1.2）：`economy.bill.utility.overdue` 已补登注册（镜像 rent 链：`agent_id`/`amount_cents`/`overdue_cents`/`period`，economy 审计） | utility 欠费落 `economy.bill.utility.overdue`（T-WA-04）；`economy.settle` 回归兜底定位（仅无注册类型的结算场景） |
| D-03 | 已转正（v1.2 回登）：04 §3.3 注释已补「8:00 打卡为内部结算、不产事件」 | 实现为内部出勤状态结算，不落事件（T-WA-02）；如未来需事件化先登 06 |
| D-04 | `time.day_summary` 触发时点设计未定义 | 工程默认：sim 日期翻转时恰一条，`payload.day`=自世界纪元模拟日序号（T-WA-02）；生产方=world_agent 日历日界钩子（06 §1.2 v1.2 注）；已登记（本文偏差表） |
| D-05 | 已转正（v1.2 回登）：01 §6.1 春节三子事件已标注「阶段 1 defer，以摘要记忆口径呈现」，无需注册事件类型 | M3 不实现（见 §2）；销项留档 |
| D-06 | DDL（04 §5.2）无以下持久化位置：每人月薪、欠租/欠费挂账、弧线实例状态与冷却、裁员候选池、请假/天气标记 | 一律 `world_state` KV；月薪初始值由 M0 seed 按档位抽定落入（T-WA-03 只读）；已登记（本文偏差表） |
| D-07 | 周末是否休市设计未明示（01 §6.1 仅言节假日休市） | 工程默认周末休市（对齐现实市场，T-WA-05）；已登记（本文偏差表） |
| D-08 | `world.layoff_rumor` 无冷却规则 | 按字面逐日判定（连续过线每日一条，T-WA-06）；已登记（本文偏差表） |
| D-09 | 晋升 `candidates` 产生规则设计未定义 | 默认该部门全体 P1（T-WA-07）；已登记（本文偏差表） |
| D-10 | 绩效 S~C 评分公式与参评范围设计未定义 | 工程默认：系统指标（出勤/工作按期/加班参与）加权 + 部门内分层映射，全员参评（T-WA-07）；已登记（本文偏差表）；后续可换经理 NPC 初评（01 §1.3） |
| D-11 | 已销项（06 v1.2）：`world.perf_review` 行已注 `delta_salary`=BIGINT 分、月薪差额绝对值（按 01 §1.5 百分比档×当前月薪） | T-WA-07 按 06 §1.2 注释口径实现，不再登记单位偏差 |
| D-12 | `world.overtime` 的 `participants` 选取规则设计未定义 | 默认该部门当日未请假员工（T-WA-08）；已登记（本文偏差表） |
| D-13 | 新文件 `disturb.py`、`director/review.py` 不在 04 §2.1 目录注释列举内（注释仅列 calendar/economy/arcs/intervene 四文件）；原列的 `director/grade.py` 随 A6 改归 02 T-ADJ-07（`adjudicator/grade.py`），不再属本文档 | 仍属 `world_agent/` 与 `world_agent/director/` 两目录，模块语义不变；已登记（本文偏差表） |
| D-14 | `world.disturb.weather` 的 `payload.effect` 语义未定义 | 工程默认=效果码+短文案，邀约取消联动由 02 文档消费（T-WA-09）；已登记（本文偏差表） |
| D-15 | `scripts/gen_event_types.py` 与 `config/event_types.yaml` 不在 00 §2 仓库布局列表（后者 04 §1.5 已列） | A2 裁定：两者唯一持有方=01 T-CFG-05（event_types.yaml 手维护单源，gen_event_types.py 派生链与 `--check`）；本文档 T-WA-10 改纯消费，撤销生成器与同名 yaml 交付；00 §2 布局登记由 01 侧完成 |
| D-16 | 弧线条件表达式 DSL 设计未定义 | 预注册 predicate 表 + YAML 声明式引用（T-DIR-01），禁自由文本；已登记（本文偏差表） |
| D-17 | `director.intervene` 与 `trigger='director'` 的 world.* 是否双落，设计未明 | 口径：每个干预动作恰一条 `trigger='director'` 事件 + 一条 `interventions` 行，防干预率重复计数（T-DIR-03）；已登记（本文偏差表） |
| D-18 | grade R1「gossip cites 波及者」推导路径设计未细化 | 工程解释：被引记忆 `agent_id` ∪ 其 `source_event_seq` 事件 `actors` ∪ `payload.about`；随初值实现归 02 T-ADJ-07（A6），本文档仅持阈值配置与联调（T-DIR-04）；已登记（本文偏差表） |
| D-19 | K3 复核时点设计仅「每日批量 1 次」 | 默认凌晨 batch 段结算后对刚结束模拟日执行（T-DIR-05）；已登记（本文偏差表） |
| D-20 | 任务简报所标「01 §3.2 经济数值（薪资/房租/账单）」（01 旧名）与 01 实际结构不符：薪资/房租/账单持有方为 01 §1.5，01 §3.2/§4 仅持债务还款期限与逾期关系行（数值见持有方，与 06 §3 登记一致） | 本文档按 01 §1.5 引用薪资/房租/账单、按 01 §3.2/§4 引用债务口径；提请修订简报口径，实现不受影响 |
| D-21 | 已销项：设计文档 04 §5.3 已统一为 `WSIM_REPLAY_MODE`（R1 §A.13，已入 00 §2 附表） | 本文档行文统一用 `WSIM_REPLAY_MODE`；不再是偏差，销项留档 |
| D-22 | 节假日表的具体日期（MM-DD）设计未给（01 §6.1 只有名称/天数） | 工程默认按 2026 自然年实历映射（春节 02-17/中秋 09-25 等），落 `world.yaml` `holidays.table`（T-WA-01）；已登记（本文偏差表） |
| D-23 | `world.disturb.illness` 的 severity→请假天数设计未给 | 工程默认：severity 1→1~2 天、2→2~4 天，配置镜像 `triggers.disturb.illness_severity_days`（T-WA-01/09）；已登记（本文偏差表） |
| D-24 | `world.overtime` 的 `reason` 与 `world.team_building` 的 `activity` 文案池设计未枚举 | 工程默认模板池落配置（`triggers.overtime.reasons`/`triggers.team_building.activities`，T-WA-01/08）；编剧/弧线可传参指定；已登记（本文偏差表） |
| D-25 | 挂账期间"余额充足自动补扣清账"的部分补扣设计未定义 | 工程默认：足额才扣（余额 < 挂账额不动，防半清状态歧义）；每日 09:30 扫描（`economy.overdue.daily`，时点为工程默认）；已登记（本文偏差表） |
| D-26 | 8 人小世界（seed_8）无部门经理 NPC（M 级全为 16 名 NPC，01 §1.3），`world.perf_review.manager_id` 无真实人可指 | 工程默认：无经理时 `manager_id=null`（键保留）；40 人 seed 起指真实经理；已登记（本文偏差表） |
| D-27 | `world.promotion_window`/`world.perf_review` 的触发时点（日内 HH:MM）01 §6.1 只给日期规则 | 工程默认：promotion_window 10:00、perf_review 16:00（01 §6.1 模板行注明 16:00；晋升窗口取公告时段）；落配置 `triggers.*`；已登记（本文偏差表） |
| D-28 | 01 §6.2 fail_forward `if_blocked` 与 stage exit 的判定先后设计未定义 | 工程默认：exit 优先（exit 满足=未阻塞），其后 if_blocked，max_days 兜底最先判；爆发铺垫门禁操作口径 = `payoff_beat.setup_days`（01 §11.3 权威），`min_days/max_days` 为镜像；已登记（本文偏差表） |
| D-29 | L1「同厨时段/同项目组/值班同班」（01 §6.3）的承载形态设计未细化 | 工程默认：落 `world_state` `schedule.co_location` 标记（02 文档决策侧消费），不直接改排程表；`world.company_crisis` 季度 ≤1 次上限以 `world_state` `company_crisis.last_at` 执行（01 §6.1）；已登记（本文偏差表） |
| D-30 | `director.grade_revise` 同落 `interventions` 行时 `level` 取值设计未定义（CHECK 仅 L0/L1/L2，04 §5.2） | 工程默认：`'L2'`（编辑终审口径）；干预率统计只看 `trigger` 字段（红线 13），不受 level 取值影响；已登记（本文偏差表） |
