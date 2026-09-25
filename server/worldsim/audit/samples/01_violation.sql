-- 审计① 已知违规样例：插入带 amount_cents 的结算事件但不改 agents.balance_cents（必报红）
INSERT INTO events (tick, sim_time, type, source, trigger, actors, visibility, payload)
SELECT 0, (SELECT (value->>'anchor_sim')::timestamptz FROM world_state WHERE key='clock.anchor'),
       'social.give_gift', 'agent:A01', 'autonomous', '{A01,A02}', 'public',
       '{"from":"A01","to":"A02","tier":1,"amount_cents":-8800}'::jsonb;
