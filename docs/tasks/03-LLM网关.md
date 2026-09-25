# WorldSim · 开发任务文档 03 · LLM 网关（LLM）

> v1.0 · 2026-09-23。模板见 `00-总览与里程碑.md` §6；契约红线见 00 §4；里程碑定义见 00 §5；仓库布局见 00 §2。
> v1.1 · 2026-09-23 修订（按 `docs/tasks/review/round1-fixes.md` §A/§C/§D-03 修复）：models.yaml 键名对齐 01 T-CFG-02（`providers/task_routes/generation_params/thresholds`，R1 §A.11）；task_type 枚举补齐 9 项含 `world_copy`、网关不得拒绝（R1 §A.12）；embedding 定案本地 bge-m3（sentence-transformers 封装，dim=1024，智谱 embedding-3 留备选档），开发期路由全档走免费档 glm-4.5-flash/glm-4-flash 并补 reasoning_content/max_tokens 要点（00 §1 A9/A10）；成本熔断判级器 + ThrottleState + `system.llm.throttle` 唯一归 T-LLM-08（R1 §A.5），测试迁至 `server/tests/llm/test_cost_breaker.py`，基线未回填期统一"只告警不动作"（与 08 对齐）；T-LLM-04 交付物补 `check_config_fill.py`、T-LLM-09 变量补 `topic`/`willingness`（§C）；pytest/脚本调用统一 `cd server && uv run`（R1 §A.14）；"轮数"与熔断倍数行文改引用持有方（源方案 §3.4/§4.7、§5.2）；prompts/、schemas/、safety/wordlist.txt 布局对齐 00 §2；D6 定案、D8 销项（04 §1.6 已回改 `WSIM_ZHIPU_API_KEY`）。
> v1.2 · 2026-09-23 修订（按 `docs/tasks/review/round2-fixes.md` §A/§B-03 修复）：头部修订说明裸 A 编号补 `R1 §A.` 前缀（R2 §A.1；与 00 §1 A 表编号独立，00 §1 A9/A10 等引用不动）；T-LLM-08 设计依据删 `WSIM_COST_ALARM_RATIO`/`WSIM_COST_THROTTLE_RATIO` 两 env 名，熔断倍数载体 = models.yaml `thresholds:` 段（01 T-CFG-02），04 §1.6 两 env 名已作废（设计侧已回登）（R2 §A.2）；defer 表 vLLM 行 defer 目标改"上线部署期"（R2 §A.8）；偏差表"待 00 §1 登记"批量改"已登记（本文偏差表）"（R2 §A.12）。
> v1.3 · 2026-09-24 修订（对齐 `docs/tasks/review/round3-fixes.md` F2 #8）：T-LLM-03 验收 3 括注"（09 M0 E7 同口径）"改"（09 M2 E1 同口径）"——bge-m3 本地加载冒烟归 09 M2 E1，09 M0 E7 已静态化为 embed 配置与 DDL `vector(1024)` 对拍（R3-A P1-3 / R3-B #6）。
> 本文档不新造数字、字段名、事件类型；一切口径引用设计文档（事件类型/字段唯一仲裁方 = 06；数字唯一持有方以 06 §3 登记为准）。
> 任务 ID 前缀 `T-LLM-`，里程碑 M2 为主；其中 T-LLM-01/02 属 **M1**（内核以 mock provider 跑通，00 §5 M1）。

> **验收命令约定**：本文验收清单中的 `uv run ...` 命令 cwd = `server/`（前缀 `cd server &&` 依 00 §1 A14 省略书写）。

## 1. 范围

- LLM 网关包 `server/worldsim/llm_gateway/`（目录结构按 04 §2.1：`router.py` / `clients.py`（RPM 桶/退避）/ `ledger.py`（计量）/ `breaker.py`（熔断））。
- provider 抽象：`chat()` / `embed()` 两接口；实现三类 provider——**zhipu**（智谱 GLM 免费档 chat：glm-4.5-flash / glm-4-flash，OpenAI 兼容端点，httpx 直连，00 §1 A3/A9）、**本地 bge-m3 embedding**（sentence-transformers 封装，dim=1024，GPU 错峰/CPU 兜底，00 §1 A10；智谱 embedding-3 留 `models.yaml` 备选档，充值后切换）、**mock**（M1 用，seed 确定性）；另为本地 vLLM 预留接口位（实现 defer 上线部署期，00 §1 A3）。
- 分层模型路由表 `server/config/models.yaml`（明星/次要/背景三档 → task_type → provider 别名 + 降级链；开发期主用档 = 免费档 glm-4.5-flash/glm-4-flash，付费档留注释、充值后仅改配置升档，00 §1 A9；价格表占位）。
- RPM 令牌桶 + 优先级抢占 + 指数退避 + 超时（04 §8.5）；GLM 撞墙判定与降级链、熔断恢复探测（04 §8.2），落 `system.llm.failover` 事件（payload 逐字按 06 §1.2）。
- 成本计量：`llm_calls` 全量记录与 ¥/模拟日聚合（04 §8.3）；成本熔断两档（04 §8.4），落 `system.llm.throttle` 事件（06 §1.2）。
- prompt 模板注册表（版本化，覆盖动作族 think/chat/argue/gossip/confess/apologize/send_message/invite/refuse/reflect/batch 摘要/日摘要）。
- 输出结构化解析：JSON schema 校验 + 重试（04 §6.1 step3）。
- 内容安全三层过滤管线（04 §11.1，开发期层序适配见 §6 偏差表）：block → 重生成一次 → 仍 block → `visibility='internal'` 且原文不出站。
- `WSIM_REPLAY_MODE=replay` 零调用保证（04 §5.3）。

## 2. 不在范围（defer）

| 项 | defer 到 | 说明 |
|---|---|---|
| 本地 vLLM provider（Qwen3-8B 兜底 / Qwen3-4B 安全初筛）实现与唤醒/休眠流程 | 上线部署期 | 00 §1 A3；接口位在 T-LLM-01 预留。bge-m3 embedding **不**随本行 defer——开发期定案本地 bge-m3（00 §1 A10），由 T-LLM-03 交付 |
| 安全 L3 云端复核（切片发布前内容安全 API + 人工终审） | M6（内容期） | 04 §11.1 L3；开发期无发布通道 |
| `soft` 标签"降权不进切片素材池"的素材池联动 | M6 | 素材池属云端切片链路；M2 只记录标签 |
| 出站白名单剥除的抓包验证（04 §13 W1 增补①：block 事件原文物理不在帧内） | M6 | 同步链路在 07-同步与副本库；M2 只在网关/安全边界做单测 |
| ¥/模拟日熔断基线写死 `WSIM_COST_LIMIT_CNY_PER_SIMDAY` | M2 出口（T-LLM-12 产出实测值后） | 04 §8.4；回填登记见 §5 |
| 对话业务规则校验（`opening_fact`/`self_eval`/`quotable_lines` 自评字段、taboo/句长/catchphrase 语言差异化校验、≤40 字/句） | M1（02-模拟内核，裁决器侧） | 01 §7；网关只在 T-LLM-10 的 schema 中**携带**这些字段，判定与重生成决策在裁决管道 |
| `lines[].at_offset_s` 生成 | M1（02-模拟内核） | 04 §6.4：生成时按每句 uniform(2,4)s 累计写死，是内核 rng 职责，**不是 LLM 输出字段**（见 §6 偏差表 D5 防误实现提示） |

