# WorldSim · 开发任务文档 05 · Web 后端与观察端（WEB）

> v1.3 · 2026-09-24。里程碑 **M4**（00 §5：观察端 API + React admin + lite）。
> **v1.3 修订（第 3 轮终审 `review/round3-fixes.md` F3）**：① T-WEB-08 `vite.config.ts` 钉 `server.port = 5173`、T-WEB-20 `start_local.sh` 就绪检查含 5173（与 09 §8 step⑤ 同口径，R3-A P1-5 / R3-B #1）；② T-WEB-13 依赖补 02 T-LOD-04（offsite 角标数据源，R2 §A.10 已点名链）；③ §3 依赖表与 T-WEB-11 依赖行补 06 T-ART-02 脚本先行段（portraits.ts 里程碑倒挂修复，R3-B #3）；④ 下方 v1.2 修订说明裸 A 编号补 `R2 §` 前缀。
> **v1.2 修订（第 2 轮评审 `review/round2-fixes.md` §A 全局裁定 + §B-05 清单）**：① v1.1 修订说明裸 A 编号补 `R1 §A.` 前缀（R2 §A1）；② v1.1 修订说明③ `start_local.sh` 形态描述补 obs_refresh 常驻段，定稿形态 = PG + 内核 + obs_refresh 常驻 + obs-api + web dev server（R2 §A17，与 09 §8 step① 同步）；③ T-WEB-01 测试改名 `tests/observe/test_obs_views_derived.py`（R2 §A3，pytest basename 全库唯一）；④ T-WEB-09 交付物补 `web/scripts/proto_check.mjs` 并在 package.json 注册 `pnpm proto:check`（读 stdin JSON 做 zod 校验，09 §8 smoke step④ 消费，R2 §A15）；⑤ 涟漪 hop 行文去值留引用（深度上限见 01 §4.1），SQL/DDL/断言镜像从配置/yaml 读值、不写死字面量；⑥ 偏差表"待 00 §1 登记"批量收口为"已登记（本文偏差表）"（R2 §A12，00 §1 只收全局条目）。
> **v1.1 修订（第 1 轮评审 `review/round1-fixes.md` §D-05 + 涉及本文的 §A 条目）**：① T-WEB-01 收窄为 M4 增量 `obs_derived_v1.sql`，复用 M0 `obs.filter_payload()`/`obs.payload_key_whitelist`，废弃 `obs_payload_whitelist.yaml`（R1 §A.2/R1 §A.3）；② `obs.health_daily` 改为主库 `health_daily` 表上的 VIEW（权威实现 08 T-AUD-08，DDL `health_daily_v1.sql`），`obs_refresh.py` 删自算口径（R1 §A.4）；③ `start_local.sh` 唯一创建者定稿（形态=PG+内核+obs_refresh 常驻+obs-api+web dev server）、端到端检查用自有脚本不引用 smoke.sh、长挂/断网演练注明墙钟拆夜班段（R1 §A.9/R1 §A.10）；④ T-WEB-08 补 corepack 启用 pnpm 9.x（R1 §A.15）；⑤ `portraits.ts` 改 `config/agents.yaml` 构建产物、生成脚本归 06 T-ART-02、禁止手改（R1 §A.17）；⑥ grade 口径统一"最新生效 grade（event_grade_view 等价物）"；⑦ `health_thresholds.yaml` 归 01 T-CFG-07 创建、本文只消费，阈值守卫改从 yaml 动态读值（R1 §A.20 口径）；⑧ env 追加对象统一根 `.env.example`（R1 §A.13）；⑨ 脚本调用统一 `cd server && uv run python scripts/…`（R1 §A.14）；⑩ D13/D14/D15 销项，D1/D5 回登确认，新增 D16。
> 遵循 00 §4 契约红线与 §6 任务模板；事件类型/字段名/数字的唯一仲裁方 = `docs/design/06-契约登记表.md`，本文只引用、不新造。
> 关键适配前提 = 00 §1 A5：观察端 API（obs-api）M4 读**主库白名单视图**（05 §2 同一脱敏口径），M6 切副本 DSN，零代码变更。

---

> **验收命令约定**：本文验收清单中的 `uv run ...` 命令 cwd = `server/`（前缀 `cd server &&` 依 00 §1 A14 省略书写）。

## 1. 范围

- **obs-api（FastAPI + uvicorn）**：REST 全量只读接口（03 §5.1）+ WS 通道 `/ws`（03 §5.2：subscribe / resume-from-seq / 心跳 / resync）；开发期 token 鉴权与访问日志（03 §8.3/§8.1）；M4 数据源 = 主库 `obs` schema 白名单视图层（05 七表 + `event_grade_view` 的同形视图/表）。
- **web/（React 观察端，同仓双形态）**：`proto/` zod 类型与 03 §5 一一对应；zustand 五 store（world/agents/timeline/ripple/ui，03 §6.1）；admin 6 页（地图/事件流/时间轴/角色详情/涟漪/关系）+ `/health` 调试态面板；lite 3 页（`/lite/*`，叙事化文案映射层）；头像组件（portraits 接入 + 签名色 initials 兜底）；数据延迟指示条；逐句播放引擎（`at_offset_s` + 客户端倍率唯一权威）。
- **测试范围**：pytest 测 obs-api 协议/白名单/合并逻辑；Vitest 只测 store 合并与协议解析纯函数（03 §0.2 测试策略），UI 组件不做重测试。

## 2. 不在范围（defer）

| 项 | defer 到 |
|---|---|
| 完整回放引擎、`/replay` 页、`web/src/replay/` 目录、`/api/replay/*` | W5+ backlog（03 §4.3/§5.1；本期**不建 replay/ 目录**） |
| 副本库七表真实物化、同步器、obs-api 切副本 DSN、`obs_ro` 对副本库授权 | M6（SYN，docs/tasks/07） |
| nginx 公网部署、rsync 原子发布、wss 强制 | 上线部署（用户自行部署，00 §1 决策；03 §8.1/§8.2 形态预留） |
| zhparser 中文分词扩展安装 | 上线评估；开发期用设计内 pg_trgm 降级路径（03 §5.1，见 D8） |
| 种子用户招募、复看率判定执行 | 01 §10.4（本模块只交 token 表/访问日志/`/api/usage` 度量管道） |
| 用户体系、支付、订阅策略中间件 | 商业化预留（03 §8.3 末节） |
| 直播画面页 | M5（LTV，00 §1 A7） |
| Canvas/像素美术皮、精灵图 | 门禁③ 后（03 §0.2、02 §1.2） |
| 40 人全量头像资产 | M5（ART）；本期已有 6 人接入 + 其余兜底（见 D11） |
| 移动端适配 | 桌面优先（03 §2.1/§9.2），仅交付"桌面优先"提示页 |

## 3. 任务依赖表

外部前置：M0（`schema_v1.sql`/`seed_8.sql`/`obs_views_v1.sql` 已执行）；M1~M3（事件流、日快照、审计指标在库——含 08 T-AUD-08 主库 `health_daily` 表，提供 M4 联调数据）。T-WEB-08~12（前端基座）可用 fixture/mock WS 与后端并行开工（03 §8.2）。

| ID | 任务 | 依赖 | 估时 |
|---|---|---|---|
| T-WEB-01 | 主库 obs 派生视图层 | M0 + M3 数据 | 2d |
| T-WEB-02 | obs-api 骨架 + token 鉴权 + 访问日志 | T-WEB-01 | 1d |
| T-WEB-03 | REST：快照与角色接口组 | T-WEB-02 | 1d |
| T-WEB-04 | REST：`/api/snapshot?tick=` 历史合并 + diff 自检 | T-WEB-03 | 1.5d |
| T-WEB-05 | REST：事件查询 + 直方图 + 中文检索 | T-WEB-02 | 1d |
| T-WEB-06 | REST：涟漪/关系/健康接口组 | T-WEB-05、T-WEB-01、T-CFG-07（01） | 1.5d |
| T-WEB-07 | WS 通道（订阅/resume/环缓冲/心跳） | T-WEB-05 | 1.5d |
| T-WEB-08 | web/ 脚手架 + 视觉 token + 全局 shell | 无（可并行） | 1d |
| T-WEB-09 | proto/ zod 协议层 + 字段漂移 diff | T-WEB-08 | 1d |
| T-WEB-10 | zustand 五 store + 增量合并 | T-WEB-09 | 1d |
| T-WEB-11 | 逐句播放引擎 + 头像 + 延迟指示条 | T-WEB-10、T-ART-02（06，脚本先行段） | 1d |
| T-WEB-12 | 事件文案渲染器 + 事件流虚拟列表 | T-WEB-09 | 1d |
| T-WEB-13 | /map 空间地图页 | T-WEB-11/12、T-WEB-07、T-LOD-04（02，offsite 角标数据源） | 2d |
| T-WEB-14 | /timeline 事件时间轴页 | T-WEB-12、T-WEB-05 | 1.5d |
| T-WEB-15 | /agent/:id + /location/:id 页 | T-WEB-12、T-WEB-03/06 | 1.5d |
| T-WEB-16 | /ripple 涟漪追踪页 | T-WEB-12、T-WEB-06 | 2d |
| T-WEB-17 | /relations 关系演化页 | T-WEB-12、T-WEB-06 | 1.5d |
| T-WEB-18 | /health 调试态面板 | T-WEB-12、T-WEB-06 | 1d |
| T-WEB-19 | lite 观众版 3 页 | T-WEB-11/12、T-WEB-03/05/06 | 1.5d |
| T-WEB-20 | 端到端联调与 M4 出口自检 | T-WEB-01~19、M3 | 2d |

