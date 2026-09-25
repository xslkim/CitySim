-- 审计⑤ 已知违规样例（两条均必报红）：
-- (a) 有升格事件但当前 tier 与事件不一致（事件说 star、当前 secondary）
-- (b) 升星事件后 1 模拟日内无追赶反思
INSERT INTO events (tick, sim_time, type, source, trigger, actors, visibility, payload)
SELECT 0, (SELECT (value->>'anchor_sim')::timestamptz FROM world_state WHERE key='clock.anchor') - interval '2 days',
       'agent.promoted', 'system', 'system', '{A04}', 'internal',
       '{"from_tier":"secondary","to_tier":"star"}'::jsonb;
UPDATE agents SET cognition_tier='secondary' WHERE id='A04';  -- (a) 事件 star ≠ 当前 secondary
INSERT INTO events (tick, sim_time, type, source, trigger, actors, visibility, payload)
SELECT 0, (SELECT (value->>'anchor_sim')::timestamptz FROM world_state WHERE key='clock.anchor') - interval '2 days',
       'agent.promoted', 'system', 'system', '{A05}', 'internal',
       '{"from_tier":"secondary","to_tier":"star"}'::jsonb;    -- (b) 无反思追赶