## 3. 任务依赖表

| 任务 | 依赖（本文档内） | 依赖（跨文档） |
|---|---|---|
| T-LLM-01 网关骨架与 provider 抽象 | 无 | T-DB（M0：`events`/`llm_calls` DDL）、T-CFG（M0：config 骨架与 .env 约定） |
| T-LLM-02 mock provider | T-LLM-01 | — |
| T-LLM-03 zhipu provider | T-LLM-01 | — |
| T-LLM-04 models.yaml 路由与热更 | T-LLM-03 | — |
| T-LLM-05 RPM 桶/退避/超时 | T-LLM-04 | — |
| T-LLM-06 降级链与熔断判定 | T-LLM-05 | — |
| T-LLM-07 ledger 计量 | T-LLM-03 | — |
| T-LLM-08 成本熔断两档 | T-LLM-07 | 判级器 + ThrottleState + `system.llm.throttle` 事件唯一归本任务（评审 R1 A5）；降速动作的消费接线归 08 T-OPS-02，读取点接口位由 02-模拟内核（裁决器/排程器）与 04 提供 |
| T-LLM-09 prompt 模板注册表 | T-LLM-04 | 人设卡 schema 见 01 §2.2；记忆配额检索结果结构由 02-模拟内核 T-MEM 提供；`topic`/`willingness` 变量分别由 02 T-REL-06 话题系统与 01 §3.5 公式供给 |
| T-LLM-10 输出结构化解析 | T-LLM-09 | — |
| T-LLM-11 安全三层过滤管线 | T-LLM-03、T-LLM-10 | — |
| T-LLM-12 真接入验收与实测回填 | T-LLM-01~11 全部 | 02-模拟内核 M1 出口（8 人 mock 1 模拟日可跑） |

下游依赖方：02-模拟内核（M1 起消费 mock provider 与网关接口，评审 R1 A7）；04-世界Agent与编剧导演（M3，task_type `director`/`world_copy`）；08-审计健康度（¥/模拟日与熔断事件进日报；T-OPS-02 消费 ThrottleState 做降速动作接线）。

---

## 4. 任务表

### T-LLM-01 网关骨架、provider 抽象与 REPLAY 守卫
- 里程碑 / 依赖: M1 ；无（跨文档依赖见 §3）
- 设计依据: 04 §2.1（llm_gateway 目录）、04 §2.2（`LLMGateway(db, load(WSIM_MODELS_CONFIG))` 构造形态）、04 §5.3（REPLAY_MODE 语义）、04 §1.6（`WSIM_REPLAY_MODE`/`WSIM_MODELS_CONFIG` 环境变量）、04 §5.2（`llm_calls.task_type` 枚举注释）
- 目标: 立起网关门面与 provider 两接口抽象，M1 内核可注入 mock，REPLAY 下物理零调用。（**M1 段交付**；网关接口为 02-模拟内核的消费接口，02 T-ADJ-02 仅声明依赖——评审 R1 A7。）
- 交付物: `server/worldsim/llm_gateway/__init__.py`（`LLMGateway` 门面）、`server/worldsim/llm_gateway/providers/base.py`（`ChatProvider`/`EmbedProvider` 协议类）、`server/tests/test_llm_gateway.py`
- 实现要点:
  - provider 协议：`async chat(task_type, messages, gen_params) -> ChatResult(text, prompt_tokens, completion_tokens, latency_ms, request_id)`；`async embed(texts) -> EmbedResult(vectors, prompt_tokens, ...)`。维度口径：`memories.embedding vector(1024)`（04 §5.2 DDL；开发期由本地 bge-m3 满足，00 §1 A10）。
  - task_type 封闭枚举取 `llm_calls` DDL 注释全集 **9 项**：`star_decision/dialogue/secondary/bgsummary/reflection/director/world_copy/safety/embed`（04 §5.2 注释与 04 §8.1 路由表一致，评审 R1 A12），代码内以 `Literal`/枚举钉死；枚举外 task_type 直接拒绝，**`world_copy` 不得拒绝**。
  - `WSIM_REPLAY_MODE=replay` 时一切 `chat/embed` 调用抛 `ReplayViolation`（04 §5.3"LLM 网关拒绝一切真实调用"）；守卫在门面层，provider 不可绕过。
  - 构造签名对齐 04 §2.2 伪代码；依赖方向遵守 04 §2.1 单向规则（gateway 不 import `scheduler/adjudicator`）。
- 验收标准:
  1. `cd server && uv run pytest tests/test_llm_gateway.py::test_replay_mode_refuses_all_calls` 绿：replay 下 chat/embed 均抛 `ReplayViolation`，且 httpx 无任何出站请求（mock transport 计数 = 0）。
  2. `cd server && uv run pytest tests/test_llm_gateway.py::test_unknown_task_type_rejected` 绿；另断言 9 项枚举（含 `world_copy`）全部放行。
  3. `grep -rn "import" server/worldsim/llm_gateway/ | grep -E "scheduler|adjudicator|world_agent"` 无输出（依赖方向单测或 grep 检查）。
- 实测回填: 无

### T-LLM-02 mock provider（seed 确定性，M1 内核用）
- 里程碑 / 依赖: M1 ；T-LLM-01
- 设计依据: 00 §5 M1（"六步管道（mock LLM）"、"replay 零 LLM 调用且状态一致"）、04 §5.3（LLM 输出不确定性说明，mock 是开发期工程替身非设计语义）
- 目标: 不联网、可复现的 provider，使 M1 内核全流程与 replay 一致性验证不依赖任何真实 LLM。（**M1 段交付**，02-模拟内核 M1 起消费，评审 R1 A7。）
- 交付物: `server/worldsim/llm_gateway/providers/mock.py`、`server/tests/test_llm_mock.py`
- 实现要点:
  - 输出 = 确定性函数 `f(seed, prompt)`：对每类 task_type 返回合法 JSON（schema 与 T-LLM-10 对齐）；`embed` 返回由 seed 派生的 1024 维单位向量（维度口径 04 §5.2）。
  - seed 来源 = 内核本 tick `rng_seed` 传入（04 §5.2 `events.rng_seed` 语义：规则骰子可复现）；同一 (seed, prompt) 输出逐字节相同。
  - mock 调用照常走 ledger 接口但写库由 T-LLM-07 控制开关（M1 期 `llm_calls` 可记 provider='mock'、cost 0；replay 模式一律不记）。
  - token 数用本地估算（字符数折算），仅用于 M1 联调，不代表真实计量。
