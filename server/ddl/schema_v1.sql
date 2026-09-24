-- ============================================================================
-- WorldSim 主库 Schema v1（W1 冻结实现；tag schema-v1 即冻结点，00 §1 A8 / 04 §13 W1）
-- 原文口径 = docs/design/04-服务器详细设计.md §5.2 逐字；仲裁 = docs/design/06-契约登记表.md
--
-- 契约口径注记：
--  - agent id 一律 TEXT 'A01'~'A40'（06 §2 / 00 §4 红线 1）
--  - trigger 封闭六枚举（06 §1.1）：autonomous|world|director|gift|vote|system
--  - **不设事件类型 CHECK**（兼容演进，04 §5.1 / 09 M0 E5 注记）；类型注册表唯一持有方 = 06 §1.2
--  - events append-only 双保险（04 §5.2）：BEFORE UPDATE/DELETE 触发器 RAISE EXCEPTION（对表 owner 亦生效）
--    + REVOKE UPDATE, DELETE FROM worldsim（应用角色仅 GRANT SELECT, INSERT）
--  - debts 方向语义（01 文档 v1.2 / round2 §A.18）：a_id = 债权人（债主）→ b_id = 债务人（欠款人）。
--    04 §5.2 行内注释（a=欠款人/b=债主）与此相反，以任务文档登记为准（01 文档 §6 D13）
--
-- 工程适配（PG16 物理约束，01 文档 §6 D12 登记；04 §5.2 字面句不可直接执行）：
--  D12-a `CREATE UNIQUE INDEX events_seq_uidx ON events (seq)`：分区表唯一索引必须包含分区键（PG16 报错），
--        落法 = pg_partman 模板表唯一索引（未来分区自动继承）+ 现存分区逐一补建 `<分区>_seq_uidx`；
--        seq 全局唯一由 GENERATED ALWAYS AS IDENTITY（单一序列）结构性保证（云端幂等依据不变，05 §3.1）。
--  D12-b `llm_calls_simday` 的 `date_trunc('day', sim_time)`：timestamptz 变体为 STABLE，索引表达式须 IMMUTABLE；
--        落法 = `date_trunc('day', sim_time AT TIME ZONE 'Asia/Shanghai')`（named-zone timezone() 与
--        timestamp 变体 date_trunc() 均 IMMUTABLE；业务语义 = 按本地时区聚合模拟日，时区见 01 文档 §6 D4）。
--  D12-c pg_partman 5.5 create_parent 签名：(p_parent_table, p_control, p_interval, p_type='range', p_premake…)；
--        周分区 p_interval='1 week'，premake=4（≥4 周，T-DB-01）。
--
-- 执行：空库一次跑通（T-DB-01 验收 1）
--   server/scripts/db_init.sh reset && psql "$WSIM_PG_DSN" -v ON_ERROR_STOP=1 -f server/ddl/schema_v1.sql
-- 前置：库内已 CREATE EXTENSION vector / pg_partman（db_init.sh / conftest fixture 以 superuser 完成）。
-- ============================================================================

CREATE EXTENSION IF NOT EXISTS vector;

