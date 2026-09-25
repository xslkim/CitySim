-- 审计⑥ 数据源查询（缓存对账的重算输入；重算/比对逻辑归 audit/daily.py，04 §6.5 口径）
-- 参数：$1 = 被抽 agent id（TEXT）
SELECT e.seq, c AS change FROM events e
CROSS JOIN LATERAL jsonb_array_elements(e.payload->'changes') AS c
WHERE e.type = 'state.needs_delta' AND c->>'agent_id' = $1
ORDER BY e.seq;