- 验收标准:
  1. `cd server && uv run pytest tests/test_llm_mock.py::test_mock_deterministic` 绿：同 seed 同 prompt 两次调用输出全等；异 seed 输出不同。
  2. `cd server && uv run pytest tests/test_llm_mock.py::test_mock_output_passes_schema` 绿：全 task_type 的 mock 输出通过 T-LLM-10 的 JSON schema 校验（用例可先行 xfail 至 T-LLM-10 落地后转绿，禁止跳过）。
  3. 全程无网络：测试在 `pytest --disable-socket`（或等价 socket 封禁）下通过。
- 实测回填: 无

### T-LLM-03 zhipu provider（GLM 免费档 chat）与本地 bge-m3 embedding provider
- 里程碑 / 依赖: M2 ；T-LLM-01
- 设计依据: 00 §1 A3/A9（主供智谱 GLM，base `https://open.bigmodel.cn/api/paas/v4`，httpx 直连；2026-09-23 实测付费档与 embedding-3 余额不足，免费档 glm-4.5-flash / glm-4-flash 可用）、00 §1 A10（embedding 定案本地 bge-m3，dim=1024；智谱 embedding-3 留备选）、04 §8.1（单价与 RPM 上限开工当天以官方文档核实后写入 `models.yaml`）、04 §11.2（密钥管理）、00 §3（httpx 钉版）
- 目标: chat 走智谱免费档真实可用，embed 走本地 bge-m3 且维度与 DDL 一致；token 用量与延迟全量可回收。
- 交付物: `server/worldsim/llm_gateway/providers/zhipu.py`、`server/worldsim/llm_gateway/providers/local_embed.py`（bge-m3，sentence-transformers 封装）、`server/tests/test_llm_zhipu.py`、`server/tests/test_llm_zhipu_live.py`（联网冒烟，默认 skip）、`server/tests/test_llm_embed.py`
- 实现要点:
  - key 只从环境变量 `WSIM_ZHIPU_API_KEY` 读（00 §1 A3；04 §1.6 已回登同名）；代码/配置/日志/文档零硬编码 key；`deploy/worldsim.env.example` 只放变量名占位（该文件唯一归 08 T-OPS-04）。
  - 免费档型号表（开发期主用档，00 §1 A9）：`glm-4.5-flash` = 明星档（`star_decision/dialogue/reflection/director/world_copy`）；`glm-4-flash` = 次要/背景/摘要档（`secondary/bgsummary/safety`）。付费档（glm-4.5-air 等）只在 `models.yaml` 留注释，充值后仅改配置升档。
  - **glm-4.5-flash 是 reasoning 模型**（00 §1 A9）：响应含 `reasoning_content` 且**消耗 completion 配额**——`max_tokens` 必须留足（余量体现在 `generation_params`，参数表口径 04 §8.6）；正文只取 `content` 字段，`reasoning_content` 不落库、不进日志（04 §12.1 口径）。
  - httpx `AsyncClient(base_url="https://open.bigmodel.cn/api/paas/v4")`，`Authorization: Bearer`；chat 走 `response_format=json_object` 能力（04 §8.6 `response_format=json`）。
  - 本地 bge-m3 provider：`local_embed.py` 用 sentence-transformers 加载 bge-m3，输出维度 = 1024 与 DDL `vector(1024)` 对齐（04 §5.2）；GPU 与图像生成错峰使用，GPU 不可用自动 CPU 兜底（00 §1 A10）。`embed` 路由 primary 指向本 provider；智谱 embedding-3 条目留 `models.yaml` 备选档，充值后切换，切换前维度核实义务保留（§6 D6）。
  - 从响应 `usage` 回收 `prompt_tokens/completion_tokens`；`latency_ms` 本地计时；`request_id` 取响应头/体的请求 id（无则本地 uuid，04 §12.1"日志只记 request_id"）。
  - 超时：connect/read 超时为 `models.yaml` 可配项（工程默认值见 §6 D3）；429 响应解析 `Retry-After` 并上抛给退避层（04 §8.5）。
- 验收标准:
  1. `cd server && uv run pytest tests/test_llm_zhipu.py` 全绿（httpx MockTransport 构造 200/429/500/超时四类响应，断言 token/延迟/request_id 解析与异常上抛形态；含"响应带 `reasoning_content` 时只取 `content`"用例）。
  2. `cd server && uv run pytest tests/test_llm_zhipu_live.py` 在 `WSIM_ZHIPU_API_KEY` 存在时真实跑通一次 glm-4.5-flash chat；无 key 时 skip 且有显式 skip 原因。
  3. `cd server && uv run pytest tests/test_llm_embed.py` 绿：bge-m3 本地加载冒烟 + 输出向量维度断言 = 1024（09 M2 E1 同口径）。
  4. `mkdir -p deploy && grep -rn "sk-" server/ deploy/ || true` 无命中（`deploy/` 目录正式填充归 08 T-OPS-04、M6 起对其内容常驻检查；M2 期以 `mkdir -p` 为前提）；`git grep -n "WSIM_ZHIPU_API_KEY"` 命中处均为变量名引用而非值。
- 实测回填: 免费档单价（若有计费变动）与 RPM 上限、付费档型号 ID → `config/models.yaml` `providers:` 段（04 §8.1，T-LLM-12 收口）；embedding-3 备选档维度核实挂"充值后"（§6 D6）。

