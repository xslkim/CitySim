-- ============================================================================
-- 副本库账号授权（07 T-SYN-06；05 §5）。由 replica_init.sh 在建库跑 DDL 后执行（幂等）。
--   摄入写账号 worldsim_ingest：原始层 INSERT + 快照 UPDATE（覆盖）+ 重灌 DELETE + 派生表全权 + derived_task 全权
--   只读账号 obs_ro：仅 SELECT 于七表 + event_grade_view（清单制 8 项）；derived_task 不授权（05 §5）
-- ============================================================================

-- 摄入写账号
GRANT SELECT, INSERT, DELETE ON events TO worldsim_ingest;            -- DELETE 仅当日重灌（04 §9.2）
GRANT SELECT, INSERT, DELETE ON memory_projection TO worldsim_ingest;
GRANT SELECT, INSERT, UPDATE, DELETE ON world_state_snapshot TO worldsim_ingest;  -- 按 sim_day 覆盖
GRANT SELECT, INSERT, UPDATE, DELETE ON relation_change_log, relation_daily, health_daily,
  ripple_edge, event_grade_view TO worldsim_ingest;                   -- 派生层重算 DELETE+INSERT（05 §4.3）
GRANT SELECT, INSERT, UPDATE, DELETE ON derived_task TO worldsim_ingest;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO worldsim_ingest;     -- IDENTITY 隐式序列

-- 只读账号（清单制 8 项 = 七表 + event_grade_view；derived_task 不授权）
GRANT SELECT ON events, memory_projection, relation_change_log, relation_daily, health_daily,
  world_state_snapshot, ripple_edge, event_grade_view TO obs_ro;
