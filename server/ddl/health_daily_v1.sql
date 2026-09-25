-- health_daily 主库侧唯一权威 DDL（08 T-AUD-08；00 §1 A12：独立增量文件，不动 schema_v1.sql）
-- 列形对齐 05 §3.5；口径唯一 = 04 §10.2（计算实现 worldsim/audit/metrics.py；阈值 config/health_thresholds.yaml）
CREATE TABLE IF NOT EXISTS health_daily (
  sim_day DATE PRIMARY KEY,               -- 模拟日
  a_grade_gap_days DOUBLE PRECISION,      -- A 级事件间隔（有效 grade 口径，04 §6.6）
  type_entropy DOUBLE PRECISION,          -- 事件类型熵（bit，7 日均值）
  gini DOUBLE PRECISION,                  -- 出场基尼系数（每模拟周）
  ngram_dup DOUBLE PRECISION,             -- 对话 3-gram 重复度（7 日窗）
  relation_week_change DOUBLE PRECISION,  -- 关系图周变化率
  high_tension_ratio DOUBLE PRECISION,    -- 高张力边占比（tension>60 / 活跃边）
  active_conflict_edges INT,              -- 活跃冲突边数（01 §9/§11.2 口径）
  stars_without_conflict INT,             -- 无活跃冲突边的明星数（01 §9 每名明星 ≥1 条）
  intervention_rate DOUBLE PRECISION,     -- 干预率（7 日滑窗，04 §10.1 ④）
  cost_micro_cny BIGINT,                  -- 日结成本（微元；¥/模拟日 04 §8.3）
  computed_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