---

## 4. 任务表

### T-WEB-01 主库 obs 派生视图层（00 §1 A5 落地·M4 增量）

- 里程碑 / 依赖: M4 ；M0（schema/seed + `obs_views_v1.sql` 最小集就绪）+ M3 数据（联调用）
- 设计依据: 00 §1 A5/A12（obs 两层文件划界）、05 §2.1/§2.3（白名单与脱敏）、05 §3.1~§3.8（逐表 DDL）、06 §1.2（payload 键标注列）、04 §5.2（主库源表）
- 目标: 在 M0 obs 最小集（01 T-DB-03 `obs_views_v1.sql`：obs schema + `obs.filter_payload()` + `obs.events` + `obs.memory_projection` + `obs.payload_key_whitelist` 表）之上建 M4 增量层，使 obs schema 整体提供与 05 七表 + `event_grade_view` **逐字同名同列**的只读接口，obs-api 只经此 schema 读数
- 交付物: `server/ddl/obs_derived_v1.sql`、`server/scripts/obs_refresh.py`、`server/tests/observe/test_obs_views_derived.py`
- 实现要点:
  - **复用 M0 层，不重建**：脱敏函数唯一 = `obs.filter_payload()`，键白名单唯一 = `obs.payload_key_whitelist` 表（种子由 `gen_event_types.py` 派生，01 T-CFG-05）；本文禁止再造 `obs_sanitize_payload` / `obs_payload_whitelist.yaml` 等第二份镜像（00 §1 A12 口径）
  - 增量 DDL `obs_derived_v1.sql`：`obs.relation_change_log` VIEW（`relation.changed` 事件 `jsonb_to_recordset(payload->'changes')` 展开，列按 05 §3.3）；`obs.event_grade_view` VIEW（基线 `ui->>'grade'` ⊕ 最新 `director.grade_revise` 覆盖 = 最新生效 grade，05 §3.8 物化逻辑的视图等价）；实体表 `obs.world_state_snapshot` / `obs.relation_daily` / `obs.ripple_edge`；`obs.health_daily` = 主库 `health_daily` 表上的 VIEW（列形 05 §3.5）
  - 刷新脚本：`obs_refresh.py` 按模拟日重算三张实体表——快照从内核快照文件按 05 §3.6 字段白名单投影入库；relation_daily 递推口径 05 §3.4；ripple_edge 由 gossip `payload.cites` × `memory_projection.source_event_seq` 递归，hop 深度上限见 01 §4.1（递归 SQL 从配置/yaml 读值，不写死字面量），distortion 用 contrib `fuzzystrmatch.levenshtein` 归一化——归一化口径见 D3。**health_daily 不在自算范围**：七指标唯一权威实现 = 08 T-AUD-08 `metrics.py`，主库表 DDL = `server/ddl/health_daily_v1.sql`（不动 schema_v1.sql），obs 侧只建透传 VIEW
  - 只读账号 `obs_ro`：`GRANT USAGE ON SCHEMA obs` + `GRANT SELECT` 仅 obs schema（覆盖 M0/M4 两层全部对象；05 §5：DDL/DML 权限为零）
  - contrib 扩展 `pg_trgm`、`fuzzystrmatch` 随 DDL `CREATE EXTENSION IF NOT EXISTS`（与 M0 pgvector 同编译路径）
- 验收标准:
  1. 前置 M0 `obs_views_v1.sql` 已执行；`psql "$WSIM_PG_DSN" -f server/ddl/obs_derived_v1.sql` 执行通过且可重复执行（幂等）
  2. `uv run pytest tests/observe/test_obs_views_derived.py` 全绿，含：`test_relation_change_log_expand`（`relation.changed` 展开列形按 05 §3.3）、`test_grade_view_revise_override`（初值 B + 改判 A → 视图 grade='A' 且 events 行零 UPDATE）、`test_health_daily_view_passthrough`（`obs.health_daily` 与主库 `health_daily` 表逐行一致，非自算）、`test_obs_ro_readonly`（obs_ro 的 UPDATE/DELETE/DDL 全被拒）；`obs.events`/`obs.memory_projection` 的白名单用例归 01 T-DB-03 验收，本文不重复
  3. `cd server && uv run python scripts/obs_refresh.py --day <sim_day>` 在 M3 数据上跑通：三表行数 >0，`obs.ripple_edge.hop` ≥1 且不超过深度上限（上限值从配置/yaml 读入断言，深度上限见 01 §4.1，脚本不写死字面量）
  4. `information_schema.columns` dump 与 05 §3.1~§3.8 列清单逐字 diff 为空（脚本内断言；M0 层两视图一并覆盖）
- 实测回填: 无

### T-WEB-02 obs-api 骨架、token 鉴权与访问日志

- 里程碑 / 依赖: M4 ；T-WEB-01
- 设计依据: 03 §8.1（FastAPI 定案/访问日志）、03 §8.3（token 表 10 行容量/字段/两种携带方式）、03 §5.1（响应包与错误约定、/api/usage）、00 §3（FastAPI+uvicorn）
- 目标: obs-api 应用骨架 + 开发期 per-user token 鉴权 + 门禁④度量访问日志管道
- 交付物: `server/worldsim/observe/{__init__.py,app.py,auth.py,db.py,access_log.py,tokens_cli.py}`、`server/tests/observe/test_obs_auth.py`、根 `.env.example`（追加条目；`deploy/worldsim.env.example` 唯一归 08 T-OPS-04，本文不动）
- 实现要点:
  - 配置走 env：`WSIM_OBS_PG_DSN`（缺省回退 `WSIM_PG_DSN`，连接带 `search_path=obs`）、`WSIM_OBS_TOKEN_DB`（SQLite 路径）、`WSIM_OBS_PORT`（默认 8080，03 §8.1）——新配置项登记见 D5
  - 响应包统一 `{ok,data,meta:{watermark_tick}}`、错误 `{ok:false,error:{code,message}}`（03 §5.1）；watermark = `SELECT max(tick) FROM obs.events`（M4 语义说明见 D4）
  - token 表固定 10 行容量、字段 `token/user_label/issued_at/disabled`（03 §8.3），存 obs-api 自管 SQLite（不入仓、不进副本库）；`tokens_cli.py` 提供 issue/revoke/list，超容量拒发
  - 鉴权中间件：`?token=` 或 `Authorization: Bearer`（03 §8.3），失败 401；通过即写访问日志（token/时间/端点，不含事件内容，03 §8.1）
  - 日志按 token×自然日聚合 → `GET /api/usage?from=&to=` 返回 `[{token,day,opens,first_seen_at,last_seen_at}]`，挂管理鉴权（03 §5.1）
- 验收标准:
  1. `uv run uvicorn worldsim.observe.app:app --port 8080` 启动；无 token `curl localhost:8080/api/usage` → 401 且 `error.code` 非空
  2. 签发 token 后 `curl -H "Authorization: Bearer $T" localhost:8080/api/snapshot` → 200、`ok=true`、`meta.watermark_tick` 为整数
  3. `uv run pytest tests/observe/test_obs_auth.py` 全绿：`test_token_capacity_10`、`test_disabled_token_rejected`、`test_query_token_accepted`
  4. 同 token 同日两次请求后 SQLite 聚合行 `opens=2`、`first/last_seen_at` 正确；`/api/usage` 返回该行
- 实测回填: 无

### T-WEB-03 REST：快照与角色接口组

