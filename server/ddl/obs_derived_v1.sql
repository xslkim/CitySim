-- ============================================================================
-- WorldSim 主库 obs 派生视图层 · M4 增量（05 T-WEB-01；00 §1 A5/A12 两层文件划界）
-- 前置：schema_v1.sql + obs_views_v1.sql（M0 最小集）+ health_daily_v1.sql（08 T-AUD-08）已执行
-- 对象（与 05 §3.1~§3.8 逐字同名同列，M6 切副本 DSN 零代码变更）：
--   VIEW  obs.relation_change_log   relation.changed 事件 payload.changes 展开（05 §3.3 列形）
--   VIEW  obs.event_grade_view      最新生效 grade = ui.grade 初值 ⊕ 最新 director.grade_revise（05 §3.8 视图等价）
--   VIEW  obs.health_daily          主库 health_daily 表透传（唯一权威实现 = 08 metrics.py，本层不自算）
--   TABLE obs.world_state_snapshot  每模拟日白名单快照（05 §3.6；由 scripts/obs_refresh.py 从内核快照文件投影入库）
--   TABLE obs.relation_daily        每日关系矩阵（05 §3.4 递推口径；obs_refresh.py 重算）
--   TABLE obs.ripple_edge           八卦传播边 + 失真度（05 §3.7；obs_refresh.py 重算）
-- 幂等：CREATE OR REPLACE VIEW / CREATE TABLE|INDEX IF NOT EXISTS / ON CONFLICT 种子，可重复执行。
-- 执行：psql "$WSIM_PG_DSN" -v ON_ERROR_STOP=1 -f server/ddl/obs_derived_v1.sql
-- ============================================================================

CREATE EXTENSION IF NOT EXISTS pg_trgm;          -- 03 §5.1 中文检索降级路径（05 文档 D8）
CREATE EXTENSION IF NOT EXISTS fuzzystrmatch;    -- ripple_edge.distortion 的 levenshtein（05 §3.7）

-- ---------------------------------------------------------------------------
-- obs.relation_change_log（05 §3.3 列形）：relation.changed 的 payload.changes 逐边展开
-- id 列为视图内稳定序号（05 §3.3 物化表为 IDENTITY；视图等价物以 row_number 充位，列名一致）
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW obs.relation_change_log AS
SELECT row_number() OVER (ORDER BY e.seq, c.a_id, c.b_id) AS id,
       e.seq AS event_seq,
       c.a_id, c.b_id,
       c.delta_affinity, c.delta_tension,
       c.labels_added, c.labels_removed,
       e.sim_time,
       (e.sim_time AT TIME ZONE 'Asia/Shanghai')::date AS sim_day  -- 模拟日按本地时区聚合（01 文档 §6 D4 同口径）
FROM obs.events e
CROSS JOIN LATERAL jsonb_to_recordset(e.payload->'changes') AS c(
  a_id TEXT, b_id TEXT, delta_affinity SMALLINT, delta_tension SMALLINT,
  labels_added JSONB, labels_removed JSONB
)
WHERE e.type = 'relation.changed';

-- ---------------------------------------------------------------------------
-- obs.event_grade_view（05 §3.8 物化逻辑的视图等价）：最新生效 grade 唯一读取处
--   基线 = events.ui->>'grade'（自动打分初值）；每次 director.grade_revise 以事件 seq 大者覆盖
--   （05 §3.8 幂等行"同 target_seq 多次修订按事件 seq 大者覆盖"的视图表达）
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW obs.event_grade_view AS
WITH revise AS (
  SELECT (payload->>'target_seq')::bigint AS target_seq,
         payload->>'new_grade' AS new_grade,
         seq AS revised_by_seq,
         payload->>'reason' AS reason,
         wall_time AS revised_at,
         row_number() OVER (PARTITION BY payload->>'target_seq' ORDER BY seq DESC) AS rn
  FROM obs.events
  WHERE type = 'director.grade_revise'
)
SELECT e.seq,
       COALESCE(r.new_grade, e.ui->>'grade') AS grade,
       r.revised_by_seq,
       r.reason,
       COALESCE(r.revised_at, e.wall_time) AS updated_at
FROM obs.events e
LEFT JOIN revise r ON r.target_seq = e.seq AND r.rn = 1
WHERE e.ui->>'grade' IN ('A', 'B', 'C');