### T-LLM-04 `config/models.yaml` 路由表、生成参数与 SIGHUP 热更
- 里程碑 / 依赖: M2 ；T-LLM-03
- 设计依据: 01 T-CFG-02（models.yaml 四段键名 M0 冻结：`providers/task_routes/generation_params/thresholds`，评审 R1 A11 以其为准回改本文）、04 §8.1（路由表结构与"只绑 task_type→provider 别名，不硬编型号"）、04 §8.6（生成参数表）、04 §12.4（SIGHUP 热更范围与校验失败保旧配置）、04 §1.6（`WSIM_MODELS_CONFIG`）、06 §3（认知调用量等数字只引用持有方）
- 目标: 明星/次要/背景三档路由、降级链、生成参数、价格表全部配置化，可热更。
- 交付物: `server/config/models.yaml`（骨架与键名归 01 T-CFG-02，本任务填路由/参数实质内容）、`server/worldsim/llm_gateway/router.py`、`server/scripts/check_config_fill.py`、`server/tests/test_llm_router.py`
- 实现要点:
  - YAML 四段键名以 01 T-CFG-02 为准：`providers:`（别名 → type/model/base_url/rpm_limit/in-out 单价）、`task_routes:`（task_type → primary + `fallback[]` 链，覆盖 04 §8.1 全部 9 项 task_type）、`generation_params:`（task_type → temperature/max_tokens 等，逐行照 04 §8.6 表；glm-4.5-flash 行的 `max_tokens` 按 reasoning 模型留足，见 T-LLM-03 要点）、`thresholds:`（熔断倍数等，04 §12.4 口径"配置文件是设计数字的部署镜像"——熔断倍数数值唯一持有方 = 源方案 §5.2，04 §8.4/§1.6 为镜像，来源标注注释）。
  - 开发期路由落地（00 §1 A9/A10）：全部 task_type primary 走智谱免费档——`star_decision/dialogue/reflection/director/world_copy` → `glm-4.5-flash` 别名（明星档），`secondary/bgsummary/safety` → `glm-4-flash` 别名（次要/背景/摘要档）；`embed` → 本地 bge-m3 条目；付费档（glm-4.5-air 等）、智谱 embedding-3 备选档与本地 vLLM 条目先留注释占位（充值后仅改配置升档；vLLM 条目上线部署期启用，§6 D1/D2 偏差登记）。
  - 免费档型号 ID（glm-4.5-flash/glm-4-flash）按 00 §1 A9 实测定案直接写入；单价、RPM 上限与付费档型号 ID **不在本文档出现**，`models.yaml` 中留 `__FILL__` 占位，T-LLM-12 实测回填（04 §8.1"开工当天以官方文档核实"）。
  - SIGHUP：收到信号重读并校验 `models.yaml`；校验失败保留旧配置 + WARN（04 §12.4）；改价即刻生效于后续 `llm_calls` 计量。
  - `check_config_fill.py`（评审 R1 §C 增项）：扫描 `models.yaml` 校验 YAML 可解析、四段键名齐全，并列出残留 `__FILL__` 占位项——有占位项则退出码非零。
- 验收标准:
  1. `cd server && uv run pytest tests/test_llm_router.py::test_route_table_covers_all_task_types` 绿：9 项 task_type 全集（04 §8.1，含 `world_copy`）均有 primary，且每条 `fallback` 链末端行为定义（暂停时钟/排队延后）与 04 §8.1 一致。
  2. `cd server && uv run pytest tests/test_llm_router.py::test_sighup_reload_valid_and_invalid` 绿：合法修改热生效；非法 YAML 保留旧配置并产生 WARN 日志记录。
  3. `cd server && uv run python scripts/check_config_fill.py`：开发期有占位项（单价/RPM/付费档 ID）被列出且退出码非零；回填后退出码 0（T-LLM-12 验收 3 复用此脚本）。
- 实测回填: 单价表、RPM 上限、付费档型号 ID 三处 `__FILL__` → T-LLM-12 回填并登记 §5 清单。

### T-LLM-05 RPM 令牌桶、优先级抢占、指数退避与超时
- 里程碑 / 依赖: M2 ；T-LLM-04
- 设计依据: 04 §8.5（桶/优先级/退避全节）、04 §8.2（重试耗尽走降级链）
- 目标: 每 provider 一个令牌桶与优先级调度，重试语义与 04 §8.5 逐字一致。
- 交付物: `server/worldsim/llm_gateway/clients.py`、`server/tests/test_llm_clients.py`
- 实现要点:
  - 令牌桶容量 = `models.yaml` 该 provider 的 `rpm_limit`（核实值占位，04 §8.2"上限按开工当天核实值配置"）；桶空时低优先级排队、高优先级可抢占低优先级**待发**请求。
  - 优先级序钉死：`safety > star_decision > dialogue > secondary > bgsummary > director`（04 §8.5）。
  - 重试：指数退避 1s/2s/4s 最多 3 次、±20% 抖动；429 优先读 `Retry-After`（04 §8.5）；重试耗尽不抛业务异常，而是返回"撞墙"信号供 T-LLM-06 走降级链（04 §8.2）。
  - 每次尝试（含失败）产生一条待写 `llm_calls` 记录（status=`ok/retry/failed/fallback`，04 §5.2 枚举注释），交 T-LLM-07 落库。
  - 桶/退避时钟可注入（fake clock），保证测试确定性与"LLM 并发、落库串行"（00 §4-10）不冲突：并发限制在网关内，落库顺序由裁决协程保证。
- 验收标准:
  1. `cd server && uv run pytest tests/test_llm_clients.py::test_bucket_rate_limit` 绿：fake clock 下 N 次调用耗时 ≥ 桶容量推算下界。
  2. `cd server && uv run pytest tests/test_llm_clients.py::test_priority_preemption` 绿：桶空时 safety 请求先于排队中的 bgsummary 发出。
  3. `cd server && uv run pytest tests/test_llm_clients.py::test_backoff_sequence_and_retry_after` 绿：429 带 `Retry-After: 7` 时等待 7s（而非 1/2/4 序列）；普通 500 按 1s/2s/4s ±20% 抖动（断言落在区间内）；第 4 次不再重试并返回撞墙信号。
  4. `cd server && uv run pytest tests/test_llm_clients.py::test_every_attempt_recorded` 绿：3 次重试产生 3 条 status=`retry` 记录 + 终态 1 条。
- 实测回填: GLM 各档实测 RPM 上限 → `models.yaml` `rpm_limit`（T-LLM-12 登记）。

### T-LLM-06 降级链、撞墙判定与 `system.llm.failover`
- 里程碑 / 依赖: M2 ；T-LLM-05
- 设计依据: 04 §8.2（三条撞墙判定 + 30 分钟冷却 + 单请求探测恢复 + 事件留痕）、06 §1.2（`system.llm.failover`：source=system、trigger=system、payload `{task_type, from_provider, to_provider, reason}`、internal 事件不携带展示文本）、04 §5.2（`llm_calls.fallback_from`）
- 目标: 撞墙三条件任一满足即沿路由表降级链切换，冷却探测恢复，全程事件与 ledger 双留痕。
- 交付物: `server/worldsim/llm_gateway/breaker.py`（撞墙判定机）、`server/worldsim/llm_gateway/router.py`（降级链执行，增量）、`server/tests/test_llm_breaker.py`
- 实现要点:
  - 撞墙条件（04 §8.2 逐字）：①连续 3 次 HTTP 429/5xx（重试耗尽后计）；②滑动 5 分钟窗口 RPM 使用率 >90%；③滑动 5 分钟 p95 延迟 >8s。窗口数据源 = T-LLM-05 的调用记录流。
  - 切换目标取 `models.yaml` 该 task_type 的 `fallback[]` 下一跳；链尽行为按 04 §8.1：明星档暂停时钟（经裁决器接口）、director 排队延后。
  - 冷却 30 分钟后单请求探测，成功即切回（04 §8.2）。
  - 切换与恢复各落一条 `events`：`type='system.llm.failover'`、`source='system'`、`trigger='system'`、`visibility='internal'`、payload 四键 `{task_type, from_provider, to_provider, reason}` 逐字对齐 06 §1.2，**不得携带 `text_display`**（06 §1.2 internal 事件口径）。
  - 降级期所有调用 `llm_calls.fallback_from` = 原 provider 别名（04 §8.2"W2 实测单价时要能区分免费档实际扛了多少"）。