- 里程碑 / 依赖: M4 ；T-WEB-02
- 设计依据: 03 §5.1（/api/snapshot、/api/agents 系列 schema）、05 §6（页面→数据来源映射逐行）、05 §3.6（快照白名单/persona_display 六键边界）、05 §3.2（反思全文通道）、04 §5.2（agent id CHECK 形态）
- 目标: 世界快照与角色档案/状态/日程/反思只读接口
- 交付物: `server/worldsim/observe/rest_snapshot.py`、`rest_agents.py`、`server/tests/observe/test_obs_rest_agents.py`
- 实现要点:
  - `GET /api/snapshot`（无参=最新）：自 `obs.world_state_snapshot` 最新份组装 03 §5.1 形态（tick/sim_time/sim_day/compression_ratio/agents[]/economy/active_dialogues[]）；`compression_ratio`（`sim.compression_ratio` 键）与 `agents[].activity` 均由 05 §3.6 白名单供数、economy 按 `stocks[]` 形态透传（三处缺口第 1 轮评审已回登，D14 销项）；agents[] 仅 UI 状态子集；`active_dialogues` = 最近 1 tick 内 dialogue 事件（窗口为工程默认值，登记见 D6②；lines 整段 + `at_offset_s` 原样下发，06 §2）
  - `GET /api/agents` / `GET /api/agents/:id`（人设卡 = `persona_display` 六键：big_five/backstory/appearance/signature_quirk/contrast_public/speech_style_public；`secret`/`trigger_point` 永不出站，05 §3.6）
  - `GET /api/agents/:id/state?at=`（需求六维/情绪/挫败值/周目标/LOD）、`/schedule?day=`（快照 `routine` + 当日 actor 事件，05 §6 行）、`/reflections?limit=`（`obs.memory_projection WHERE agent_id=:id AND kind='reflection' ORDER BY sim_time DESC`，仅 `content_display`）
  - agent id 入参校验 `^A(0[1-9]|[1-3][0-9]|40)$`（04 §5.2 CHECK 同形）
- 验收标准:
  1. curl 六接口全部 200；`/api/snapshot` 的 agents 数组长度 = seed 人数；字段名与 03 §5.1 示例逐字一致
  2. `uv run pytest tests/observe/test_obs_rest_agents.py` 全绿：`test_persona_whitelist_six_keys`（响应无 secret/trigger_point）、`test_reflections_display_only`（响应无 `content` 字段）、`test_agent_id_invalid_422`
  3. `/api/agents/:id/state` 需求值与 `obs.world_state_snapshot` 直查比对一致（pytest 内勾稽）
- 实测回填: 无

### T-WEB-04 REST：`/api/snapshot?tick=` 历史合并与 diff 自检

- 里程碑 / 依赖: M4 ；T-WEB-03
- 设计依据: 03 §4.2（W3 简化回放唯一状态数据源 + 正确性自检进 CI + 能力边界 1~5）、03 §5.1、05 §6（回放行）
- 目标: "≤tick 最近快照 + 事件增量合并"服务端实现，及与下一份快照的字段级 diff 自检
- 交付物: `server/worldsim/observe/snapshot_merge.py`、`server/scripts/snapshot_diff_check.py`、`server/tests/observe/test_snapshot_merge.py`
- 实现要点:
  - 合并输入：`obs.world_state_snapshot`（≤tick 最近一份）+ 其间 `obs.events`；归约只覆盖 UI 字段（位置/情绪/需求/关系/经济，03 §4.2 边界 3），消费 `agent.move`（位置）、`state.needs_delta`（需求/情绪，`payload.changes[]`）、`relation.changed`（关系增量）等
  - 历史时刻对话显整段文本、不做连续状态机动画（03 §4.2 边界 1/2）；无 seek <500ms 指标，目标尽力 ≤2s（边界 4）
  - `snapshot_diff_check.py`：合并结果 vs 下一份 `world_state_snapshot` 字段级 diff，不一致非零退出（03 §4.2 正确性自检；兼作事件落库完整性回归）
- 验收标准:
  1. `curl "localhost:8080/api/snapshot?tick=<历史tick>"` 200；同 tick 两次响应逐字节一致
  2. `uv run pytest tests/observe/test_snapshot_merge.py` 全绿：`test_merge_equals_next_snapshot`（M3 连续 7 模拟日逐日比对）、`test_merge_deterministic`
  3. `cd server && uv run python scripts/snapshot_diff_check.py` 正常库退出码 0；测试库人为删一条 `agent.move` 后退出码非 0
  4. 历史查询抽样 20 次均值 ≤2s（输出留痕）
- 实测回填: 历史重建响应时间（均值/p95）→ 03 §9.2"历史重建"行备注 + 本文 §5

### T-WEB-05 REST：事件查询、直方图与中文检索

- 里程碑 / 依赖: M4 ；T-WEB-02
- 设计依据: 03 §5.1（/api/events 参数/cursor 分页/q 检索降级）、05 §3.8 + 06 §1.2 `director.grade_revise` 注释（grade 过滤读视图仲裁）、06 §1.1（trigger 枚举含 system）、06 §2（cursor=seq）
- 目标: 时间轴主查询接口（六维过滤 + cursor 分页 + 密度直方图）
- 交付物: `server/worldsim/observe/rest_events.py`、`server/tests/observe/test_obs_rest_events.py`
- 实现要点:
  - `GET /api/events?from=&to=&type=&actor=&location=&trigger=&grade=&q=&cursor=&limit=`：from/to 接受 tick 或 sim_time；type 逗号多值（合法值 = 06 §1.2 注册表）；trigger 六值含 `system`；actor 匹配 `actors` 数组（GIN 索引 04 §5.2）；**grade 过滤匹配最新生效 grade（JOIN `obs.event_grade_view` 等价物），不读 `events.ui` 初值**（05 §3.8/06 §1.2 注释仲裁；03 §5.1 文字已按同口径回改，D15 销项）；`q` 走 `pg_trgm`（`payload->>'text_display'` 三元组索引，03 §5.1 降级路径）
  - 分页：`cursor` = 上一页末条 **seq**（全局单调，00 §4 红线 2）；`limit` ≤500 默认 200；items[] 单条 schema 逐字按 03 §5.1（`ui` JSONB 原样；`payload.caused_by` 裸 seq 数字字符串不改写，06 §2）
  - `GET /api/events/histogram?from=&to=&bucket=sim_hour` → `[{bucket_start,count,a_count}]`，a_count 按 `event_grade_view.grade='A'` 计
- 验收标准:
  1. `uv run pytest tests/observe/test_obs_rest_events.py` 全绿：`test_cursor_pagination_no_dup_no_gap`、`test_trigger_system_accepted`、`test_type_unknown_422`、`test_grade_filter_reads_view`（grade_revise 改判后初值 B 事件被 grade=A 命中）、`test_q_trgm_hit`
  2. curl 六维过滤组合抽样各 200 且含 `meta.watermark_tick`
  3. `limit=501` → 422；缺省 limit 返回 ≤200 条
- 实测回填: 无

### T-WEB-06 REST：涟漪、关系与健康度接口组

- 里程碑 / 依赖: M4 ；T-WEB-05、T-WEB-01（obs_refresh 三表 + health_daily VIEW）、01 T-CFG-07（`health_thresholds.yaml`）
- 设计依据: 03 §3.4（五段 SQL + 今日热涟漪）、03 §3.5、03 §3.6（阈值随 API 下发、前端零硬编码）、01 §9（阈值唯一持有方）、05 §6（映射表）、03 §5.1（接口形态）
- 目标: `/api/ripple/today`、`/api/ripple/:eventId`、`/api/relations/snapshots`、`/api/relations/pair`、`/api/health` 五接口
- 交付物: `server/worldsim/observe/rest_ripple.py`、`rest_relations.py`、`rest_health.py`、`server/tests/observe/test_obs_rest_ripple.py`（`server/config/health_thresholds.yaml` 归 01 T-CFG-07 创建，本文只消费）
- 实现要点:
  - `/api/ripple/:eventId`：URL `e<seq>` 去前缀（03 §3.4）；单接口聚合五段——① `obs.memory_projection WHERE source_event_seq=:e0`；② `obs.ripple_edge WHERE root_event_seq=:e0 ORDER BY hop, sim_time`；③ distortion 只读 ripple_edge（05 §3.7）；④ `(payload->>'caused_by')::bigint IN (:e0_plus_chain)`（裸 seq 数字字符串，06 §2；命中 caused_by 部分索引 05 §3.1）；⑤ `obs.relation_change_log WHERE event_seq IN (:chain)`；响应 `{projections[],chain[],followups[],relation_changes[],stats{}}`
  - `/api/ripple/today`：当前模拟日 `event_grade_view.grade='A'` 事件按涟漪统计（投影人数×传播手数×关系边变更数）Top 5（03 §3.4）
  - `/api/relations/snapshots`：`obs.relation_daily` 稀疏编码 `[{a,b,aff,ten,label}]`；`/api/relations/pair`：`relation_change_log` 按对聚合 + `relation_daily` 补齐 + 关键事件标记（05 §6 行）
  - `/api/health`：`obs.health_daily` 七指标 + 当日未定稿部分实时小查询（05 §6 行）+ 干预率 + 成本 vs 熔断线 + 副本延迟；**阈值与阈值色随响应下发**，读 `config/health_thresholds.yaml`（文件归 01 T-CFG-07 创建，数值逐字 = 01 §9 阈值表，持有方仍是 01 §9，见 D13）
