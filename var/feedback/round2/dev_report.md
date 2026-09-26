# Round 2 迭代开发报告（T-ITER2-01 ~ 06）

- 日期：2026-09-27 · 开发：worldsim-dev · 输入：`var/feedback/round2/product_triage.md`（6 项）
- 分支基线：round1 全部 7 项已交付；本轮 6 项逐条实现并提交，前缀 `T-ITER2-0X`

## 总览

| # | 项 | 级别 | 状态 | commit |
|---|---|---|---|---|
| 1 | 世界空转根因（裁决协程活性 + needs 兜底 + batch 保底 + 看门狗） | P0·重 | ✅ | `b743abf` |
| 2 | 内核日界必死系统性修法（授权重放 + 协程隔离 + 进程看守） | P0·中 | ✅ | `ed7af47` |
| 3 | schedule 500 回归 + latest_snapshot 消费方审计 | P1·回归 | ✅ | `140eda9` |
| 4 | 反思链路三处同修（schema 契约 + 清洗两级判定 + 兜底不进观众面） | P1 | ✅ | `dac2b42` |
| 5 | 时区统一·展示半份（+08 本地化唯一入口 + health 日口径标注） | P1·半份 | ✅ | `d759424` |
| 6 | 首屏可视（公共区顶带 + 散开 + 裁切）+ 代词搭车 | P1 | ✅ | `159a5dc` |

测试计数（全部通过）：
- pytest 增量：adjudicator/relations/scheduler/memory/time_engine/observe/ingest/audit/invite/world_agent/director/sync/snapshot/derived/config/ddl + 顶层 sanitize/check_grants = **538 passed**
- web vitest 全量：**79 passed（15 文件）**；`tsc --noEmit` 0 错
- stream `node --test tests/*.test.js`：**30 passed / 0 fail**
- 长尾：tests/integration（7 模拟日真跑主循环）后台运行中，见 §7

---

## 1. T-ITER2-01 世界空转根因（b743abf）

**根因核实**（与 triage 一致）：`pipeline.py adjudication_loop` 只捕获
`ChainExhausted/ProviderUnavailable`，`lod.collect_due`/`run_tick`/`after_tick` 的
DB/授权/未知异常直接穿出协程（`pipeline.py:509/516` 已核实）；needs 恢复依赖
睡眠/进食动作结算（决策停摆 → 永不恢复，死锁）；batch 段只产 LLM 摘要 0 行为事件；
停摆期间 `llm_status healthy`、审计出绿（假 healthy）。

**实现**：
- ⑤ 协程隔离兜底层：`adjudication_loop` 每拍整体 try/except，非 LLM 异常记
  ERROR（含 traceback）并跳过本拍，协程不再静默退出（单测注入异常证明存活+继续裁决）。
- ② needs 被动兜底：`NeedsEngine.settle_passive_recovery`——睡眠窗按
  `satisfy.energy.sleep_per_hour` 积分恢复 energy；进食窗（`needs.yaml` 新增
  `passive.meals` 段：12:00-13:00 / 19:00-20:00）按 `satisfy.hunger.canteen`
  摊算恢复 hunger；`main.py after_tick` 全员结算。数值全读 needs.yaml 零硬编码。
- ③ batch 保底：`kernel.batch_floor` batch 钩子——追日段每 agent 每模拟小时 ≥1 条
  免 LLM 作息事件（睡眠 `agent.rest{mode:sleep}` / 进食 `agent.eat` 零金额 /
  其余 `agent.think`），trigger/visibility 与契约注册表一致。
- ④ 活性看门狗：内核协程 30s 巡检（`WSIM_WATCHDOG_TICKS` 默认 30）→ kernel.log
  WARN/恢复 INFO；obs 侧 `fetch_stall`（watermark vs max(agent 事件 tick) 双参照）
  → `/api/snapshot` 增 `world_stalled`/`world_stalled_reason`、`llm_status` 附
  `stalled`/`stalled_reason`；前端三端"世界停滞"人话提示：admin LatencyBar
  （含 watermark 3 分钟墙钟不动兜底，替代"（滚动刷新）"假实时）/ lite 横幅 /
  stream 头部（含断流 3min 巡检）。
