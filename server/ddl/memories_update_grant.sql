-- ============================================================================
-- T-MEM-03：memories 归档治理 UPDATE 授权（02 文档偏差表 D13 登记）
-- Schema v1 冻结口径下 memories 应用角色仅 GRANT SELECT, INSERT（04 §5.2）；
-- 膨胀治理（摘要合并/归档）需置 archived=true——只置位不删行（04 §7.3 可回溯口径），
-- 按列最小授权，不开整表 UPDATE。一次性执行：
--   psql "$WSIM_PG_DSN" -v ON_ERROR_STOP=1 -f server/ddl/memories_update_grant.sql
-- ============================================================================
GRANT UPDATE (archived) ON memories TO worldsim;