-- ---------------------------------------------------------------------------
-- 事件表：append-only，按 sim_time 周分区（pg_partman 模板）
-- ---------------------------------------------------------------------------
CREATE TABLE events (
  seq BIGINT GENERATED ALWAYS AS IDENTITY,
  tick BIGINT NOT NULL,
  sim_time TIMESTAMPTZ NOT NULL, wall_time TIMESTAMPTZ NOT NULL DEFAULT now(),
  type TEXT NOT NULL,                 -- <domain>.<object>[.<verb>]，注册表见 06（如 dialogue.chat/agent.move/relation.changed）
  source TEXT NOT NULL,               -- 'agent:<aid>' | 'world' | 'director' | 'system'
  trigger TEXT NOT NULL CHECK (trigger IN ('autonomous','world','director','gift','vote','system')),
  arc_id TEXT, location_id TEXT,          -- 故事弧线(可空) / 地点节点（目击投影依据）
  actors TEXT[] NOT NULL DEFAULT '{}', -- 参与者 agent id（'A01'~'A40'；双人事件=[A,B]）
  rng_seed BIGINT,                    -- 只复现规则骰子（§5.3）
  payload JSONB NOT NULL,             -- 键约定见 §5.1（caused_by/cites/lines/text_raw/text_display）
  visibility TEXT NOT NULL DEFAULT 'internal' CHECK (visibility IN ('public','internal')),
  ui JSONB,                           -- UI 展示字段（气泡样式/镜头提示/切片标记）；ui.grade=自动打分初值（§6.6），
                                      -- 随 INSERT 写入后永不 UPDATE，编剧复核修正=director.grade_revise 追加事件
  schema_version SMALLINT NOT NULL DEFAULT 1,
  PRIMARY KEY (sim_time, seq)
) PARTITION BY RANGE (sim_time);
CREATE INDEX events_tick_idx ON events (tick); CREATE INDEX events_type_time ON events (type, sim_time DESC);
CREATE INDEX events_actors_gin ON events USING GIN (actors);
CREATE INDEX events_arc_idx ON events (arc_id) WHERE arc_id IS NOT NULL;
CREATE INDEX events_loc_time ON events (location_id, sim_time DESC);
-- events_seq_uidx 见文件尾 partman 段（D12-a：模板表唯一索引 + 逐分区补建；04 §5.2 字面句物理不可执行）
-- append-only：REVOKE UPDATE,DELETE + 触发器 RAISE EXCEPTION 双保险（见文件尾）
-- 出站白名单列（§1.3/05 §3.1）：seq,tick,sim_time,wall_time,type,source,trigger,arc_id,
--   location_id,actors,rng_seed,payload(键白名单剥除 text_raw 后),visibility,ui,schema_version