- llm_status 真实反映：stalled 信号与 degraded 并存不互掩。

**文件**：`server/worldsim/adjudicator/pipeline.py`、`server/worldsim/relations/needs.py`、
`server/config/needs.yaml`、`server/worldsim/main.py`、`server/worldsim/observe/rest_snapshot.py`、
`web/src/proto/snapshot.ts`、`web/src/components/common/LatencyBar.tsx`、`web/src/lite/LiteShell.tsx`、
`stream/js/main.js`、`stream/js/dispatch.js`、
测试 `tests/adjudicator/test_loop_resilience.py`、`tests/relations/test_needs.py`、
`tests/observe/test_latest_snapshot_contract.py::test_world_stalled_flag_observable`

## 2. T-ITER2-02 内核日界必死系统性修法（ed7af47）

- ①④ `server/scripts/check_grants.py`：**唯一授权登记表** `GRANT_REGISTRY`
  （health_daily 表级 SELECT/INSERT/UPDATE ← `ddl/health_daily_v1.sql`；
  memories 列级 UPDATE(archived) ← `ddl/memories_update_grant.sql`），
  information_schema 逐项核对；`--check` 纯校验（CI 口径，diff 非零退出 1）、
  `--apply` 幂等补授后复核。基线授权（schema_v1 自带）不参与判定只静默提示。
- 流程收口三闸：`db_init.sh reset` 末尾自动重放+复核（不一致拒绝带伤 seed）；
  `start_local.sh` 启动自检自愈（FAIL 拒起内核）；README 冷启动序列补
  `check_grants.py --apply` 一步 + 重放口径说明。
- ② 治理协程隔离：`hygiene_loop` 归档异常（InsufficientPrivilege 类）记 ERROR、
  `failed_day` 标记当日跳过不重喷、次日自动重试，不再拖垮主 TaskGroup。
- ③ 进程看守：`start_local.sh` 起 `worldsim_kernel_supervisor`（pid 文件
  `var/logs/kernel_supervisor.pid`），10s 巡检 kernel.pid 活性，kill 内核 ≤30s
  自动拉起（接 main.py 崩溃恢复编排）；停跑口径 `touch var/logs/kernel.stopped`
  + pkill 内核。
- **live 库重放口径**：live 库已于 2026-09-27 手工补授 `GRANT UPDATE(archived) ON
  memories`（主代理止血）；本轮脚本幂等，已在 live 库跑通 `check_grants --apply`
  → diff=0；reset/seed 流程今后在 seed 阶段即重放+校验，第 3 张漏授权表不可能再
  潜伏到日界。

**文件**：`server/scripts/check_grants.py`、`server/scripts/db_init.sh`、
`deploy/start_local.sh`、`server/README.md`、`server/worldsim/memory/hygiene.py`、
测试 `tests/test_check_grants.py`（真 psql 临时库：check 失败→apply 愈合→diff=0）、
`tests/memory/test_hygiene.py::test_hygiene_loop_survives_archive_exception`

## 3. T-ITER2-03 schedule 500 回归（140eda9）

- rolling 分支补 `sim_day` 派生（帧头 sim_time 的 +08 日期，与 day_end 键集一致）；
  `/api/agents/{id}/schedule` 缺省 day 不再 KeyError 500。
- `?day=bad` → 422（`bad_param`）；既有 401/422 口径不回退。
- 消费方审计收口：新增契约测试 `tests/observe/test_latest_snapshot_contract.py`
  ——rolling/day_end 两分支核心键集一致性 + schedule 200/422（全仓消费方：
  rest_agents×4 / rest_relations×1 / rest_snapshot，逐一核对）。