- 验收标准:
  1. `uv run pytest tests/observe/test_obs_rest_ripple.py` 全绿：`test_ripple_five_sections`（构造 gossip 链五段全非空）、`test_ripple_e_prefix_stripped`、`test_ripple_today_top5`、`test_health_thresholds_from_config`（响应阈值与 yaml 一致，改 yaml 重读生效）
  2. curl `/api/relations/snapshots` 返回稀疏边数组；`/api/relations/pair?a=A01&b=A02` 返回双向时序
  3. `/api/health` 响应含七指标值+阈值+阈值色+干预率+成本+延迟六块，前端无需任何阈值常量
- 实测回填: 无

### T-WEB-07 WS 通道：订阅、resume-from-seq、环缓冲与心跳

- 里程碑 / 依赖: M4 ；T-WEB-05
- 设计依据: 03 §5.2（全帧形态/resume/resync/心跳）、03 §6.3（state_diff 500ms 合帧、WS 吞吐预算）、05 §6（state_diff obs-api 派生不落表）、00 §4 红线 2（seq 续传）、06 §3（事件量双档口径）
- 目标: `/ws` 全协议实现
- 交付物: `server/worldsim/observe/ws.py`、`state_diff.py`、`server/tests/observe/test_obs_ws.py`
- 实现要点:
  - hello（token + 可选 `last_seq`）→ welcome（watermark_tick/server_time/session_id）；subscribe channels：events（filter types/actors/locations/grades，空=全量；grades 匹配最新生效 grade——`event_grade_view` 等价物）/world_state/health（每分钟一次）
  - 新事件感知 = 轮询主库（工程默认 500ms，对齐 03 §6.3 服务端合帧节拍，见 D6）；event 帧逐条推；`state_diff` 从事件流派生（`agent.move`→location_id、`state.needs_delta`→needs/mood），500ms 合帧
  - resume：缺口 ≤ 内存环缓冲（容量 10,000 事件，03 §5.2）直接补推接 live；超缓冲 → `{op:"resync_required"}`，客户端走 `/api/snapshot` 重建
  - 心跳：客户端 30s ping、90s 无心跳断开（03 §5.2）；重连退避逻辑在客户端（T-WEB-10），服务端 tolerate
- 验收标准:
  1. `uv run pytest tests/observe/test_obs_ws.py` 全绿：`test_resume_no_dup_no_gap`、`test_resync_required_on_large_gap`（构造 >10,000 缺口）、`test_subscribe_filter_types`、`test_state_diff_coalesce_500ms`、`test_heartbeat_timeout_disconnect`
  2. 脚本实测 hello→welcome→subscribe→event 帧序列与 03 §5.2 键名逐字一致（JSON 键 diff 为空）
  3. token 错误的 hello → 连接关闭；access_log 无事件内容
  4. 环缓冲复核：按 M2 实测事件量（06 §3 回填行）核算 10,000 覆盖的模拟日数，结论记本文 §5（不达标只登记建议，不改数）
- 实测回填: WS 推送延迟（落库→推送）抽样 → 供 T-WEB-20 合成端到端延迟

### T-WEB-08 web/ 脚手架、视觉 token 与全局 shell

- 里程碑 / 依赖: M4 ；无（可并行开工）
- 设计依据: 03 §0.2（钉版栈与目录结构）、03 §1.1/§1.2（双形态与路由表）、03 §2.1（三栏布局/导航）、02 §7.1（视觉 token 唯一定义）、02 §7.3（卡片/字号）、03 §3.6（调试态开关）、03 §9.2（移动端提示页）、03 §8.2（prebuild 强制）
- 目标: Vite+React+TS 工程基座、admin/lite 双组路由、全局三栏 shell
- 交付物: `web/`（package.json/vite.config.ts/tailwind.config.js/tsconfig.json）、`src/{main.tsx,App.tsx,router.tsx}`、`src/styles/tokens.css`、`src/components/common/AppShell.tsx`、`src/pages/*` 占位
- 实现要点:
  - 依赖钉版逐字 03 §0.2（React 18.3/TS 5.5/Vite 5/Zustand 5/Tailwind 3.4/ECharts 5.5/d3-hierarchy/@tanstack/react-virtual 3/zod/Vitest+RTL）；**不建 `src/replay/` 目录**（03 §4.3）
  - `vite.config.ts` 钉 `server.port = 5173`（admin/lite dev server 固定端口，与 09 §8 step⑤ 及 T-WEB-20 就绪检查同口径）
  - 包管理 = pnpm 9.x（00 §3 钉版）：先 `corepack enable && corepack prepare pnpm@9 --activate`，再 `pnpm install`；node ≥20（见 D10）
  - 视觉 token 全量落 CSS 变量：bg-0/bg-1/bg-2/border/text-0/text-1/accent/positive/negative/warn/neutral/director，HEX 逐字 02 §7.1（注释标持有方）；卡片 8px 圆角/12px 内边距/8px 网格/字号阶梯（02 §7.3）
  - 路由：admin 组按 03 §1.2（/map、/agent/:id、/location/:id、/timeline、/ripple[/:eventId]、/relations、/health）；lite 组挂 `/lite` 前缀（隔离决策见 D2）；/health 默认不进导航，调试态开关（快捷键 d）下出现（03 §3.6）
  - 顶导：Logo/六项/sim 时钟（Day X HH:mm，源 snapshot.sim_time）/压缩比档位/延迟指示条槽位/token 显示（03 §2.1）；左栏地点树+角色列表容器（按 LOD 分组徽标 ★/◐/○）
  - 移动端 UA → "桌面优先"提示页（03 §9.2）；`pnpm build` 前置强制 `tsc --noEmit` + `vitest run`（03 §8.2）
- 验收标准:
  1. `cd web && pnpm install && pnpm dev` 正常启动；全部路由渲染占位不报错
  2. 02 §7.1 十二个 token HEX 在 tokens.css 逐字 grep 全中
  3. `tsc --noEmit` 失败时 `pnpm build` 中止（prebuild 钩子生效）
  4. 按 d 键导航出现/隐藏"健康度"
- 实测回填: 无

### T-WEB-09 proto/ zod 协议层与字段漂移 diff

- 里程碑 / 依赖: M4 ；T-WEB-08
- 设计依据: 03 §5.1/§5.2（全部 schema）、06 §2（agent id/seq/caused_by/cites/at_offset_s 值形态）、06 §1.1（trigger/source 枚举）、03 §7.2（前端零信任）、03 §8.2（漂移 diff 常跑）
- 目标: 与 03 §5 一一对应的 zod schema 库 + REST 客户端封装
- 交付物: `web/src/proto/{event.ts,snapshot.ts,ws.ts,health.ts,ripple.ts,relations.ts,index.ts}`、`web/src/api/client.ts`、`web/src/proto/fixtures/*.json`、`web/src/proto/*.test.ts`、`web/scripts/proto_check.mjs`（`package.json` scripts 注册 `pnpm proto:check`）
- 实现要点:
  - 事件 schema：seq 整数主键/tick/sim_time ISO/type 枚举 = 06 §1.2 全集/source 四形态/trigger 六值含 system/arc_id 可空/ui JSONB/payload JSONB；agent id `^A(0[1-9]|[1-3][0-9]|40)$`；`caused_by`/`cites[]`/`for_event_ref`/`target_seq` = 裸 seq 数字字符串（`^\d+$`，06 §2）；`lines[].at_offset_s` = float ≥0
  - 前端零信任：未知键剥除 + 命中 `text_raw`/未知键时告警计数（03 §7.2）；proto 层只定义 `*_display` 字段
  - fixtures：03 §5.1/§5.2 的 JSON 示例原样抽取（注释标节号）；漂移 diff 测试 = fixture 过 zod + 键集合互差为空
  - `proto_check.mjs`：从 stdin 读 JSON、用 proto/ zod schema 校验（按 `meta.kind`/端点类型选 schema，未知键剥除并告警计数同前端零信任口径），校验失败非零退出；`package.json` scripts 注册 `pnpm proto:check`，09 §8 smoke step④ = curl 出响应管道给它消费（08 T-OPS-05 对应步骤引用）
  - REST client：统一解包 `{ok,data,meta}`，错误抛 `ApiError{code,message}`；token 自 URL `?token=` 注入
