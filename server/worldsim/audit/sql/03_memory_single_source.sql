-- 审计③ 双人记忆同源（04 §10.1 ③ 评审二轮 P2-2 修复版：同 agent 同事件双投影比对）
-- 无参数
SELECT e.seq, m.agent_id
FROM events e
JOIN memories m ON m.agent_id = ANY(e.actors)
JOIN events se ON se.seq = m.source_event_seq AND se.type = 'dialogue.chat'
WHERE e.type = 'dialogue.chat'
  AND m.sim_time BETWEEN e.sim_time - interval '1 hour' AND e.sim_time + interval '1 hour'
GROUP BY e.seq, m.agent_id
HAVING COUNT(DISTINCT m.source_event_seq) > 1;