- 验收标准:
  1. `cd server && uv run pytest tests/test_llm_breaker.py::test_trip_on_each_condition_and_recover` 绿：三种触发分别构造命中，冷却 30 分钟（fake clock）后探测成功切回。
  2. `cd server && uv run pytest tests/test_llm_breaker.py::test_failover_event_payload_contract` 绿 + SQL 验证：落库后 `SELECT type, source, trigger, visibility, payload FROM events WHERE type='system.llm.failover' ORDER BY seq DESC LIMIT 1;` 的 payload 键集精确等于 `{task_type, from_provider, to_provider, reason}` 且不含 `text_display`。
  3. `cd server && uv run pytest tests/test_llm_breaker.py::test_fallback_from_recorded` 绿：降级期调用行 `fallback_from` 非空，恢复后新行为 NULL。
- 实测回填: 无（撞墙阈值数字唯一持有方 = 04 §8.2，本文不复制之外的新值）。

### T-LLM-07 ledger 计量：`llm_calls` 全量记录与 ¥/模拟日聚合
- 里程碑 / 依赖: M2 ；T-LLM-03
- 设计依据: 04 §8.3（全量记录、成本公式、¥/模拟日口径、7 模拟日滚动平滑）、04 §5.2（`llm_calls` DDL 列与索引）、04 §11.2（`llm_calls` 明细永不出站、只记 `prompt_hash`）、04 §12.1（prompt/响应全文不进日志）
- 目标: 每次调用（含失败与重试）落一行，成本按价格表可算，¥/模拟日一条 SQL 出数。
- 交付物: `server/worldsim/llm_gateway/ledger.py`、`server/scripts/llm_cost.sql`、`server/tests/test_llm_ledger.py`
- 实现要点:
  - 列逐一映射 DDL（04 §5.2）：`task_type/agent_id/provider/model/prompt_tokens/completion_tokens/cost_micro_cny/latency_ms/status/fallback_from/request_id/prompt_hash`；`sim_time` 由内核时钟注入（ nullable 按 DDL，但正常路径必填，聚合依赖）。
  - `cost_micro_cny = prompt_tokens×in_price + completion_tokens×out_price`（04 §8.3；单价读 `models.yaml`，微元整数运算防浮点漂移）；mock/本地 provider 记 0 但记录耗时（04 §8.3 归账口径）。
  - `prompt_hash` = 渲染后完整 prompt 的 SHA-256 hex（工程默认值，§6 D4）；prompt 原文只进 hash，不落库不落日志（04 §11.2/§12.1）。
  - 聚合 SQL（04 §8.3 公式）：`SELECT SUM(cost_micro_cny)::float/1e6 / COUNT(DISTINCT date(sim_time)) AS cny_per_simday FROM llm_calls;` 另出 7 模拟日滚动平滑变体；写入 `scripts/llm_cost.sql` 供审计日报（08 文档）引用。
  - 写库串行：ledger 提供 `record()` 由裁决协程调用/批量 flush，遵守"LLM 并发、落库串行"（00 §4-10）。
- 验收标准:
  1. `cd server && uv run pytest tests/test_llm_ledger.py::test_cost_formula_and_full_columns` 绿：fixture 调用后行各列非空且 `cost_micro_cny` 与手算一致（含 in/out 不同单价用例）。
  2. SQL 对账：灌入已知 fixture 后 `psql -f server/scripts/llm_cost.sql` 的 `cny_per_simday` 与 Python 侧按同公式计算值相等（误差 0）。
  3. `cd server && uv run pytest tests/test_llm_ledger.py::test_no_prompt_plaintext_anywhere` 绿：构造含哨兵字符串的 prompt，断言 `llm_calls` 全表与日志捕获中均无哨兵原文、仅有其 hash。
- 实测回填: ¥/模拟日初测值在 T-LLM-12 产出；本任务只保证公式与管道正确。

### T-LLM-08 成本熔断两档与 `system.llm.throttle`
- 里程碑 / 依赖: M2 ；T-LLM-07
- 设计依据: 04 §8.4（报警/降速/恢复三级表与五条降速动作）、04 §1.6（`WSIM_COST_LIMIT_CNY_PER_SIMDAY` 环境变量）、熔断倍数载体 = models.yaml `thresholds:` 段（01 T-CFG-02；04 §1.6 两 env 名已作废（设计侧已回登）；倍数值见源方案 §5.2）、06 §1.2（`system.llm.throttle` payload `{level: alarm/throttle/recover, ratio}`）、06 §3（成本熔断线口径持有方 = 源方案 §5.2）、评审 R1 A5（判级唯一归属）
- 目标: 滚动窗口单价越线即按级别动作，事件落库，恢复逐级撤销。
- 交付物: `server/worldsim/llm_gateway/breaker.py`（成本熔断段，增量）、`server/tests/llm/test_cost_breaker.py`
- 实现要点:
  - **唯一归属（评审 R1 A5）**：成本熔断判级器 + `ThrottleState` + `system.llm.throttle` 事件只在本任务实现（`llm_gateway/breaker.py`），测试统一 `server/tests/llm/test_cost_breaker.py`；08 T-OPS-02 只做降速动作消费接线（读取 `ThrottleState`）、告警接 T-OPS-01、24h 未恢复转 T-OPS-03 `clock.pause`，不重复实现判级器与事件。
  - 输入 = T-LLM-07 聚合的滚动 7 模拟日 ¥/模拟日；基线读 `WSIM_COST_LIMIT_CNY_PER_SIMDAY`。
  - 基线未回填期（M2 出口前基线为空）行为统一写死：**只告警不动作**——产生 WARN（接入 08 T-OPS-01 告警通道）但不执行任何降速/暂停动作，不臆造默认基线（与 08 文档口径一致）。
  - 级别动作（三级表见 04 §8.4；熔断倍数与持续条件数值唯一持有方 = 源方案 §5.2，本文不复制）：报警档持续越线 → 告警 + 日报置顶 + 等人工确认；降速档持续越线 → 五条动作（其中"①secondary/bgsummary 强制本地 8B"开发期适配为强制 GLM 最低档别名，§6 D1 偏差登记）；24 真实小时后仍超降速线 → 经 08 T-OPS-03 暂停时钟等人工；回落至恢复线下 → 逐级撤销。
  - 状态迁移各落 `system.llm.throttle`（source/trigger=`system`，internal，payload 仅 `{level, ratio}` 两键，逐字 06 §1.2）。
  - 降速五动作以网关侧开关对象 `ThrottleState` 暴露（对话场次上限系数、深度反思阈值上调、director 调用减半等，动作参数数值口径 04 §8.4），读取点由 08 T-OPS-02 接线（02 裁决器/排程器、04 director 侧提供接口位）；本任务只保证开关状态机正确。