- 验收标准:
  1. `pnpm vitest run src/proto` 全绿：fixture 解析、`test_trigger_includes_system`、`test_agent_id_regex`（A01/A40 过，ag07/A41 拒）、`test_caused_by_bare_seq_string`（'1089' 过、'e1089' 拒）、`test_text_raw_stripped_with_warning`
  2. 03 §5 全文出现的帧/schema 在 proto/ 均有对应导出（勾稽 checklist 留测试注释）
- 实测回填: 无

### T-WEB-10 zustand 五 store 与事件增量合并

- 里程碑 / 依赖: M4 ；T-WEB-09
- 设计依据: 03 §6.1（store 职责）、03 §6.2（合并规则 1~4）、03 §6.3（环形缓冲 3,000/LRU 20）、03 §5.2（resume/resync/退避）、03 §3.2（倍率档位）、00 §4 红线 2
- 目标: worldStore/agentsStore/timelineStore/rippleStore/uiStore + WS 管理器直写 store
- 交付物: `web/src/stores/{worldStore,agentsStore,timelineStore,rippleStore,uiStore}.ts`、`web/src/ws/manager.ts`、`web/src/stores/merge.ts`、`web/src/stores/*.test.ts`
- 实现要点:
  - timelineStore：环形缓冲容量 3,000（03 §6.1）；事件按 seq 升序插入、重复 seq 去重（append-only 幂等，03 §6.2.1）；live/历史模式与过滤条件态
  - worldStore：snapshot 整体替换 + `state_diff` 按 tick apply，`diff.tick <= 当前 tick` 丢弃（03 §6.2.2）；快照重建时令 timelineStore 只保留 ≥快照 tick 事件（03 §6.2.3）
  - agentsStore 详情缓存 LRU 20 人（03 §6.1）；rippleStore 一次只持一个源事件；uiStore 持路由上下文/选中/跟拍/调试态/播放倍率
  - ws/manager.ts：原生 WebSocket 自封装（03 §0.2，~150 行）；断线重连指数退避 1s→2s→…→30s 封顶、±20% 抖动（03 §5.2）；重连 hello 带 last_seq；收 `resync_required` → 丢内存态走 `/api/snapshot` 重建；React 外直写 store
- 验收标准:
  1. `pnpm vitest run src/stores` 全绿：`test_out_of_order_insert_by_seq`、`test_dup_seq_dedup`、`test_state_diff_stale_tick_dropped`、`test_snapshot_rebuild_truncates_timeline`、`test_ring_buffer_evicts_oldest`（3001 入 → 3000 容量）、`test_agents_lru_20`、`test_backoff_schedule`（1,2,4…≤30 且抖动 ±20% 内）
  2. 测试主体 = merge/协议解析纯函数（03 §0.2 测试策略）
  3. 缓冲复核：按 M2 实测事件量核算 3,000 条覆盖时长，结论记本文 §5（不达标只登记建议）
- 实测回填: 环形缓冲覆盖时长复核结论 → 本文 §5

### T-WEB-11 逐句播放引擎、头像组件与数据延迟指示条

- 里程碑 / 依赖: M4 ；T-WEB-10、06 T-ART-02 生成脚本先行段（`web/src/lib/portraits.ts` 由其派生，M4 前必须交付）
- 设计依据: 06 §2（at_offset_s 唯一时间表/客户端倍率唯一节拍权威）、03 §3.1（2~4s/句、进度点、>2× 跳动画）、03 §6.2.4（播放队列与事件队列分离）、02 §4.3（签名色池）、03 §0.1/§9.2（延迟语义与 >60s 黄 >10min 红）
- 目标: 三个跨页共用 common 件
- 交付物: `web/src/lib/playbackEngine.ts`、`web/src/components/common/{Avatar.tsx,LatencyBar.tsx}`、`web/src/lib/portraits.ts`（**构建产物**：由 `config/agents.yaml` 生成，生成脚本归 06 T-ART-02，禁止手改）、`web/src/lib/playbackEngine.test.ts`
- 实现要点:
  - playbackEngine：对话事件只入队"剧本"（`lines[].at_offset_s`），逐句实际间隔 = 相邻 at_offset_s 差值 ÷ uiStore 当前倍率（**客户端倍率是唯一节拍权威**，06 §2）；暂停/倒带不影响数据完整性；播放中进度点 `●●○○○`；倍率 >2× 整段直显（03 §3.1">2× 跳动画"档在逐句播放语境的适配，登记见 D16）
  - Avatar：有 portraits 资产（`ui/assets/portraits/<slug>.png`，映射表 `portraits.ts` 为 agents.yaml 构建产物、当前 6 人）用 img；无资产兜底 = 签名色圆底 + 姓名首字（签名色唯一权威 = `config/agents.yaml`，池口径 02 §4.3，形态参照 ui/web-lite `.av`）；签名色缺失按 id 稳定散列取池内一色
  - LatencyBar：`watermark_tick − 最新事件 tick` 换算滞后时长（03 §0.1）；>60s 黄（warn）/ >10min 红（negative）（03 §9.2）；M4 恒近零属预期（D4）
- 验收标准:
  1. `pnpm vitest run src/lib/playbackEngine.test.ts` 全绿：`test_interval_divided_by_rate`（差值 3s、2× → 1.5s）、`test_pause_resume_keeps_cursor`、`test_rate_above_2x_shows_full`、`test_offsets_never_mutated`
  2. 40 人列表渲染：6 人出 img、34 人出签名色 initials（截图 + `test_fallback_initials_stable_color`）
  3. LatencyBar 单测：注入 61s → warn 类名；601s → negative 类名
- 实测回填: 无

### T-WEB-12 事件文案渲染器与事件流虚拟列表

- 里程碑 / 依赖: M4 ；T-WEB-09
- 设计依据: 03 §5.3（映射表全表 + 缺省规则 + 股价色例外 + L3 无位）、06 §1.2（类型名仲裁）、06 §1.3（amount_cents 口径）、02 §7.3（trigger 色条）、03 §6.3（行高 44px/±50 窗口）、03 §1.1（反查原则）
- 目标: type→图标/气泡/文案的唯一渲染函数 + 可复用事件流组件
- 交付物: `web/src/lib/eventText.ts`、`web/src/components/common/EventStream.tsx`、`web/src/lib/eventText.test.ts`
- 实现要点:
  - eventText.ts 逐行实现 03 §5.3 表（含系统事件灰条行）；`text_display` 优先、缺省模板拼；金额展示由 `amount_cents`（分，正入负出，06 §1.3）换算
  - 缺省规则：06 已注册且带 `text_display` 的未列 type → 通用社交行（卡片横幅 + 域图标）；无 `text_display` 未知 type → 灰条原始 JSON + 前端错误日志（03 §5.3 N-P1-8）
  - 股价组件红涨绿跌 + 图例（03 §5.3 N-P1-13 例外区）；`director.intervene` 紫色描边（director token，02 §7.1）；`e<seq>` 仅渲染层拼前缀，值永远是 seq（06 §2）
  - 事件条目左 4px 色条标识 trigger（autonomous=text-1/world=accent/director=director，02 §7.3）
  - EventStream：@tanstack/react-virtual，行高 44px，窗口 ±50 条（03 §6.3）；点击跳 `/timeline?focus=e<seq>`（03 §1.1）
- 验收标准:
  1. `pnpm vitest run src/lib/eventText.test.ts` 全绿：06 §1.2 全类型遍历有渲染路径无 throw、`test_unknown_type_gray_bar_logged`、`test_stock_red_up_green_down`、`test_amount_cents_display`
  2. 显式类型清单与 03 §5.3 表行数一致（测试内断言）
  3. EventStream 灌 5,000 条 fixture：DOM 行数 ≤ 窗口上限（RTL 渲染计数断言）
- 实测回填: 无

### T-WEB-13 /map 空间地图页

