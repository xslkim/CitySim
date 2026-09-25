-- 审计⑥ 数据源查询（缓存对账的重算输入；重算/比对逻辑归 audit/daily.py，04 §6.5 口径）
-- 参数：$1 = 被抽 agent id（TEXT）
-- WITH ORDINALITY：同事件内多 changes 的施加序 = 数组序（M3 实测回归：无序 join 会乱序，
-- 重放/对账必须保序，04 §6.5「按 seq 序逐条施加」延伸到事件内数组序）
SELECT e.seq, c AS change FROM events e
CROSS JOIN LATERAL jsonb_array_elements(e.payload->'changes') WITH ORDINALITY AS t(c, ord)
WHERE e.type = 'state.needs_delta' AND t.c->>'agent_id' = $1
ORDER BY e.seq, t.ord;