- 验收标准:
  1. `cd server && uv run pytest tests/llm/test_cost_breaker.py::test_alarm_then_throttle_then_recover` 绿：注入成本序列驱动 alarm → throttle → recover 全链路，每迁移断言 `ThrottleState` 五开关值（含反思阈值 20→35、对话上限 60%，口径 04 §8.4）。
  2. SQL：`SELECT payload FROM events WHERE type='system.llm.throttle' ORDER BY seq;` 键集精确等于 `{level, ratio}`，`level` 序列 = alarm/throttle/recover。
  3. `cd server && uv run pytest tests/llm/test_cost_breaker.py::test_no_baseline_warn_only` 绿：基线未配置时熔断只告警（WARN 记录存在）不动作。
- 实测回填: 熔断基线值（= T-LLM-12 产出的 ¥/模拟日初测）→ `.env` 与 06 §3"项目止损线"行的回填动作，见 §5。

### T-LLM-09 prompt 模板注册表（版本化，全动作族）
- 里程碑 / 依赖: M2 ；T-LLM-04
- 设计依据: 04 §6.1 step1/step3（感知注入结构化 obs、人设卡每次必带、render(agent, obs, mems, goals) 形态）、04 §4.3（次要层 prompt ≤900 tokens 与背景层批量摘要结构）、04 §7.1（配额检索结果 N/M/K 桶）、04 §7.2（深度反思/每日兜底反思）、01 §7（对话规则：开场白/自评/收尾/台词约束）、01 §3.5（意愿分注入：供 prompt 与观测、不做判定）、01 §4.1（gossip 失真规则，fidelity 为内核计算输入）、01 §5.2（改期理由由 LLM 生成）、06 §2（fidelity 不进 payload 不出站）
- 目标: 全部 LLM 出口文本经版本化模板渲染，变量契约显式，渲染产物可直接 hash 计量。
- 交付物: `server/config/prompts/*.yaml`（每模板一文件：`{id, version, task_type, variables[], system, user}`）、`server/worldsim/llm_gateway/prompts.py`（注册表/渲染/校验）、`server/tests/test_llm_prompts.py`
- 实现要点:
  - 模板族清单（task_type 映射）：`think`（轻反思档，04 §6.2 think 行）→ secondary；`chat/argue/gossip/confess/apologize`（整段对话生成，04 §6.4 + 01 §7；对话轮数口径见源方案 §3.4/§4.7，本文不复制数值）→ dialogue；`send_message` → dialogue；`invite/refuse`（含改期理由生成，01 §5.2）→ dialogue；`reflect`（深度反思，04 §7.2）→ reflection；`daily_summary`（每日兜底反思 04 §7.2 + 升格追赶反思 04 §4.3）→ secondary；`batch_summary`（背景层批量摘要 04 §4.3、记忆摘要合并 04 §7.3）→ bgsummary。
  - 变量契约（每模板 `variables[]` 显式声明，渲染缺变量即错）：`persona`（人设卡，明星层全量/次要层压缩版，04 §6.1 step1、§4.3）、`obs`（结构化感知）、`mems`（配额检索结果三桶，04 §7.1）、`goals`、`topic`（当前话题条目，由 02 T-REL-06 话题系统供给，口径 01 §7）、`willingness`（意愿分，内核按 01 §3.5 公式计算后注入，供 prompt 与观测、不做判定）；gossip 族额外 `fidelity` 与失真档位指令——**fidelity 由内核按 01 §4.1 计算后作为模板变量注入，不是 LLM 输出字段、不进 payload**（06 §2）。
  - 对话模板输出契约带 01 §7 结构化字段（`lines[]/opening_fact/self_eval/quotable_lines`），字段语义消费在裁决器（02 文档）；`at_offset_s` 不在模板输出中（§6 D5）。
  - 版本化：渲染返回 `(template_id, version, text)`；`prompt_hash` 计算输入 = 模板版本 + 渲染文本（T-LLM-07 接口）。
- 验收标准:
  1. `cd server && uv run pytest tests/test_llm_prompts.py::test_all_templates_render_with_fixture` 绿：12 个模板族全部用 fixture 上下文渲染成功（fixture 含 `topic`/`willingness`）；缺失变量渲染抛 `MissingVariable`。
  2. `cd server && uv run pytest tests/test_llm_prompts.py::test_gossip_template_injects_fidelity` 绿：fidelity=0.6 时渲染文本包含"丢数字/时间"档指令（阈值口径见 01 §4.1，模板只引用不复制数值——断言指令档位存在即可）。
  3. `grep -rn "at_offset_s" server/config/prompts/` 无输出。
  4. 次要层模板渲染 fixture 输出 ≤900 tokens 预算有断言（预算持有方 04 §4.3；以 tiktoken 或字符上界近似，方法写入测试注释）。
- 实测回填: 无

### T-LLM-10 输出结构化解析（JSON schema 校验 + 重试）
- 里程碑 / 依赖: M2 ；T-LLM-09
- 设计依据: 04 §6.1 step3（parse_json、解析失败重试 1 次追加格式纠错后缀、仍失败记 think"走神了"并落 llm_calls）、04 §8.6（`response_format=json` 档）、01 §7（对话输出结构化自评字段形态）
- 目标: 每类 task_type 一份输出 JSON schema，校验失败路径与设计逐字一致。
- 交付物: `server/worldsim/llm_gateway/parse.py`、`server/config/schemas/*.json`（每 task_type 一份 schema；`schemas/` 与 `prompts/` 平级，布局以 00 §2 为准，§6 D9）、`server/tests/test_llm_parse.py`
- 实现要点:
  - schema 集：`star_decision`（`{intent, action:{type 枚举, args}, say?, emotion_delta}`，04 §6.1 step3）、`secondary`（04 §4.3 输出段）、`bgsummary`（04 §4.3 输出段）、`dialogue`（`{lines:[{speaker, text}], opening_fact, self_eval, quotable_lines}`，01 §7；`lines` 轮数上下界断言，口径见源方案 §3.4/§4.7）、`reflection`（2~3 条洞察，04 §7.2）。
  - 失败路径钉死：校验/解析失败 → 重试 1 次（prompt 追加格式纠错后缀）→ 仍失败 → 返回结构化"降级决策"（think"走神了"）交裁决器，并在 `llm_calls` 落 `status='failed'` 行（04 §6.1 step3）。
  - 动作 `type` 枚举校验 = 19 项动作空间（唯一持有方 01 §4，schema 从共享常量生成，禁止抄副本）；参数级业务校验（前置校验列）不在本任务，属裁决器（04 §6.2）。