- 里程碑 / 依赖: M4 ；T-WEB-11、T-WEB-12、T-WEB-07、02 T-LOD-04（offsite 角标数据源，R2 §A.10 链）
- 设计依据: 03 §3.1（布局 JSON/校外区块/缩放/跟拍/移动动画/调试叠加）、03 §2.2(a)、02 §7.2（z-order）、03 §4.2（scrub 历史重建）、03 §9.1（/map 与校外验收行）、01 §1.6（校外时段口径）
- 目标: SVG 地图四 site tab + pawn/名牌/气泡 + 跟拍 + 底部 scrub bar
- 交付物: `web/src/pages/MapPage.tsx`、`web/src/components/map/{SiteSvg.tsx,Pawn.tsx,BubbleLayer.tsx,LocationTree.tsx,ScrubBar.tsx}`、`web/public/map_layout.json`
- 实现要点:
  - map_layout.json 按 03 §3.1 结构（apt 6 层×4 房+公共区/corp zones/ext/offsite aggregate）；逻辑坐标系（×4 换算为门禁③预留，本期纯 SVG）
  - z-order 六层逐字 02 §7.2；缩放 0.5×~4× 滚轮/拖拽/双击归位；名牌气泡反缩放恒定字号（03 §3.1）
  - pawn = Avatar + 名牌（姓名+LOD 徽标）+ 情绪符号；气泡三态（💬双人连线/💭灰虚线调试态全文/📢公告波及）；逐句播放接 playbackEngine
  - offsite 聚合区块：人数角标随 `agent.move` 增减、点击出驻留 NPC 列表可跳 `/agent/:id`、区块内无气泡与移动动画（03 §3.1 N-P1-9）
  - 跟拍：rAF 插值跟随、跨 site 自动切 tab、右栏锁定摘要卡；`agent.move` pawn 300ms 缓动、跨 site 淡入淡出、倍率 >2× 跳变
  - 调试叠加层（d 键）：房间在场人数/location_id/事件 id tooltip（服务"不在两地"审计目检）
  - 底部 scrub bar：拖到历史 tick → `/api/snapshot?tick=` 静态重建；拖回或"跳至 live"恢复 WS；历史时刻对话显整段（03 §4.2 边界 2）
  - 右栏：选中角色摘要卡（跳 /agent/:id）或本地点/全局 EventStream；顶部"今日热涟漪"卡片条（组件来自 T-WEB-16）
- 验收标准:
  1. 40 pawn 位置与 `/api/snapshot` 响应一致（数据勾稽 + 截图）
  2. 校外 tab：工作日 18:30~次日 8:00（01 §1.6）offsite 角标人数 = 快照中 offsite 节点 NPC 数（SQL 直查比对）
  3. 缩放/平移/跟拍/自动切 tab 手测通过；40 pawn+气泡同屏 ≥55fps（Chrome 性能面板，03 §9.2，实测进 §5）
  4. scrub 历史重建与当时事件流抽样一致（03 §9.1 简化回放行）
- 实测回填: 地图帧率实测 → 本文 §5 与 03 §9.2 行

### T-WEB-14 /timeline 事件时间轴页

- 里程碑 / 依赖: M4 ；T-WEB-12、T-WEB-05
- 设计依据: 03 §3.2（倒带/变速/六维过滤/直方图/虚拟列表）、03 §2.2(c)、06 §1.2（grade_revise 改判标记）、03 §1.1（focus 反查）、06 §1.1（trigger 含 system）
- 目标: 事件维度补看页（倒带/seek + 全文过滤）
- 交付物: `web/src/pages/TimelinePage.tsx`、`web/src/components/timeline/{FilterBar.tsx,DensityHistogram.tsx,TimelineList.tsx}`
- 实现要点:
  - 六维过滤（type/actor/location/trigger 多选 + 关键词 + A 级开关）全部下推 REST 参数，前端不另滤（03 §3.2）
  - scrub bar 以模拟时间为轴；拖离右端 live→历史（顶部"跳至 live"），拖回恢复订阅；速度档 0.5/1/2/4/8× 入 uiStore，与内核压缩比分别显示（03 §3.2）
  - 密度直方图：`/api/events/histogram`，A 级红柱叠加，点击定位时段
  - 行：时间戳/图标/文案（eventText）/地点/trigger 徽标；`director.grade_revise` 行渲染改判标记（`target_seq` 渲染 e<seq> 链接，06 §1.2）；详情抽屉含 trigger 与 `payload.caused_by` 链
  - `/timeline?focus=e<seq>` 入口解析定位（03 §1.1）
- 验收标准:
  1. 六维过滤逐维 + 组合发出正确 REST 参数（网络面板核对）且结果与 SQL 直查一致（抽样 3 组）
  2. 倒带→历史→跳至 live 全程事件不重不漏（手测 + T-WEB-10 合并测试）
  3. vitest：`test_focus_param_parsing`（e1102→1102）、`test_grade_revise_badge_render`
  4. 单次 500 条渲染滚动无掉帧（03 §9.2 行）
- 实测回填: 无

### T-WEB-15 /agent/:id 角色详情页与 /location/:id 地点详情页

- 里程碑 / 依赖: M4 ；T-WEB-12、T-WEB-03、T-WEB-06
- 设计依据: 03 §3.3（七区块 + ⏱ 调试钩子）、03 §2.2(b)、05 §6（数据源逐行）、05 §3.2（反思全文通道）、03 §1.2（/location 行与上下文 query）
- 目标: 角色七区块详情与地点事件流页（原型缺页，见 D7）
- 交付物: `web/src/pages/{AgentPage.tsx,LocationPage.tsx}`、`web/src/components/agent/{PersonaCard.tsx,NeedsRadar.tsx,ScheduleList.tsx,ReflectionList.tsx,RelationList.tsx,DebugSourcePopover.tsx}`
- 实现要点:
  - 七区块逐字 03 §3.3：人设卡（persona_display 六键）/LOD 徽标+升降格历史（`/api/events?actor=&type=agent.promoted,agent.demoted`）/需求六维雷达+24 模拟小时趋势线/今日日程（已执行✓/爽约✗）/最新反思 3 条（`/api/agents/:id/reflections`，仅展示通道）/关系列表按 \|affinity\| 排序（边色=张力，跳 `/relations?pair=A,B`）/周目标+受阻计数+挫败进度条
  - ⏱ 钩子：需求/关系值旁弹层列最近 10 次变更来源事件（`state.needs_delta`/`relation.changed` 的 `payload.changes[].cause`，05 §6 行），e<seq> 可点击跳 timeline
  - /location/:id：EventStream（`/api/events?location=`）+ 在场角色；跨页上下文 query 保留（03 §1.2 末节）
- 验收标准:
  1. 七区块齐全，抽样 5 个数值均可 ⏱ 反查来源事件 seq（03 §9.1 角色行）
  2. 反思区 = SQL 直查 `obs.memory_projection WHERE kind='reflection'` 结果勾稽一致
  3. /location/:id 事件流与 `?location=` 查询一致；地点树选中联动（03 §9.1 地点行）
  4. vitest：`test_needs_radar_six_dims`、`test_debug_popover_seq_links`
- 实测回填: 无

### T-WEB-16 /ripple 涟漪追踪页

- 里程碑 / 依赖: M4 ；T-WEB-12、T-WEB-06
- 设计依据: 03 §3.4（五段数据/渲染结构/今日热涟漪/导出选题卡）、02 §7.4（涟漪视觉语言）、05 §3.7（distortion 持有方）、02 §7.6（A 级脉冲加速）
- 目标: 源事件扩散 DAG 可视化 + 无参落地页推荐位
- 交付物: `web/src/pages/RipplePage.tsx`、`web/src/components/ripple/{RippleDag.tsx,SourceEventCard.tsx,ProjectionList.tsx,FollowupChain.tsx,RelationChangeMini.tsx,TodayRipples.tsx}`、`web/src/lib/rippleLayout.ts`
- 实现要点:
  - 布局：D3-hierarchy 算布局 + SVG 自绘（03 §0.2）；传播代数=列、时间从左向右分层，禁纯力导向（02 §7.4 布局行）
  - 视觉逐字 02 §7.4：源事件 24px accent 实心圆脉冲 1.5s（A 级 0.8s，02 §7.6）；投影节点 12px 签名色 70% 透明；边 = 有向箭头，第 1 手实线、第 2 手起虚线透明度 100%−20%×n；失真标签挂边中点 11px；失真三档 ≤15% neutral / 15~40% warn / >40% negative（档值持有方 05 §3.7，引用）；后续事件菱形节点 bg-2+text-0 描边，跳 `/timeline?focus=`
  - 五段全渲染：投影列表（当事/目击标注）/传播链（点节点展开"听到的版本"）/失真条/后续事件链/关系边变化迷你卡；统计条"覆盖 x/40 人 · n 手 · 最大失真 · m 条后续"
  - `/ripple` 无参落地页 = TodayRipples（Top 5）+ 源事件检索框；"导出选题卡"生成纯文本摘要（03 §3.4）
- 验收标准:
  1. 03 §9.1 涟漪行：任选 gossip 起源事件，五问（投影几人/几手到谁/每手失真/后续事件/关系边）页面可答，M3 真实案例截图留痕
  2. vitest：`test_distortion_three_bands`（0.14→neutral/0.2→warn/0.41→negative）、`test_hop_opacity_decay`、`test_export_card_text`
  3. 今日热涟漪 Top5 点击直达 `/ripple/:eventId`（03 §9.1）
- 实测回填: 无

### T-WEB-17 /relations 关系结构演化页