-- ---------------------------------------------------------------------------
-- obs.health_daily：主库 health_daily 表透传 VIEW（七指标唯一权威 = 08 T-AUD-08 metrics.py；
-- 列形随主库表——含 01 §9 v1.1 stars_without_conflict 列，05 §3.5 列集为其子集，05 文档偏差表登记）
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW obs.health_daily AS
SELECT sim_day, a_grade_gap_days, type_entropy, gini, ngram_dup, relation_week_change,
       high_tension_ratio, active_conflict_edges, stars_without_conflict,
       intervention_rate, cost_micro_cny, computed_at
FROM health_daily;

-- ---------------------------------------------------------------------------
-- 三实体表（05 §3.6/§3.4/§3.7 DDL 逐字列形；写入方 = scripts/obs_refresh.py，按模拟日重算）
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS obs.world_state_snapshot (
  sim_day DATE PRIMARY KEY,
  state JSONB NOT NULL,            -- 字段白名单 = 05 §3.6（内核快照白名单子集同源）
  digest TEXT NOT NULL,            -- 'sha256:…'（canonical 唯一实现 = worldsim/snapshot/canonical.py）
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS obs.relation_daily (
  sim_day DATE NOT NULL,
  a_id TEXT NOT NULL, b_id TEXT NOT NULL,
  affinity SMALLINT NOT NULL,
  tension SMALLINT NOT NULL,
  labels TEXT[] NOT NULL DEFAULT '{}',
  PRIMARY KEY (sim_day, a_id, b_id)
);

CREATE TABLE IF NOT EXISTS obs.ripple_edge (
  id BIGINT GENERATED ALWAYS AS IDENTITY,
  root_event_seq BIGINT NOT NULL,
  dst_event_seq BIGINT NOT NULL,
  src_event_seq BIGINT NOT NULL,
  hop SMALLINT NOT NULL,           -- 深度上限见 01 §4.1（镜像值 config/relations.yaml gossip.hop_max）
  teller_id TEXT NOT NULL,
  listener_id TEXT NOT NULL,
  distortion NUMERIC(4,3),         -- normalized_levenshtein 0~1（归一化口径 05 文档 D3：分母 max(len)）
  sim_time TIMESTAMPTZ NOT NULL,
  sim_day DATE NOT NULL,
  PRIMARY KEY (id),
  UNIQUE (root_event_seq, dst_event_seq)
);
CREATE INDEX IF NOT EXISTS ripple_root ON obs.ripple_edge (root_event_seq, hop, sim_time);
CREATE INDEX IF NOT EXISTS ripple_simday ON obs.ripple_edge (sim_day);

-- ---------------------------------------------------------------------------
-- 主库 events 加性索引（obs 查询性能；不改冻结列形，纯增量）：
--   ① 中文检索降级路径 pg_trgm（03 §5.1）；② 涟漪第 4 段 caused_by 部分索引（05 §3.1 同形）
-- ---------------------------------------------------------------------------
CREATE INDEX IF NOT EXISTS events_text_display_trgm
  ON events USING gin ((payload->>'text_display') gin_trgm_ops);
CREATE INDEX IF NOT EXISTS events_caused_by_idx
  ON events ((payload->>'caused_by')) WHERE payload ? 'caused_by';

-- ---------------------------------------------------------------------------
-- obs_ro 授权（05 §5：SELECT 仅 obs schema，DDL/DML 权限为零；覆盖 M0/M4 两层全部对象）
-- ---------------------------------------------------------------------------
GRANT SELECT ON obs.relation_change_log, obs.event_grade_view, obs.health_daily TO obs_ro;
GRANT SELECT ON obs.world_state_snapshot, obs.relation_daily, obs.ripple_edge TO obs_ro;
-- M0 授权补丁（本层落地，不动 M0 文件）：obs.events 视图目标列调用 obs.filter_payload()
-- （SECURITY INVOKER 默认），obs_ro 实际取 payload 列时需可读键白名单表（内容为注册表公开信息，非敏感）
GRANT SELECT ON obs.payload_key_whitelist TO obs_ro;

-- obs_refresh.py（常驻重算脚本）写入账号 = 内核应用角色 worldsim：仅三实体表可写，视图/主库侧仍按其既有授权
-- （obs-api 只读账号 obs_ro 授权不变——"应用角色对 obs 只读"红线指观察端服务账号，05 §5）
GRANT USAGE ON SCHEMA obs TO worldsim;
GRANT SELECT ON obs.events, obs.memory_projection, obs.relation_change_log, obs.payload_key_whitelist TO worldsim;
GRANT SELECT, INSERT, UPDATE, DELETE ON obs.world_state_snapshot, obs.relation_daily, obs.ripple_edge TO worldsim;
