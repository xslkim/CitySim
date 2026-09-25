-- ¥/模拟日聚合（03 T-LLM-07 交付物；04 §8.3 公式唯一口径）。
-- 用法：psql -h /tmp -d worldsim -f scripts/llm_cost.sql
-- 成本公式：cost_micro_cny = prompt_tokens×in_price + completion_tokens×out_price（单价 ¥/百万 tokens，models.yaml）。

-- ① 全量 ¥/模拟日（04 §8.3 字面公式）
SELECT SUM(cost_micro_cny)::float/1e6 / COUNT(DISTINCT date(sim_time)) AS cny_per_simday
FROM llm_calls;

-- ② 7 模拟日滚动平滑变体（04 §8.3"滚动 7 模拟日窗口平滑"；窗口 = 库内最新模拟日往前 7 天）
SELECT SUM(cost_micro_cny)::float/1e6 / COUNT(DISTINCT date(sim_time)) AS cny_per_simday_7d
FROM llm_calls
WHERE date(sim_time) > (SELECT max(date(sim_time)) FROM llm_calls) - interval '7 days';

-- ③ 分 provider/model/task_type 成本分布（fallback_from 留痕分析，04 §8.2"免费档实际扛了多少"）
SELECT provider, model, task_type, status,
       COUNT(*) AS calls,
       SUM(prompt_tokens) AS prompt_tokens,
       SUM(completion_tokens) AS completion_tokens,
       SUM(cost_micro_cny)::float/1e6 AS cost_cny,
       percentile_cont(0.95) WITHIN GROUP (ORDER BY latency_ms) AS p95_latency_ms,
       COUNT(*) FILTER (WHERE fallback_from IS NOT NULL) AS fallback_calls
FROM llm_calls
GROUP BY provider, model, task_type, status
ORDER BY cost_cny DESC NULLS LAST, calls DESC;
