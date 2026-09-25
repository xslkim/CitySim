-- 审计③ 已知违规样例：同一对话被错误落成两条 dialogue.chat，参与者 A01 的记忆同时指向两源（必报红）
WITH anchor AS (SELECT (value->>'anchor_sim')::timestamptz AS t FROM world_state WHERE key='clock.anchor'),
ins AS (
  INSERT INTO events (tick, sim_time, type, source, trigger, actors, visibility, payload)
  SELECT 0, anchor.t, 'dialogue.chat', 'agent:A01', 'autonomous', '{A01,A02}', 'public',
         '{"participants":["A01","A02"],"mode":"small","topic_ids":[],"lines":[],"witnesses":[]}'::jsonb
  FROM anchor RETURNING seq)
SELECT seq FROM ins;
-- 第二条同对话（重复落库的错误形态）
INSERT INTO events (tick, sim_time, type, source, trigger, actors, visibility, payload)
SELECT 0, anchor.t + interval '1 minutes', 'dialogue.chat', 'agent:A01', 'autonomous', '{A01,A02}', 'public',
       '{"participants":["A01","A02"],"mode":"small","topic_ids":[],"lines":[],"witnesses":[]}'::jsonb
FROM (SELECT (value->>'anchor_sim')::timestamptz AS t FROM world_state WHERE key='clock.anchor') anchor;
-- A01 的两条投影记忆分别指向两个 seq（同事件双源）
INSERT INTO memories (agent_id, sim_time, kind, content, importance, source_event_seq)
SELECT 'A01', (SELECT (value->>'anchor_sim')::timestamptz FROM world_state WHERE key='clock.anchor'),
       'event', '投影记忆甲', 5, e.seq
FROM events e WHERE e.type='dialogue.chat' ORDER BY e.seq DESC LIMIT 1;
INSERT INTO memories (agent_id, sim_time, kind, content, importance, source_event_seq)
SELECT 'A01', (SELECT (value->>'anchor_sim')::timestamptz FROM world_state WHERE key='clock.anchor'),
       'event', '投影记忆乙', 5, e.seq
FROM events e WHERE e.type='dialogue.chat' ORDER BY e.seq ASC LIMIT 1;
