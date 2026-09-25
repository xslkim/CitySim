-- 审计① 余额守恒（04 §10.1 ①；06 §1.3：经济类型清单由 06 标记生成，禁 LIKE 前缀法）
-- 参数：$1 economy_types text[] / $2 initial_fund bigint
-- 违规口径：actual <> expected（返回行即报红）
SELECT (SELECT SUM(balance_cents) FROM agents) AS actual,
       (SELECT coalesce(SUM((payload->>'amount_cents')::bigint), 0) FROM events
         WHERE type = ANY($1::text[]))
       + $2 AS expected
WHERE (SELECT SUM(balance_cents) FROM agents)
  <> (SELECT coalesce(SUM((payload->>'amount_cents')::bigint), 0) FROM events
       WHERE type = ANY($1::text[])) + $2;
