-- 审计④ 已知违规样例：窗内灌入足量 trigger='director' 事件使干预率超线（必报红；阈值由审计侧读配置）
INSERT INTO events (tick, sim_time, type, source, trigger, visibility, payload)
SELECT 0, (SELECT (value->>'anchor_sim')::timestamptz FROM world_state WHERE key='clock.anchor'),
       'director.intervene', 'director', 'director', 'internal',
       '{"level":"L1","reason":"样例灌入"}'::jsonb
FROM generate_series(1, 10);
INSERT INTO events (tick, sim_time, type, source, trigger, visibility, payload)
SELECT 0, (SELECT (value->>'anchor_sim')::timestamptz FROM world_state WHERE key='clock.anchor'),
       'agent.think', 'agent:A01', 'autonomous', 'internal', '{"topic_hint":"x"}'::jsonb
FROM generate_series(1, 10);