- 里程碑 / 依赖: M4 ；T-WEB-12、T-WEB-06
- 设计依据: 03 §3.5（force/timeline/双曲线/周变化率小卡）、02 §7.5（演化动画规范）、03 §2.2(e)
- 目标: ECharts force+timeline 全网演化 + 任意两人双曲线
- 交付物: `web/src/pages/RelationsPage.tsx`、`web/src/components/relations/{ForceGraph.tsx,PairCurves.tsx,WeekChangeCard.tsx}`
- 实现要点:
  - ECharts `series.type:'graph'` force 布局 40 节点；边宽 ∝ \|affinity\|（1~5px 线性）、边色 affinity>0 positive/<0 negative、tension>50 叠加 warn 1px 内芯（02 §7.5）；钉住布局——播放期间节点锁死只变边
  - `timeline` 组件按模拟日步进（`/api/relations/snapshots`）；边增删/粗细 300ms 缓动；新边 0 宽生长 500ms、断边闪烁 2 次消失（02 §7.5）
  - 双人对比：`/api/relations/pair` 双向 affinity/tension 双 y 轴折线 + 交互事件标记点（点击跳 `/timeline?focus=`）；`?pair=A,B` 入口（03 §3.3）
  - 周变化率小卡嵌入本页（与 /health 同源，阈值 API 下发）
- 验收标准:
  1. 播放 7 模拟日动画边变化可见且节点不漂移（截图对比）
  2. 任选两人出双曲线，标记点点击跳 timeline 且 focus 正确（03 §9.1 关系行）
  3. vitest：`test_edge_width_mapping`、`test_pair_query_parsing`
- 实测回填: 无

### T-WEB-18 /health 叙事健康度调试态面板

- 里程碑 / 依赖: M4 ；T-WEB-12、T-WEB-06
- 设计依据: 03 §3.6（七卡 + 建议动作 + 运行指标条 + 调试态归属）、01 §9（阈值唯一持有方）、05 §3.5（health_daily 列）、02 §7.1（语义色）
- 目标: 五指标 + 两项结构指标卡 + 运行指标条，阈值全由 API 下发
- 交付物: `web/src/pages/HealthPage.tsx`、`web/src/components/health/{MetricCard.tsx,RuntimeBar.tsx}`
- 实现要点:
  - 七卡：A 级事件间隔/类型熵/出场基尼/3-gram 重复度/关系周变化率 + 高张力边占比/活跃冲突边密度（01 §9 v1.1 结构两指标）；每卡趋势 sparkline + 超阈值变色 + 建议动作文案（03 §3.6 表逐行）
  - **前端零阈值硬编码**：值/阈值/阈值色全取 `/api/health`；色映射 green→positive/yellow→warn/red→negative（02 §7.1）
  - 运行指标条：干预率（红线值见源方案 §4.8，06 §3 登记）、¥/模拟日 vs 熔断线（倍数见源方案 §5.2，判级器归 03 T-LLM-08）、副本延迟
  - 仅调试态可达（导航不出现，d 键开关，03 §3.6）
- 验收标准:
  1. 阈值守卫：vitest 从 `server/config/health_thresholds.yaml`（01 T-CFG-07）动态读全部阈值数值生成 grep 模式，断言 `web/src` 零命中（守卫自身不写死任何阈值字面量）
  2. 改 `health_thresholds.yaml` 后卡片变色随之变化（`test_threshold_color_from_api` + 手测）
  3. 七卡 + 运行指标条渲染齐全；非调试态访问 /health 不进导航（03 §3.6）
- 实测回填: 无

### T-WEB-19 lite 观众版 3 页

- 里程碑 / 依赖: M4 ；T-WEB-11、T-WEB-12、T-WEB-03/05/06
- 设计依据: 03 §1.1（双形态：呈现层减法 + 叙事化文案映射层、不产生新数据需求、门禁④只发 lite）、01 §10.4、视觉/文案参照 `ui/web-lite/` 三页原型
- 目标: `/lite/home`（地图+今日看点）、`/lite/agent/:id`（角色故事页）、`/lite/story`（传播链故事页）
- 交付物: `web/src/lite/{LiteHomePage.tsx,LiteAgentPage.tsx,LiteStoryPage.tsx,narrativeMap.ts}`
- 实现要点:
  - 数据复用同一 obs-api（03 §1.1 不产生新数据需求）；视觉 token 同源 02 §7.1；布局与文案沿用 `ui/web-lite/` 原型风格（叙事卡片、人话标注）
  - narrativeMap.ts = 叙事化文案映射层：事件类型 → 人话模板；**不出现事件类型名/trigger/tick/LOD/seq/压缩比等工程词汇**；数值降级为语言（需求六维 → 心情语句，03 §1.1 示例口径）
  - 无调试态组件、无健康度、无原始 payload 查看；头像复用 Avatar；token 访问同一中间件（03 §8.3）
- 验收标准:
  1. 三页与 ui/web-lite 原型结构并排截图对照一致
  2. 叙事层输出 grep 断言：`tick|trigger|LOD|seq|压缩比|grade|autonomous` 零命中（lint 脚本进 vitest）
  3. 手测链路：/lite/home 点人 → /lite/agent/:id → /lite/story 走通
- 实测回填: 无

### T-WEB-20 端到端联调与 M4 出口自检

- 里程碑 / 依赖: M4 ；T-WEB-01~19 全部、M3（连续 7 模拟日数据）
- 设计依据: 03 §9.1/§9.2（验收 checklist 与性能表）、00 §5（M4 出口：端到端延迟 p95 ≤5s）、03 §8.2（两项 diff 常跑）、03 §7.3（未过审占位）、00 §2（start_local.sh）
- 目标: 全链路本机起栈、断网演练、性能实测、双通道抓包验收
- 交付物: `deploy/start_local.sh`、`server/scripts/e2e_latency_probe.py`、`server/tests/observe/test_e2e.py`
- 实现要点:
  - **本任务 = `deploy/start_local.sh` 唯一创建者**（00 §1/§2 已登记；M6 由 08 T-OPS-05 只增补 ingest/worker 起停段）：一条命令起全栈 = PG + 内核 + obs_refresh 常驻 + obs-api + web dev server；**就绪检查含 5173**（web dev server 端口，与 09 §8 step⑤ 同口径）
  - 端到端检查用本任务自有脚本（`e2e_latency_probe.py` + `test_e2e.py`），不引用 smoke.sh（唯一创建者 = 08 T-OPS-05，M6）
  - 断线演练：停 obs-api 5 分钟恢复 → 按 last_seq resume 不重不漏（seq 连续性校验）；停 2 小时恢复 → resync 全量重建成功（03 §9.1 末行）
  - 端到端延迟：probe 记录事件落库 wall_time → WS 收帧时差，跑 1 模拟日取 p95 ≤5s（00 §5 M4 出口）
  - 双通道：WS/REST 全帧抓包 grep 无 `text_raw`/embedding/prompt；构造未过审事件 → 前端渲染 `▮内容审核中▮` 占位且事件可见（03 §7.3）
  - 性能自查：首屏 <2s（Lighthouse 4G）、峰值 2 事件/s 8h 内存增长 <50MB、历史重建 ≤2s（03 §9.2 表）
  - **墙钟标注**：断线演练（5min+2h）与 8h 内存长跑为长墙钟项，拆独立夜班段执行，不占本任务 2d 日间估时；M4 出口判定以日间项为准，夜班段结果回填本文 §5 与 03 §9.2
  - 两项 diff 常跑：proto 漂移（T-WEB-09）+ snapshot 合并（T-WEB-04）挂 prebuild/pytest 常跑（CI 形态见 D9）
- 验收标准:
  1. `bash deploy/start_local.sh` 起全栈，浏览器 /map 实时可见事件流
  2. 5min/2h 断网演练两项通过，seq 连续性输出留痕
  3. 端到端 p95 ≤5s；03 §9.2 性能表逐项实测填写（未达项进 §5，不跳过）
  4. 抓包 grep `text_raw` 命中 0；占位渲染截图留痕
- 实测回填: 端到端延迟 p95、首屏、帧率、8h 内存、历史重建耗时 → 03 §9.2 表备注 + 00 §5 M4 出口记录

---

## 5. 风险与实测回填清单

| 回填项 | 产生任务 | 登记位置 | 口径 |
|---|---|---|---|
| 环缓冲覆盖复核（服务端 10,000 / timelineStore 3,000） | T-WEB-07/10 | 本文本节结论 + 06 §3 回填行引用 | 03 §5.2/§6.3"实测回填后复核"，不改数只登记建议 |
| 历史快照重建响应时间 | T-WEB-04 | 03 §9.2 行备注 | 目标 ≤2s（03 §4.2 边界 4） |
| 端到端延迟 p95 / 首屏 / 帧率 / 8h 内存 | T-WEB-20/13 | 03 §9.2 表备注 + 00 §5 M4 出口记录 | p95 ≤5s 为 M4 出口线 |

风险（按优先级）：

