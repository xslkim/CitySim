-- 当日滚动世界状态通道（R1 迭代包 #1，独立 latest 通道；日界快照 world_state_snapshot 闭环不动）。
-- 单行表（id=1 恒等 upsert），随帧 digest 用 canonical 唯一实现复核（worldsim/ingest/batch.py）；
-- 与 world_state_snapshot（sim_day 键、日界定稿）并列，digest_check（T-SYN-09）只管事件流，本表不参与。
CREATE TABLE IF NOT EXISTS world_state_latest (
  id SMALLINT PRIMARY KEY DEFAULT 1 CHECK (id = 1),
  tick BIGINT NOT NULL,
  sim_time TIMESTAMPTZ NOT NULL,
  state JSONB NOT NULL,
  digest TEXT NOT NULL,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
GRANT SELECT ON world_state_latest TO obs_ro;
GRANT SELECT, INSERT, UPDATE ON world_state_latest TO worldsim_ingest;
