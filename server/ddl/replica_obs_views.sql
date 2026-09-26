-- ============================================================================
-- 副本库 obs 兼容视图层（07 T-SYN-10；00 §1 A5 零代码变更前提的落地桥，07 D8）
-- 背景：obs-api SQL 一律 schema 限定 `obs.<对象>`（M4 主库白名单视图层）；副本契约表在 public。
-- 本文件在副本库建 obs schema 同名透传视图（payload 已是同步规范形，无需再过 filter_payload），
-- 使 T-SYN-10 切换 = `.env` 恰一行 DSN 值变更、代码零变更。
-- 授权：obs_ro USAGE obs + SELECT 八视图（与 05 §5 清单制一致；视图基础表已授权）。
-- ============================================================================

CREATE SCHEMA IF NOT EXISTS obs;

-- 05 §3.1 列形（副本 payload 出站前已剥除 internal 键，透传即白名单语义）
CREATE OR REPLACE VIEW obs.events AS
SELECT seq, tick, sim_time, wall_time, type, source, trigger, arc_id, location_id, actors,
       rng_seed, payload, visibility, ui, schema_version
FROM public.events;

CREATE OR REPLACE VIEW obs.memory_projection AS
SELECT memory_id, agent_id, sim_time, kind, content_display, importance, source_event_seq, is_witness
FROM public.memory_projection;

CREATE OR REPLACE VIEW obs.relation_change_log AS
SELECT id, event_seq, a_id, b_id, delta_affinity, delta_tension, labels_added, labels_removed,
       sim_time, sim_day
FROM public.relation_change_log;

CREATE OR REPLACE VIEW obs.relation_daily AS
SELECT sim_day, a_id, b_id, affinity, tension, labels
FROM public.relation_daily;

-- 05 §3.5 列集（主库 obs.health_daily 含 stars_without_conflict 列属主库侧增列，05 文档偏差表登记；
-- 副本列集 = 05 §3.5 逐字，obs-api 读取列为该集子集）
CREATE OR REPLACE VIEW obs.health_daily AS
SELECT sim_day, a_grade_gap_days, type_entropy, gini, ngram_dup, relation_week_change,
       high_tension_ratio, active_conflict_edges, intervention_rate, cost_micro_cny, computed_at
FROM public.health_daily;

CREATE OR REPLACE VIEW obs.world_state_snapshot AS
SELECT sim_day, state, digest, created_at
FROM public.world_state_snapshot;

CREATE OR REPLACE VIEW obs.ripple_edge AS
SELECT id, root_event_seq, dst_event_seq, src_event_seq, hop, teller_id, listener_id,
       distortion, sim_time, sim_day
FROM public.ripple_edge;

CREATE OR REPLACE VIEW obs.event_grade_view AS
SELECT seq, grade, revised_by_seq, reason, updated_at
FROM public.event_grade_view;

-- R1 #1 当日滚动通道：单行最新世界态（独立 latest，日界 world_state_snapshot 闭环不动）
CREATE OR REPLACE VIEW obs.world_state_latest AS
SELECT id, tick, sim_time, state, digest, updated_at
FROM public.world_state_latest;

GRANT USAGE ON SCHEMA obs TO obs_ro;
GRANT SELECT ON obs.events, obs.memory_projection, obs.relation_change_log, obs.relation_daily,
  obs.health_daily, obs.world_state_snapshot, obs.ripple_edge, obs.event_grade_view,
  obs.world_state_latest TO obs_ro;
