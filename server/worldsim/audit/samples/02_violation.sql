-- 审计② 已知违规样例：同一 agent 连续两次 move，第二次 from ≠ 第一次 to（瞬移，必报红）
INSERT INTO events (tick, sim_time, type, source, trigger, actors, location_id, visibility, payload)
SELECT 0, (SELECT (value->>'anchor_sim')::timestamptz FROM world_state WHERE key='clock.anchor'),
       'agent.move', 'agent:A03', 'autonomous', '{A03}', 'apt.kitchen', 'public',
       '{"from":"apt.L5.503","to":"apt.kitchen","sim_cost_min":5}'::jsonb;
INSERT INTO events (tick, sim_time, type, source, trigger, actors, location_id, visibility, payload)
SELECT 0, (SELECT (value->>'anchor_sim')::timestamptz FROM world_state WHERE key='clock.anchor') + interval '5 minutes',
       'agent.move', 'agent:A03', 'autonomous', '{A03}', 'corp.ops', 'public',
       '{"from":"apt.roof","to":"corp.ops","sim_cost_min":20}'::jsonb;  -- from=apt.roof ≠ 前次 to=apt.kitchen
