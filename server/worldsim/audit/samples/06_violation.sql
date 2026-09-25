-- 审计⑥ 已知违规样例：篡改全部 agent 的 needs 缓存列（被抽中者必在其中 → 必报红；不动余额不影响①）
UPDATE agents SET needs = jsonb_set(needs, '{mood}', '1');
