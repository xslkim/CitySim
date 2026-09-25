-- ============================================================================
-- WorldSim 副本库 schema · worldsim_replica（07 T-SYN-06，M6）
-- 唯一 schema 权威 = docs/design/05-云端物化层设计.md §3.1~§3.8 + §4.1：本文件逐字照抄 05 SQL 块，
-- 不改列名不改类型。七张契约表（events/memory_projection/relation_change_log/relation_daily/
-- health_daily/world_state_snapshot/ripple_edge）+ event_grade_view（物化表）+ derived_task（摄入队列）。
-- 副本 events 不加 append-only 触发器/REVOKE（当日重灌需 DELETE，04 §9.2；偏差 07 D9）。
-- 管道级脱敏：本 schema 物理无 text_raw/记忆原文/embedding 载体（05 §1 原则 2）。
-- 执行：bash server/scripts/replica_init.sh（建库/角色/授权唯一入口；01 db_init.sh 不预留副本，R1 §A.1）
-- digest_log（07 D5 内部表）归 T-SYN-09 独立增量文件 ddl/replica_digest_log.sql，不在本文件。
-- ============================================================================

-- ---------------------------------------------------------------------------
-- 05 §3.1 events（白名单列同步；seq PK 即幂等位点，04 §9.1）
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS events (
  seq BIGINT PRIMARY KEY,                 -- 与主库同值（幂等与重灌键）
  tick BIGINT NOT NULL,
  sim_time TIMESTAMPTZ NOT NULL,
  wall_time TIMESTAMPTZ NOT NULL,
  type TEXT NOT NULL,                     -- <domain>.<object>[.<verb>]，注册表见 06
  source TEXT NOT NULL,
  trigger TEXT NOT NULL,                  -- autonomous|world|director|gift|vote|system（全枚举透传）
  arc_id TEXT, location_id TEXT,
  actors TEXT[] NOT NULL DEFAULT '{}',    -- 'A01'~'A40'
  rng_seed BIGINT,
  payload JSONB NOT NULL,                 -- 同步规范形（键白名单后；无 text_raw）
  visibility TEXT NOT NULL,
  ui JSONB,                               -- ui.grade 等（04 §6.6）；grade 仅为自动打分初值——复核修订走
                                          -- director.grade_revise 追加事件，本表行永不 UPDATE（append-only），
                                          -- 最新 grade 以 event_grade_view 为准（§3.8）
  schema_version SMALLINT NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS events_simday_idx ON events (((sim_time AT TIME ZONE 'Asia/Shanghai')::date));  -- 重灌/digest 分组
CREATE INDEX IF NOT EXISTS events_type_time ON events (type, sim_time DESC);
CREATE INDEX IF NOT EXISTS events_actors_gin ON events USING GIN (actors);
CREATE INDEX IF NOT EXISTS events_loc_time ON events (location_id, sim_time DESC);
CREATE INDEX IF NOT EXISTS events_caused_by ON events ((payload->>'caused_by'))   -- 涟漪后续链；值为裸 seq 数字字符串（'1089'），可直接 ::bigint
  WHERE payload ? 'caused_by';

-- ---------------------------------------------------------------------------
-- 05 §3.2 memory_projection（记忆展示通道投影；只出 content_display，行不可变）
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS memory_projection (
  memory_id BIGINT PRIMARY KEY,           -- 主库 memories.id
  agent_id TEXT NOT NULL,                 -- 'A01'~'A40'
  sim_time TIMESTAMPTZ NOT NULL,
  kind TEXT NOT NULL,                     -- event|projection|reflection|summary|plan
  content_display TEXT NOT NULL,          -- 仅展示通道文本
  importance SMALLINT NOT NULL,
  source_event_seq BIGINT,                -- 主库 memories.source_event_seq（= events.seq；06 §2 定名）
  is_witness BOOLEAN NOT NULL DEFAULT FALSE  -- 目击投影=true（04 §6.3 写入，直传）
);
CREATE INDEX IF NOT EXISTS memproj_agent_time ON memory_projection (agent_id, sim_time DESC);
CREATE INDEX IF NOT EXISTS memproj_src_event ON memory_projection (source_event_seq);       -- 涟漪第 1 段
CREATE INDEX IF NOT EXISTS memproj_reflection ON memory_projection (agent_id, sim_time DESC)
  WHERE kind = 'reflection';                                               -- 角色页"最新反思"

-- ---------------------------------------------------------------------------
-- 05 §3.3 relation_change_log（关系变更流水，由 relation.changed 事件投影）
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS relation_change_log (
  id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  event_seq BIGINT NOT NULL,              -- relation.changed 事件 seq（BIGINT 值 = events.seq；'e<seq>' 仅展示形态）
  a_id TEXT NOT NULL, b_id TEXT NOT NULL, -- 有序对 A→B
  delta_affinity SMALLINT NOT NULL,
  delta_tension SMALLINT NOT NULL,
  labels_added JSONB,                     -- 源 = relation.changed payload.changes[].labels_added（04 §6.5），标签字符串数组
  labels_removed JSONB,                   -- 源 = relation.changed payload.changes[].labels_removed；/relations 演化页边标签变更标注用
  sim_time TIMESTAMPTZ NOT NULL,
  sim_day DATE NOT NULL,
  UNIQUE (event_seq, a_id, b_id)
);
CREATE INDEX IF NOT EXISTS rcl_pair_time ON relation_change_log (a_id, b_id, sim_time);
CREATE INDEX IF NOT EXISTS rcl_event ON relation_change_log (event_seq);
CREATE INDEX IF NOT EXISTS rcl_simday ON relation_change_log (sim_day);          -- 重灌单位

-- ---------------------------------------------------------------------------
-- 05 §3.4 relation_daily（每日关系矩阵快照；递推口径见 05 §3.4 末段）
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS relation_daily (
  sim_day DATE NOT NULL,
  a_id TEXT NOT NULL, b_id TEXT NOT NULL,
  affinity SMALLINT NOT NULL,
  tension SMALLINT NOT NULL,
  labels TEXT[] NOT NULL DEFAULT '{}',
  PRIMARY KEY (sim_day, a_id, b_id)
);

-- ---------------------------------------------------------------------------
-- 05 §3.5 health_daily（五指标 + 结构两指标 + 干预率 + 成本日聚合；阈值不存本表）
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS health_daily (
  sim_day DATE PRIMARY KEY,
  a_grade_gap_days NUMERIC,        -- A 级事件间隔（grade 取 event_grade_view 最新值，§3.8）
  type_entropy NUMERIC,            -- 事件类型熵（bit）
  gini NUMERIC,                    -- 出场基尼系数
  ngram_dup NUMERIC,               -- 对话 3-gram 重复度（0~1；判定阈见 01 §9）
  relation_week_change NUMERIC,    -- 关系图周变化率（滚动 7 模拟日窗口，0~1）
  high_tension_ratio NUMERIC,      -- 高张力边占比 = 全楼 tension>60 边 / 活跃边总数（口径 01 §9，每日结算）
  active_conflict_edges INT,       -- 活跃冲突边数 = tension≥30 或近 7 模拟日发生过 argue/gossip/refuse/抢功/爽约的有序对数（口径 01 §9/§11.2）
  intervention_rate NUMERIC,       -- trigger='director' 占比（红线 15%，源方案 §4.8）
  cost_micro_cny BIGINT,           -- ¥/模拟日（微元；本机 audit 日结经快照流送达）
  computed_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------------
-- 05 §3.6 world_state_snapshot（每模拟日全量状态；state 字段白名单见 05 §3.6）
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS world_state_snapshot (
  sim_day DATE PRIMARY KEY,
  state JSONB NOT NULL,            -- 字段白名单见 05 §3.6
  digest TEXT NOT NULL,            -- 'sha256:…'，本机生成，云端复核
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------------
-- 05 §3.7 ripple_edge（八卦传播边 + 失真度离线预计算；hop 上限 4 手，01 §4.1）
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ripple_edge (
  id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  root_event_seq BIGINT NOT NULL,   -- 链根：被转述链的源事件 seq（涟漪页查询键）
  dst_event_seq BIGINT NOT NULL,    -- 本手 gossip 事件 seq
  src_event_seq BIGINT NOT NULL,    -- 上一手事件 seq（hop=1 时 = root_event_seq；重算/调试用）
  hop SMALLINT NOT NULL CHECK (hop BETWEEN 1 AND 4),   -- 深度上限 4 手（01 §4.1）
  teller_id TEXT NOT NULL,          -- 转述者 agent id（'A01'~'A40'）
  listener_id TEXT NOT NULL,        -- 听者 agent id
  distortion NUMERIC(4,3),          -- 0~1：normalized_levenshtein（05 §3.7 V1 口径）
  sim_time TIMESTAMPTZ NOT NULL,
  sim_day DATE NOT NULL,
  UNIQUE (root_event_seq, dst_event_seq)
);
CREATE INDEX IF NOT EXISTS ripple_root ON ripple_edge (root_event_seq, hop, sim_time);
CREATE INDEX IF NOT EXISTS ripple_simday ON ripple_edge (sim_day);

-- ---------------------------------------------------------------------------
-- 05 §3.8 event_grade_view（物化表：最新 grade 唯一读取处；events 行永不 UPDATE）
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS event_grade_view (
  seq BIGINT PRIMARY KEY,               -- 目标事件 seq（= events.seq）
  grade TEXT NOT NULL CHECK (grade IN ('A','B','C')),  -- 最新 grade（含复核修订）
  revised_by_seq BIGINT,                -- 最近一次 director.grade_revise 事件 seq；NULL = 未经复核（初值）
  reason TEXT,                          -- 复核理由（对外口径）
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- 物化逻辑（05 §3.8）：INSERT ... SELECT seq, ui->>'grade' FROM events WHERE ui ? 'grade'
--   ON CONFLICT (seq) DO NOTHING;                       -- 基线初值
-- 每条 director.grade_revise 事件：
--   INSERT ... VALUES (target_seq, new_grade, revise_seq, reason)
--   ON CONFLICT (seq) DO UPDATE WHERE 传入 revise 事件 seq > 现有 revised_by_seq（或 revised_by_seq IS NULL）;

-- ---------------------------------------------------------------------------
-- 05 §4.1 derived_task（摄入派生队列表；不授权 obs_ro，05 §5）
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS derived_task (
  id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  task_type TEXT NOT NULL,            -- project_relation_change|compute_ripple_edge|
                                      -- materialize_event_grade|refresh_relation_daily|refresh_health_daily
  dedupe_key TEXT NOT NULL UNIQUE,    -- 如 'relchg:e12345' / 'ripple:e12346' / 'grade:e12347' / 'relation_daily:2026-10-12'
  sim_day DATE,
  status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','running','done','failed')),
  attempts INT NOT NULL DEFAULT 0,
  run_at TIMESTAMPTZ, done_at TIMESTAMPTZ, last_error TEXT
);
CREATE INDEX IF NOT EXISTS derived_task_pending ON derived_task (id) WHERE status = 'pending';
