-- 审计④ 干预率（04 §10.1 ④：滚动 7 模拟日 trigger='director' 占比；00 §4 红线 13 只看 trigger）
-- 参数：$1 sim_now / $2 cap（干预率上限，models.yaml thresholds.intervention_rate_cap，应用层传入）
-- 返回行即超线报红
SELECT COUNT(*) FILTER (WHERE trigger='director')::float / NULLIF(COUNT(*), 0) AS rate
FROM events WHERE sim_time > $1::timestamptz - interval '7 days'
HAVING COUNT(*) FILTER (WHERE trigger='director')::float / NULLIF(COUNT(*), 0) > $2;