- 验收标准:
  1. `cd server && uv run pytest tests/test_llm_parse.py::test_valid_outputs_all_types` 绿：各 task_type 合法样例过 schema。
  2. `cd server && uv run pytest tests/test_llm_parse.py::test_retry_once_then_fallback_think` 绿：首次非法 → 重试请求带纠错后缀（mock provider 断言 prompt 增量）；二次仍非法 → 返回 think 降级决策且恰好 2 次 provider 调用。
  3. `cd server && uv run pytest tests/test_llm_parse.py::test_action_enum_matches_19` 绿：schema 动作枚举与共享常量集相等（19 项口径见 01 §4）。
- 实测回填: 无

### T-LLM-11 内容安全三层过滤管线
- 里程碑 / 依赖: M2 ；T-LLM-03、T-LLM-10
- 设计依据: 04 §11.1（三层管线、{pass, soft, block} 标签、block→重生成一次→仍 block 置 internal 并记安全审计）、04 §11.2（`text_raw` 永不出站、双通道分离）、06 §1.2（`text_display` 全局条件键：仅 `visibility='public'` 事件放行）、03 §7（展示通道/内部通道字段级规则）、06 §2（text_display/text_raw 对照行）
- 目标: 所有将置 `visibility='public'` 的文本（对话、公告、反思展示句）落库前过管线；block 原文不出站。
- 交付物: `server/worldsim/safety/__init__.py`（管线门面）、`server/worldsim/safety/rules.py`（层 1 本地规则）、`server/config/safety/wordlist.txt`（敏感词表，本地配置不进文档）、`server/tests/test_safety.py`
- 实现要点:
  - 层序（开发期适配，§6 D2 登记）：**层 1 本地规则** = 04 §11.1 的 L2（敏感词表 + 正则，政治/色情/暴恐/未成年人四类，毫秒级短路）；**层 2 GLM 审核** = 04 §11.1 的 L1 在本地 Qwen3-4B 缺位期的替身（task_type=`safety`，标签输出 {pass, soft, block}，gen_params 见 04 §8.6 safety 行）；**层 3 落库前复查** = 写库前对最终 `text_display` 再跑一次层 1（防模板拼装/二次加工引入）。
  - 判定语义照 04 §11.1：`soft` → 标签随行记录（素材池联动 defer M6）；`block` → 同事件重生成一次 → 仍 block → 事件 `visibility='internal'`、**不携带 `text_display`**（06 §1.2 internal 事件口径）、原文只留 `text_raw`（本机-only，04 §11.2）并记安全审计（落点 = 结构化 WARN 日志 + `llm_calls` safety 行，§6 D7 疑点）。
  - 管线同时服务记忆 `content_display` 写入点（04 §11.2/§7.2 反思双通道）；接口 `check(text) -> {label, filtered_text}`。
- 验收标准:
  1. `cd server && uv run pytest tests/test_safety.py::test_l1_short_circuits_l2` 绿：词表命中样本不触发任何 safety LLM 调用（调用计数 = 0）。
  2. `cd server && uv run pytest tests/test_safety.py::test_block_regenerate_once_then_internal` 绿：连续 block → 恰好 1 次重生成 → 事件 internal；SQL `SELECT visibility, payload ? 'text_display' AS has_td FROM events WHERE seq=:seq;` 返回 `(internal, false)`。
  3. `cd server && uv run pytest tests/test_safety.py::test_raw_never_in_display_path` 绿：哨兵原文仅出现在 `text_raw`/`memories.content`，`text_display`/`content_display` 与管线返回值中均无。
  4. `cd server && uv run pytest tests/test_safety.py::test_l3_recheck_on_final_text` 绿：层 2 pass 但终稿被加工出词表命中词时，层 3 拦截。
- 实测回填: 无（GLM 审核接口的误杀/漏判率观察记入 T-LLM-12 日报，不设阈值）。

### T-LLM-12 M2 出口：真实 GLM 跑 1 模拟日与实测回填
- 里程碑 / 依赖: M2 ；T-LLM-01~11 全部（跨文档依赖见 §3）
- 设计依据: 00 §5 M2 出口标准（真实 GLM 跑 1 模拟日、`llm_calls` 全量、¥/模拟日初测值产出）、04 §8.1（定价/RPM 核实回填；免费档型号已定案，00 §1 A9）、04 §8.3（¥/模拟日口径）、04 §8.4（熔断基线 = 初测单价）、06 §3（项目止损线行"单价上限 W2 写死"回填位）、00 §1 A6（实测回填纪律）
- 目标: 真实 provider 全链路跑通 1 模拟日，产出并登记全部实测回填项。
- 交付物: `server/scripts/llm_cost.sql` 实跑输出、回填后的 `server/config/models.yaml`（单价/rpm_limit/付费档型号）、`.env` 的 `WSIM_COST_LIMIT_CNY_PER_SIMDAY`（本机，gitignored）、向 08 T-OPS-04 提 `deploy/worldsim.env.example` 占位条目需求（该文件唯一归 08，评审 R1 §A13）、回填记录（commit message 前缀 `T-LLM-12`，逐项列数值与登记位置）
- 实现要点:
  - 跑法：M1 的 8 人世界脚本（02 文档产物）将 `models.yaml` primary 从 mock 切至 zhipu 免费档（glm-4.5-flash/glm-4-flash），跑满 1 模拟日；期间观察 breaker/降级/熔断日志。
  - 回填清单逐项执行（见 §5），含向 06 §3 的回填 PR（单价上限口径）与 04 §8.1 路由表单价/RPM 落值（免费档型号 ID 已在 T-LLM-03/04 按 00 §1 A9 写入，无需回填）。
  - REPLAY 回归：跑完后以 `WSIM_REPLAY_MODE=replay` 重放该模拟日事件流，断言网关零调用（02 文档 `scripts/replay_check.py` 联动）。
- 验收标准:
  1. `psql -c "SELECT COUNT(*), COUNT(*) FILTER (WHERE provider='zhipu') FROM llm_calls WHERE sim_time >= :day_start;"` 两值相等且 >0（全量且真实）。
  2. `psql -f server/scripts/llm_cost.sql` 输出 ¥/模拟日初测值，数值写入 commit message 与 §5 登记位。
  3. `cd server && uv run python scripts/check_config_fill.py` 退出码 0（`models.yaml` 无 `__FILL__` 残留）。
  4. replay 重放期间 `SELECT COUNT(*) FROM llm_calls` 前后差 = 0；`test_replay_mode_refuses_all_calls` 保持绿。
- 实测回填: 本任务即回填动作本体，清单见 §5。

---

## 5. 风险与实测回填清单

