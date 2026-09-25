-- 审计② 不在两地/瞬移（04 §10.1 ② 逐字：相邻两次 agent.move，后一次 payload.from 须等于前一次 location_id）
-- 无参数
WITH mv AS (
  SELECT e.seq, u.agent_id, e.location_id AS to_loc, e.payload->>'from' AS from_loc,
         LAG(e.location_id) OVER (PARTITION BY u.agent_id ORDER BY e.sim_time, e.seq) AS prev_to
  FROM events e CROSS JOIN LATERAL unnest(e.actors) AS u(agent_id)
  WHERE e.type = 'agent.move')
SELECT agent_id, seq FROM mv
WHERE prev_to IS NOT NULL AND from_loc IS DISTINCT FROM prev_to;
