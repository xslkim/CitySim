-- ============================================================================
-- digest_log（07 T-SYN-09；偏差 D5 工程补充表）：每日 digest 比对结果留痕，供 /v1/health 与审计日报。
-- 非契约表：**不授权 obs_ro**（授权面不变 = 清单制 8 项，评审 P2-4）。
-- ============================================================================
CREATE TABLE IF NOT EXISTS digest_log (
  id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  sim_day DATE NOT NULL,               -- 比对分组（date(sim_time) 本地时区）
  status TEXT NOT NULL CHECK (status IN ('ok','mismatch','reprojected','skipped_window')),
  detail JSONB,                        -- 两端 count/sum/digest 对照与 mismatch 字段
  checked_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS digest_log_day ON digest_log (sim_day, checked_at DESC);
GRANT SELECT, INSERT ON digest_log TO worldsim_ingest;
