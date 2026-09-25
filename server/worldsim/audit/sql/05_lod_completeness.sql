-- 审计⑤ LOD 记录完整（04 §10.1 ⑤；两段合一：a=tier 与最近升格事件不一致，b=升星后 1 模拟日内无追赶反思）
-- 参数：$1 sim_now（b 段窗口闭合判定）
-- (a) agents.cognition_tier 与最近一次 agent.promoted/agent.demoted 的 to_tier 不一致
WITH latest AS (
  SELECT DISTINCT ON (u.aid) u.aid, e.payload->>'to_tier' AS to_tier
  FROM events e CROSS JOIN LATERAL unnest(e.actors) AS u(aid)
  WHERE e.type IN ('agent.promoted', 'agent.demoted')
  ORDER BY u.aid, e.seq DESC)
SELECT 'tier_mismatch' AS kind, a.id AS agent_id, NULL::bigint AS event_seq
FROM agents a JOIN latest l ON l.aid = a.id
WHERE a.cognition_tier IS DISTINCT FROM l.to_tier
UNION ALL
-- (b) agent.promoted（to_tier='star'）后 1 模拟日内无反思（memories kind='reflection' 或 agent.reflection 事件，04 §7.2）
SELECT 'no_catchup_reflection' AS kind, u.aid, e.seq
FROM events e CROSS JOIN LATERAL unnest(e.actors) AS u(aid)
WHERE e.type = 'agent.promoted' AND e.payload->>'to_tier' = 'star'
  AND e.sim_time <= $1::timestamptz - interval '1 day'
  AND NOT EXISTS (
    SELECT 1 FROM memories m WHERE m.agent_id = u.aid AND m.kind = 'reflection'
      AND m.sim_time BETWEEN e.sim_time AND e.sim_time + interval '1 day')
  AND NOT EXISTS (
    SELECT 1 FROM events r WHERE r.type = 'agent.reflection' AND u.aid = ANY(r.actors)
      AND r.sim_time BETWEEN e.sim_time AND e.sim_time + interval '1 day');