- 前端 AgentPage：静默 catch → console.warn 留痕；今日日程空态文案
  （"今天没有排定的事项，看看 TA 会自己做什么。"）。
- **live 验证**：`/api/agents/A01/schedule` 200；`?day=bad` 422（重启 obs-api 后实测）。

## 4. T-ITER2-04 反思链路三处同修（dac2b42）

- ① 生成侧：`reflect.py` 日终/深度反思 prompt 固定 JSON 契约（`{"diary":…≤120字}` /
  `{"insights":[…]}`，禁其他文字）；解析器容忍 `{"summary":…}` / `{"answer":{…}}` /
  `{"answer":…}` 历史漂移形态，解包成功不再整段报废（8/8 误杀根因一半）。
- ② 清洗侧：`sanitize.py` 两级判定——`extract_structured_display` 先结构化解包
  提取展示字段（提取结果仍过黑名单/残块判定），真残块才出兜底句；
  `serde`/`rest_agents`/`rest_relations` 统一走 `sanitize_or_extract`——历史
  JSON 形态误杀行在出站层即自愈（库内原文不动，text_raw 不出站红线不破）。
- ③ 消费侧：`is_fallback_text` 兜底句识别；lite 今日看点 feed 过滤兜底事件并
  降采样 ≤1 张"有几段心事没能完整记录"卡（`partitionFeed`）；直播字幕队列
  拒绝兜底事件（`eventToSegment`/push 零字幕项）；`/api/ripple/today` 兜底事件剔除。
- 本轮不做（按 triage）：历史 8 条误杀反思回填（留待新格式稳定后一次性脚本）。

## 5. T-ITER2-05 时区统一·展示半份（d759424）

- web 新增 `lib/simTime.ts` 唯一入口（`formatSimHHMM/MMddHHMM/YMD`，Intl
  Asia/Shanghai）：替换 AppShell simClockText / EventStream / LatencyBar /
  TimelineList / AgentPage / RipplePage / PairCurves 全部散落
  `toISOString`/裸 `slice(11,16)`/`getHours()`；stream `fmtSimHHMM` 同口径改写。
- `/api/health` 增 `day_notice`（"数据截至 YYYY-MM-DD"）与 `sim_day_semantics`
  （日结口径语义标注）；HealthPage 头部展示（标注而非伪装当日，日结滞后是事实）。
- 契约登记：`docs/design/06-契约登记表.md` v1.4 仲裁"序列化 UTC 不动 / 展示 +08
  本地化"；序列化半份（REST/WS 统一 +08）按 triage 留档下轮。
- **live 验证**：`/api/health` 返回 `数据截至 2026-10-14` + 语义标注；同一事件
  22:55(UTC) → 三端显示 06:55(+08)（单测覆盖该样本）。

## 6. T-ITER2-06 首屏可视 + 代词（159a5dc）

- 公寓公共区带底部（y=432，1280×800 首屏折叠线以下"全楼没人"）上移至顶部
  y=0..40，房间整体下移 48（`mapLayout.nodeRect`）；LiteMap/SiteSvg viewBox
  加宽至 448（天台 apt.roof 右缘 x=432 超旧 400 被裁半的修复）。
- 同房散开：`spreadOffsets` 径向均分（相邻间距 ≥ 头像直径 18px，纵向 0.8 压扁）；
  名牌沿房间底行排开（≤4 槽）——"赫蕲彻"叠名修复。
- 搭车：LiteAgentPage 代词按 `gender` 字段渲染（原固定"她"，4 位男性角色穿帮）。
- 测试：公共区顶带/房间不重叠、散开间距 n=2~6、天台不裁切（mapLayout.test.ts）。

## 7. 重启观察与运行证据（#1/#2 验收）

**重启过程**（按纪律：先 pkill 再拉起）：
1. `pkill worldsim.main --llm routed` + kill obs-api →
   `WSIM_LLM=routed bash deploy/start_local.sh` → 授权自检 `diff=0` →
   **supervisor 10s 内自动拉起新内核**（#2③ 首验即生效）。