CREATE TABLE agents (
  id TEXT PRIMARY KEY CHECK (id ~ '^A(0[1-9]|[1-3][0-9]|40)$'),  -- 'A01'~'A40'，全链路统一（§5.1）
  name TEXT NOT NULL, gender TEXT NOT NULL, age INT NOT NULL,
  room_no TEXT, department TEXT, job_title TEXT,   -- 租客 24 / 公司员工 16
  cognition_tier TEXT NOT NULL DEFAULT 'background'
    CHECK (cognition_tier IN ('star','secondary','background')),
  persona JSONB NOT NULL,                       -- 人设卡全量（Big Five/经历/视觉符号）
  needs JSONB NOT NULL,                         -- 缓存列：六需求 0~100，事实源 = 初始值 + Σ state.needs_delta（§6.5）
  mood JSONB,                                   -- 缓存列：同上，可由事件流重建（§6.5）
  balance_cents BIGINT NOT NULL DEFAULT 0,
  position TEXT NOT NULL, holdings JSONB NOT NULL DEFAULT '{}',  -- 地点节点 / 持仓
  next_due_sim TIMESTAMPTZ,                     -- 调度器下次兜底时间
  consecutive_over INT NOT NULL DEFAULT 0, consecutive_under INT NOT NULL DEFAULT 0,  -- 迟滞计数（只管路径一，§4.2）
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE world_state (  -- 唯一事实源 KV：时钟锚点/股价/公告/暂停态
  key TEXT PRIMARY KEY, value JSONB NOT NULL,
  updated_tick BIGINT NOT NULL, updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE memories (
  id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  agent_id TEXT NOT NULL REFERENCES agents(id), sim_time TIMESTAMPTZ NOT NULL,
  kind TEXT NOT NULL CHECK (kind IN ('event','projection','reflection','summary','plan')),
  content TEXT NOT NULL,               -- 内部通道原文，永不出站（§11.2）
  content_display TEXT,                -- 展示通道文本（过安全管线后写入；同步投影只出此列，05 §3.2）
  importance SMALLINT NOT NULL CHECK (importance BETWEEN 1 AND 10),
  embedding vector(1024),              -- bge-m3 1024 维，永不出站
  source_event_seq BIGINT,             -- 双人事件单源：双方记忆指向同一 seq
  is_witness BOOLEAN NOT NULL DEFAULT FALSE,  -- 目击投影=true（§6.3 写入；06 §2，副本侧由 05 memory_projection 直传）
  archived BOOLEAN NOT NULL DEFAULT FALSE, created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX mem_agent_time ON memories (agent_id, sim_time DESC) WHERE NOT archived;
CREATE INDEX mem_agent_imp ON memories (agent_id, importance DESC, sim_time DESC) WHERE NOT archived;
CREATE INDEX mem_src_event ON memories (source_event_seq);
CREATE INDEX mem_embed_hnsw ON memories USING hnsw (embedding vector_cosine_ops)
  WITH (m = 16, ef_construction = 64);  -- pgvector 0.8；查询侧 ef_search=40

CREATE TABLE relations (  -- 有序对 (A→B)；可由 relation.changed 事件流全量重建（§6.5），副本侧增量见 05 relation_change_log
  a_id TEXT NOT NULL REFERENCES agents(id), b_id TEXT NOT NULL REFERENCES agents(id),
  affinity SMALLINT NOT NULL DEFAULT 0 CHECK (affinity BETWEEN -100 AND 100),
  tension SMALLINT NOT NULL DEFAULT 0,
  labels TEXT[] NOT NULL DEFAULT '{}', one_line TEXT,
  last_event_seq BIGINT, updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (a_id, b_id), CHECK (a_id <> b_id)
);

CREATE TABLE debts (  -- 债务边：borrow_money 生成 / repay_money 核销 / 逾期日结算（§6.2；A4 弧线与 G-MON-03 数据源）
  id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  a_id TEXT NOT NULL REFERENCES agents(id),   -- 债权人（债主）；方向口径 = round2 §A.18（01 文档 §6 D13）
  b_id TEXT NOT NULL REFERENCES agents(id),   -- 债务人（欠款人）
  amount_cents BIGINT NOT NULL CHECK (amount_cents > 0),
  due_sim TIMESTAMPTZ NOT NULL,               -- 还款期限（模拟时间，默认借款 +14 模拟日，01 §4）
  repaid_cents BIGINT NOT NULL DEFAULT 0 CHECK (repaid_cents >= 0),
  created_tick BIGINT NOT NULL,
  CHECK (repaid_cents <= amount_cents), CHECK (a_id <> b_id)
);
CREATE INDEX debts_open_idx ON debts (b_id, due_sim) WHERE repaid_cents < amount_cents;

CREATE TABLE goals (
  id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  agent_id TEXT NOT NULL REFERENCES agents(id), sim_week INT NOT NULL, goal TEXT NOT NULL,
  blocked_count INT NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','done','abandoned')),
  frustration SMALLINT NOT NULL DEFAULT 0   -- 长期受阻→挫败→换策略/迁怒/放弃
);

CREATE TABLE intent_cooldown (  -- 意图冷却（按模拟时间）
  agent_id TEXT NOT NULL, intent_key TEXT NOT NULL,   -- 如 invite:A07:dinner
  until_sim TIMESTAMPTZ NOT NULL, reason TEXT, PRIMARY KEY (agent_id, intent_key)
);

CREATE TABLE interventions (  -- 编剧干预审计（干预率 <15% KPI 的数据源）
  id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  sim_time TIMESTAMPTZ NOT NULL,
  level TEXT NOT NULL CHECK (level IN ('L0','L1','L2')),   -- L3 永禁
  arc_id TEXT, reason TEXT NOT NULL, payload JSONB NOT NULL,
  event_seq BIGINT NOT NULL            -- 对应 events.seq（trigger='director'）
);

CREATE TABLE llm_calls (  -- 全量记录：¥/模拟日 计量的唯一依据
  id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  wall_time TIMESTAMPTZ NOT NULL DEFAULT now(), sim_time TIMESTAMPTZ,
  task_type TEXT NOT NULL,   -- star_decision/dialogue/secondary/bgsummary/reflection/director/world_copy/safety/embed
  agent_id TEXT, provider TEXT NOT NULL, model TEXT NOT NULL,
  prompt_tokens INT NOT NULL, completion_tokens INT NOT NULL,
  cost_micro_cny BIGINT NOT NULL,      -- 微元，按 models.yaml 价格表算
  latency_ms INT NOT NULL, status TEXT NOT NULL,   -- ok/retry/failed/fallback
  fallback_from TEXT, request_id TEXT, prompt_hash TEXT   -- fallback_from=降级链来源 provider
);
-- D12-b：04 §5.2 字面 `date_trunc('day', sim_time)` 为 STABLE 不可入索引；AT TIME ZONE 变体 IMMUTABLE（语义等价：本地时区日界）
CREATE INDEX llm_calls_simday ON llm_calls (date_trunc('day', sim_time AT TIME ZONE 'Asia/Shanghai'));

CREATE TABLE sync_state (  -- 单行：出站复制位点（三条流各一位点，§1.3）
  id SMALLINT PRIMARY KEY DEFAULT 1 CHECK (id = 1),
  last_acked_seq BIGINT NOT NULL DEFAULT 0,            -- events 白名单流
  last_acked_memory_id BIGINT NOT NULL DEFAULT 0,      -- memories 投影流
  last_acked_snapshot_day DATE,                        -- world_state 日快照流
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- presentation_queue 表已取消（评审 P2-7）：逐句时间表并入 events.payload.lines[].at_offset_s，
-- 客户端倍率为唯一播放节拍权威（§6.4），不再维护服务端播放队列。

-- ---------------------------------------------------------------------------
-- append-only 双保险（04 §5.2）：触发器（对 owner 亦生效）+ REVOKE（应用角色）
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION events_append_only() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION 'events is append-only (04 §5.2)';
END
$$;
CREATE TRIGGER events_append_only_trg BEFORE UPDATE OR DELETE ON events
  FOR EACH ROW EXECUTE FUNCTION events_append_only();

-- ---------------------------------------------------------------------------
-- 应用角色 worldsim 授权（T-DB-01：内核读写需要）
-- ---------------------------------------------------------------------------
GRANT SELECT, INSERT ON events TO worldsim;
REVOKE UPDATE, DELETE ON events FROM worldsim;
GRANT SELECT, INSERT, UPDATE ON agents, world_state, relations, debts, goals, intent_cooldown TO worldsim;
GRANT SELECT, INSERT ON memories, llm_calls, interventions TO worldsim;
GRANT ALL ON sync_state TO worldsim;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO worldsim;  -- GENERATED ALWAYS AS IDENTITY 隐式序列（非 owner 插入需要）

-- ---------------------------------------------------------------------------
-- pg_partman 注册 events 周分区（T-DB-01：周界 premake=4 ≥4 周；新建分区自动继承索引/触发器/权限）
-- 签名 = 5.5 实测（D12-c）：create_parent(p_parent_table, p_control, p_interval, p_type, p_premake)
-- ---------------------------------------------------------------------------
SELECT partman.create_parent('public.events', 'sim_time', '1 week', 'range', p_premake := 4);

-- D12-a：events_seq_uidx 落法 = 模板表唯一索引（未来分区继承）+ 现存分区逐一补建
CREATE UNIQUE INDEX events_seq_uidx ON partman.template_public_events (seq);  -- 04 §5.2 索引名保留（\di 可见）
DO $$
DECLARE child text;
BEGIN
  FOR child IN
    SELECT c.relname FROM pg_inherits i JOIN pg_class c ON c.oid = i.inhrelid
    WHERE i.inhparent = 'public.events'::regclass
  LOOP
    EXECUTE format('CREATE UNIQUE INDEX %I ON public.%I (seq)', child || '_seq_uidx', child);
  END LOOP;
END
$$;

-- sync_state 单行初始化（工程补全，01 文档 §6 D10）
INSERT INTO sync_state (id) VALUES (1) ON CONFLICT DO NOTHING;