| # | 回填/风险项 | 口径与设计依据 | 产出任务 | 登记位置 |
|---|---|---|---|---|
| R1 | GLM in/out 单价与付费档型号 ID | 免费档型号 ID 已定案（glm-4.5-flash/glm-4-flash，00 §1 A9）；单价与付费档 ID"开工当天以官方文档核实"（04 §8.1），占位待 M2 实测回填 | T-LLM-03 起、T-LLM-12 收口 | `config/models.yaml` `providers:` 段 |
| R2 | GLM 各档 RPM 上限 | "上限按开工当天核实值配置"（04 §8.2/§8.5） | T-LLM-05 占位、T-LLM-12 实测 | `config/models.yaml` `rpm_limit` |
| R3 | ¥/模拟日初测值 | 公式 04 §8.3；是熔断基线与项目止损线的输入 | T-LLM-12 | `.env` `WSIM_COST_LIMIT_CNY_PER_SIMDAY` + 06 §3"项目止损线"行（W2 写死口径）+ commit 留痕 |
| R4 | 免费/低档实际抗压占比 | `llm_calls.fallback_from` 留痕动机（04 §8.2） | T-LLM-06 数据、T-LLM-12 分析 | 审计日报（08 文档引用） |
| R5 | 次要层调用量口径（192+40 上限 vs 原 ~144 假设作废） | 04 §8.1（评审二轮 N-P1-7），W2 实测定稿 | T-LLM-12 提供计量数据 | 由 04 §8.1 持有方定稿，本模块只供数 |
| R6 | GLM 审核（层 2）稳定性 | 本地 Qwen3-4B 缺位期替身（00 §1 A3），误杀率高时评估提前启用本地档 | T-LLM-11/T-LLM-12 观察 | 日报观察项；不预设阈值 |
| R7 | 单 tick 预算内 LLM 延迟占比 | "p95 须 < 预算 60%（W2 实测校准）"（04 §2.2） | T-LLM-12 从 `llm_calls.latency_ms` 出 p95 | 02-模拟内核排程调参输入 |

## 6. 适配与偏差（本模块特有；00 §1 已登记者不重复）

| # | 偏差/适配 | 说明与依据 | 状态 |
|---|---|---|---|
| D1 | 成本降速动作①"强制本地 8B"开发期改为"强制 GLM 最低档别名" | 04 §8.4 原动作依赖本地 vLLM（上线部署期才就位，00 §1 A3）；开发期无本地档可切 | **已登记（本文偏差表）** |
| D2 | 安全管线层序适配：层 1 本地规则 / 层 2 GLM 审核 / 层 3 落库前复查 | 04 §11.1 原序为 L1 本地 Qwen3-4B 分类器、L2 规则短路、L3 云端复核；本地 4B 与云端复核分别缺位于 M2（00 §1 A3）与 M6，故以 GLM 审核顶替 L1 并把规则层前置短路（04 §11.1 本来就规定规则"先于 L1 短路"，语义保留）；L3 云端复核 defer M6 | **已登记（本文偏差表）** |
| D3 | httpx connect/read 超时、探测请求间隔等工程默认值为代码配置项 | 设计未定数值；均进 `models.yaml` 可配，不在文档写死 | **已登记（本文偏差表）** |
| D4 | `prompt_hash` = SHA-256 hex（模板版本 + 渲染文本） | 04 §11.2/§12.1 只规定"只记 hash 不记原文"，算法未指定 | 工程默认值（无需契约变更） |
| D5 | `lines[].at_offset_s` 由内核按每句 uniform(2,4)s 累计写死，**不是 LLM 输出字段** | 04 §6.4；防误实现提示：网关 schema（T-LLM-10）与模板（T-LLM-09）均不得包含该字段 | 提醒，非偏差 |
| D6 | embedding 定案本地 bge-m3（dim=1024，与 DDL `vector(1024)` 一致，04 §5.2）；智谱 embedding-3 留 `models.yaml` 备选档 | 评审 R1 裁定（00 §1 A10）：智谱账号实测 embedding-3 余额不足，开发期走本地 bge-m3；备选档切换前须核实其维度参数支持 1024，不支持则停手回登 06/04 §5.2 再动 DDL | **已定案**（2026-09-23 评审 R1），维度阻塞销项 |
| D7 | **疑点**："仍 block 则置 internal 并记安全审计"（04 §11.1）的"安全审计"无 DDL 落点（04 §5.2 无 safety_audit 表，06 §1.2 无对应事件类型） | 本模块 interim：结构化 WARN 日志 + `llm_calls`（task_type='safety'）行留痕；若需表/事件，须先回登 06 再实现（00 §4-5） | **待回登 06 裁决** |
| D8 | ~~04 §1.6 环境变量表仍列 `WSIM_GLM_API_KEY`~~ | 04 §1.6 已回改为 `WSIM_ZHIPU_API_KEY`（2026-09-23 评审 R1 仲裁者回登），与 00 §1 A3 一致 | **已销项** |
| D9 | `server/config/prompts/`、`server/config/schemas/`、`server/config/safety/wordlist.txt` 布局登记 | 00 §2 仓库布局已补此三项；`schemas/` 与 `prompts/` 平级——T-LLM-10 交付物据此定为 `server/config/schemas/*.json`，不放 `prompts/` 子目录 | 已对齐 00 §2（2026-09-23 评审 R1） |
| D35 | **rpm_limit 未回填期路由模式工程默认 60**（网关 `DEFAULT_RPM_LIMIT` 常量） | models.yaml `rpm_limit` 为 T-LLM-12 实测回填项（04 §8.2"开工当天核实值"）；回填前降级链路由模式以保守默认运转，回填后一律以 yaml 为准 | **已登记（本文偏差表）** |
| D36 | **单价单位 = ¥/百万 tokens**：`cost_micro_cny = prompt_tokens×in_price + completion_tokens×out_price`（tokens×¥/Mtok 数值上恰为微元，整数化 round） | 04 §8.3 给了公式未给单价单位；单位落 ledger.py 文档串与 models.yaml 注释 | **已登记（本文偏差表）** |
| D37 | **04 §8.5 优先级序未列 task_type（reflection/world_copy/embed）取中档默认优先级 3** | 04 §8.5 只列 6 项优先级；其余任务桶内排队取中档（clients.py `DEFAULT_PRIORITY`） | **已登记（本文偏差表）** |
| D38 | **dialogue schema `lines` 上下界取全族并集 1~8** | chat 6~8 轮（源方案 §3.4/§4.7）/ argue 2~4 轮（01 §4.1）/ send_message 恰 1 条共用 dialogue 输出契约；细分轮数约束在模板措辞与裁决器侧，schema 只钉并集上下界 | **已登记（本文偏差表）** |
| D39 | **safety 层 2 审核输出无法解析为标签时保守按 block 处理** | 04 §11.1 未定解析失败口径；宁误杀不放行（误杀观察归 T-LLM-12 日报，§5 R6） | **已登记（本文偏差表）** |