1. **快照合并 diff 失败** = 事件落库完整性问题，阻塞 M4 出口（03 §4.2 自检定位即回归测试）；优先级最高，T-WEB-04 不绿不进页面联调。
2. **obs 视图/重算性能**：`relation_daily`/`ripple_edge` 全量重算在 40 人数据量耗时未知；M4（8 人）无压力，`obs_refresh.py` 计时留痕，M6 由 SYN 流式 worker 替换（05 §4.2）。
3. **pg_trgm 中文召回粗**（03 §5.1 已自认降级）：时间轴关键词体验打折，上线前评估 zhparser（D8）。
4. **8h 内存长跑**依赖数据灌注速度：可用内核 `WSIM_REPLAY_MODE` 加速灌事件后再测。
5. **门禁④度量**只到管道为止：`/api/usage` 数据无种子用户时为空转，招募与判定执行属 01 §10.4。

## 6. 适配与偏差

> 00 §1 已登记的全局适配（A1~A8）不重复。以下为本模块新增；模块级工程默认的登记处即本文偏差表（第 2 轮评审 A12 口径：00 §1 只收全局条目），状态标"已登记（本文偏差表）"。

- **D1（已回登，00 §1 A12 / §2 布局）**：obs 白名单层落两个 DDL 文件——`server/ddl/obs_views_v1.sql`（M0 最小集，归 01 T-DB-03：obs schema + `obs.filter_payload()` + `obs.events` + `obs.memory_projection` 视图 + `obs.payload_key_whitelist` 表）与 `server/ddl/obs_derived_v1.sql`（M4 增量，归 T-WEB-01：`relation_change_log` / `event_grade_view` / 三实体表 + `health_daily` VIEW）；原 `server/config/obs_payload_whitelist.yaml` 废弃（键白名单种子由 `gen_event_types.py` 派生，禁止第二份手维护镜像）。视图/表名与列名逐字对齐 05 §3.1~§3.8，保证 M6 切副本 DSN 零代码变更。
- **D2（已登记（本文偏差表））**：lite 路由挂 `/lite` 前缀（/lite/home、/lite/agent/:id、/lite/story）——03 §1.1 的 lite 路由 `/home` `/agent/:id` 与 admin 路由冲突，设计未定同仓隔离方案。
- **D3（已登记（本文偏差表））**：ripple distortion 归一化分母取 `max(len)`（PG contrib `fuzzystrmatch.levenshtein`）——05 §3.7 只写"normalized_levenshtein 0~1"未钉归一化口径；需回登 05 §3.7 并与 SYN M6 派生 worker 实现对齐。
- **D4**：M4 `watermark_tick` = 主库 `max(tick)`（无副本），延迟指示条恒近零、仅演练 UI 变色逻辑；M6 切副本后获得 03 §0.1 真实语义（00 §1 A5 推论，无需登记）。
- **D5（已入 00 §2 env 总表，第 1 轮评审确认）**：新增配置项 `WSIM_OBS_PG_DSN` / `WSIM_OBS_TOKEN_DB` / `WSIM_OBS_PORT`——04 §1.6 环境配置表未列 obs-api 条目；M4 段追加对象 = 根 `.env.example`（01 T-ENV-05 创建），`deploy/worldsim.env.example` 唯一归 08 T-OPS-04。
- **D6（已登记（本文偏差表））**：工程默认值两处——① obs-api 新事件感知 = 轮询主库 500ms（对齐 03 §6.3 服务端合帧节拍；LISTEN/NOTIFY 需改 M0 append-only 触发器，不采用）；② `/api/snapshot` 的 `active_dialogues` 时间窗 = 最近 1 tick 内 dialogue 事件（03 §5.1 未定义窗口；tick = 模拟 5 分钟，00 §4 红线 10）。
- **D7**：admin 原型缺 2 页——`ui/web/` 仅 5 页原型（map/timeline/ripple/relations/health），`/agent/:id` 与 `/location/:id` 按 03 §2.2(b) wireframe + 02 §7 token 自绘，视觉沿用既有 5 页。
- **D8**：开发期不装 zhparser（SCWS 源码编译成本高），直接启用 03 §5.1 明示的 pg_trgm 降级路径；上线部署再评估（记录项，无需登记）。
- **D9（已登记（本文偏差表））**：CI 形态适配——03 §8.2"git push 单 job"在纯本机开发（无远端 CI 平台）下落为 prebuild/pretest 本地强制钩子；接远端平台后补 job 定义。
- **D10（已登记（本文偏差表））**：Node 版本设计未钉（03 §0.2 只钉库版本），`package.json engines` 写 `node>=20`（LTS 工程默认）。
- **D11**：头像资产仅 6/40 人（`ui/assets/portraits/` 现有 chenyu/hanche/linwan/suman/yezhen/zhouxu）；其余 34 人走签名色 initials 兜底，全量资产属 M5/ART。
- **D12**：`/api/health` 当日未定稿部分在 M4 主库侧现算（05 §6 行"events 实时小查询"同口径），8 人数据量无性能预算问题。
- **D13（已销项）**：`server/config/health_thresholds.yaml` 为 01 §9 阈值的服务端载体（数值逐字抄 01 §9，持有方仍是 01 §9）——03 §3.6"阈值随 API 下发、前端零硬编码"与 00 §7 DoD 4"代码零硬编码阈值"的共同落点；第 1 轮评审裁定文件创建归 01 T-CFG-07、00 §2 config 清单已补登，本文（T-WEB-06/18）只消费、不新建。
- **D14（已销项，第 1 轮评审 B 表）**：快照三处形态缺口已全部回登——① `compression_ratio` 与 ② `agents[].activity`：05 §3.6 快照白名单已增 `sim.compression_ratio` 与 `agents[].activity` 键，M6 断供消解，M4 亦不再走主库 `world_state` KV 兜底；③ economy 形态：03 §5.1 示例已按 05 `stocks[]` 数组回改。本期响应按 05 §3.6 白名单形态透传（T-WEB-03）。
- **D15（已销项，第 1 轮评审 B 表）**：03 §5.1 `/api/events` 的 `grade` 参数原描述"匹配 `ui->>'grade'`"与 05 §3.8/§6 及 06 §1.2 `director.grade_revise` 注释冲突——03 侧三处（§5.1 过滤参数 / §5.2 subscribe grades / §3.4 今日热涟漪）已统一回改为"最新生效 grade（event_grade_view 等价物）"，本文 T-WEB-05/07 按同口径实现。
- **D16（已登记（本文偏差表））**：逐句播放"倍率 >2× 整段直显"——03 §3.1 的 >2× 档原文语境为地图 pawn"跳动画"与"2~4s/句"默认语速，对话逐句播放在 >2× 下整段直显是播放侧工程适配（T-WEB-11）；间隔公式不变 = 相邻 `at_offset_s` 差值 ÷ 当前倍率（06 §2 客户端倍率唯一节拍权威）。
- **D17（已登记（本文偏差表））**：gossip 传播深度上限的配置镜像键 = `config/relations.yaml` `gossip.hop_max`（唯一持有方仍是 01 §4.1，T-WEB-01 递归断言/脚本从本键读值不写死字面量）。落点理由：world.yaml 顶层键集被 04 侧冻结测试钉死（`test_frozen_four_sections`），relations.yaml 已有 01 §4.1 镜像先例（refuse_streak）。
- **D18（已登记（本文偏差表））**：`obs.health_daily` 透传 VIEW 列集 = 主库 `health_daily` 表全列（含 01 §9 v1.1 增列 `stars_without_conflict`，超出 05 §3.5 列集）——主库表为 08 T-AUD-08 权威 DDL，obs 层只做透传不自算；T-WEB-01 验收 4 的列形 diff 对 health_daily 以主库表列集为准。
- **D19（已登记（本文偏差表））**：obs 授权两处补丁——① obs_ro 补 `SELECT ON obs.payload_key_whitelist`（`obs.filter_payload()` 为 SECURITY INVOKER，obs_ro 实际取 `payload` 列时实测必需；M0 测试只 `count(*)` 未触发函数求值故未暴露）；② `obs_refresh.py` 写入账号 = 内核应用角色 worldsim（USAGE + 三实体表 INSERT/UPDATE/DELETE），obs-api 仍只走 obs_ro 只读（红线 12 不变）。
- **D20（已登记（本文偏差表））**：contrib `pg_trgm`/`fuzzystrmatch` 在 M0 装机时未编译（pg_build.sh 只编 pgvector/pg_partman），T-WEB-01 实装并补 `pg_build.sh` phase 2.5（幂等）；`obs_derived_v1.sql` 对主库 `events` 增两个**加性**索引（`payload->>'text_display'` trgm GIN、`caused_by` 部分索引），不改 schema_v1 冻结列形。