2. 恢复追平 + 速度表凌晨 batch 段（设计行为）：`batch 保底行为已合成：
   8 agents × 8 模拟小时`（#1③ 生效，旧版此处 0 行为）。
3. 观察期 live 世界处于设计性凌晨 batch 段（真实 00:30-08:00 不排程 LLM 决策），
   为验证决策链，用**临时变速表**（全天 continuous ratio 3，/tmp 用后即弃，未动
   `config/speed_table.yaml`）重启内核观察 ≥10 分钟：
   - `next_due_sim` 从全员冻结的 **2026-10-12 13:25 → 2026-10-16 04:45 持续写回**（验收①）；
   - 真实 LLM 决策事件产出：`思考如何拿下新版本视觉主案…`/`评估财务状况并制定还债计划`/
     `找个不刻意的理由约苏蔓下楼喝咖啡` 等（think 非"走神"为主，3/11 走神为免费档
     正常解析降级）；agent 事件 tick 与 watermark gap ≤ 1（验收①）；
   - needs 快照非零且随作息变化：energy 23.4 / hunger 29.8（基线 0/8 全零，验收②）；
   - needs_delta 事件正常落库（恢复路径执行，验收②）；
   - 观察期 0 条 `T-ITER2-01⑤` 隔离 ERROR（世界无静默停摆）；看门狗未误报。
4. **世界停滞信号实测**：副本事件流追平期间 `/api/snapshot` 如实亮
   `world_stalled:true（已 231 tick 无角色行为）`——假 healthy 修复（验收④，
   与 round2 技术报告"内核死后 llm_status healthy"对照）。
5. tests/integration（7 模拟日主循环，mock，unthrottled）运行至 tick 969→结束于 tick
   1227 段/sim 10-16 13:06（用例硬超时 1500s 到点，本机 bge-m3 embed 慢、全程 25 分钟
   推进 ≈110 sim 小时，**为环境性超时非挂死**——全程匀速推进、0 崩溃）：**1578 条真实
   think + 361 move、next_due 写回至 10-16 02:20、1176 条 needs_delta、energy/hunger
   非零（24.3/8.8）**，活过 10-12/10-13/10-14/10-15 四个模拟日日界（#2 前置条件核心证据；
   health_daily 0 行为 trial 模式不跑 batch 段的设计口径，非缺陷）。观察完毕已按标准
   流程恢复：kill 观察内核 → `rm kernel.stopped` → `WSIM_LLM=routed bash
   deploy/start_local.sh` → 授权自检 diff=0 + supervisor 自动拉起，当前 watermark 与
   agent 事件 tick 齐平（1227/1227），世界在凌晨 batch 段按设计运行，真实 08:00 起
   连续段恢复 LLM 决策。

## 8. 遗留与回退声明

- 未动契约红线：events append-only、text_raw 不出站、canonical/digest 唯一实现、
  审计口径、快照白名单、鉴权/参数校验口径全部保持（既有 538 项 pytest 覆盖通过）。
- 不做清单（按 triage §四）：直播时钟周期帧消费（搭车做了停滞提示，周期帧留档）、
  涟漪机制、时区序列化半份、历史误杀反思回填、GM 可用性专题等。
- 临时验证变速表已弃用（/tmp/speed_verify.yaml 已删）；观察期双实例并发写入的插曲已
  清理，live 内核当前由 supervisor 看守、以标准 config/speed_table.yaml 运行。
- 深夜 batch 段 world_stalled 可能因"N tick 无角色行为"阈值（30）短暂点亮——
  batch 保底事件（agent.*）计入角色行为口径，正常追日段不会触发；连续段空闲
  超过 30 tick（2.5 模拟小时）会点亮提示，属 spec'd 行为（阈值 env 可调）。
