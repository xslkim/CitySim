-- ============================================================================
-- WorldSim 主库白名单视图 · M0 最小集（00 §1 A5/A12；05 §2 脱敏口径主库视图实现）
-- 对象：obs schema + obs.payload_key_whitelist（种子 = gen_event_types.py 派生物，单源生成链 00 §1 A11）
--      + obs.filter_payload()（全项目唯一过滤函数）+ obs.events / obs.memory_projection 视图 + obs_ro 授权
-- 消费：M4 观察端 API 直连（M6 切副本 DSN 零代码变更）；M4 衍生层归 ddl/obs_derived_v1.sql（05 T-WEB-01）
-- 执行：schema_v1 之上 `psql "$WSIM_PG_DSN" -v ON_ERROR_STOP=1 -f server/ddl/obs_views_v1.sql`
-- 口径注记（01 文档 §6 D5）：05 §2.1 "block→internal 整行不出站或仅元数据" 的精细区分属 M6 sync 层职责，
--      本视图统一按 "internal 不放行展示文本"（conditional 键仅 visibility='public' 放行）执行。
-- ============================================================================

CREATE SCHEMA obs;

-- 键白名单（唯一依据 = 06 §1.2 "payload 键标注"列；种子由 scripts/gen_event_types.py 派生，禁第二份手维护镜像）
CREATE TABLE obs.payload_key_whitelist (
  event_type TEXT NOT NULL,
  key TEXT NOT NULL,
  policy TEXT NOT NULL CHECK (policy IN ('public','conditional')),  -- public=放行；conditional=仅 visibility='public' 放行
  PRIMARY KEY (event_type, key)
);

\ir obs_whitelist_seed.sql

-- 过滤函数（全项目唯一，00 §1 A12）：仅保留该 type 登记 public 的键；conditional 键仅 public 事件放行；
-- 未登记键一律剥除（06 §1.2 默认剥除原则；text_raw 永不登记 → 永不出站，00 §4 红线 7）
CREATE OR REPLACE FUNCTION obs.filter_payload(p_type TEXT, p_visibility TEXT, p_payload JSONB)
RETURNS JSONB
LANGUAGE sql STABLE
AS $$
  SELECT COALESCE(jsonb_object_agg(e.key, e.value), '{}'::jsonb)
  FROM jsonb_each(COALESCE(p_payload, '{}'::jsonb)) AS e(key, value)
  WHERE EXISTS (
    SELECT 1 FROM obs.payload_key_whitelist w
    WHERE w.event_type = p_type
      AND w.key = e.key
      AND (w.policy = 'public' OR (w.policy = 'conditional' AND p_visibility = 'public'))
  );
$$;

-- 15 白名单列与 05 §2.1/04 §5.2 出站列逐字一致（05 §3.1 列形）
CREATE VIEW obs.events AS
SELECT seq, tick, sim_time, wall_time, type, source, trigger, arc_id, location_id, actors, rng_seed,
       obs.filter_payload(type, visibility, payload) AS payload,
       visibility, ui, schema_version
FROM events;

-- 记忆展示通道投影（05 §3.2 列形）：只出 content_display；"已出行"口径 = content_display 非空（04 §11 写入时已过滤）
CREATE VIEW obs.memory_projection AS
SELECT id AS memory_id, agent_id, sim_time, kind, content_display, importance, source_event_seq, is_witness
FROM memories
WHERE content_display IS NOT NULL;

-- obs_ro 只读账号（05 §5）：仅 obs schema 两视图；对 public schema 零授权（默认即无，不显式 REVOKE）
GRANT USAGE ON SCHEMA obs TO obs_ro;
GRANT SELECT ON obs.events, obs.memory_projection TO obs_ro;
