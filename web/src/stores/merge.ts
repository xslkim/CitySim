/**
 * 事件增量合并纯函数（03 §6.2 规则 1~4；00 §4 红线 2：seq 为唯一排序/去重依据）。
 * 测试策略（03 §0.2）：store 合并与协议解析的纯函数为测试主体。
 */
import type { ObsEvent } from '../proto/event';
import type { SnapshotData } from '../proto/snapshot';
import type { StateDiff } from '../proto/ws';

/** 规则 1：按 seq 升序插入 + 重复 seq 去重（append-only 幂等）；环形容量由调用方裁剪。 */
export function insertEventsBySeq(existing: ObsEvent[], incoming: ObsEvent[], capacity: number): ObsEvent[] {
  const bySeq = new Map<number, ObsEvent>();
  for (const e of existing) bySeq.set(e.seq, e);
  for (const e of incoming) bySeq.set(e.seq, e); // 重复 seq 幂等覆盖（append-only 同值）
  const merged = [...bySeq.values()].sort((a, b) => a.seq - b.seq);
  return merged.length > capacity ? merged.slice(merged.length - capacity) : merged;
}

/** 规则 2：state_diff 按 tick apply；diff.tick <= 当前 tick 直接丢弃（快照重建后的旧推送）。 */
export function applyStateDiffToAgents(
  agents: SnapshotData['agents'],
  currentTick: number,
  diff: StateDiff,
): { agents: SnapshotData['agents']; tick: number } | null {
  if (diff.tick <= currentTick) return null;
  const next = agents.map((a) => ({ ...a }));
  const byId = new Map(next.map((a) => [a.id, a]));
  for (const d of diff.agents) {
    const a = byId.get(d.id);
    if (!a) continue;
    if (d.location_id !== undefined) a.location_id = d.location_id;
    if (d.mood !== undefined) a.mood = d.mood;
    if (d.needs) a.needs = { ...(a.needs ?? {}), ...d.needs };
  }
  return { agents: next, tick: diff.tick };
}

/** 规则 3：快照重建 → 时间轴只保留 ≥快照 tick 的事件。 */
export function truncateTimelineForSnapshot(events: ObsEvent[], snapshotTick: number): ObsEvent[] {
  return events.filter((e) => e.tick >= snapshotTick);
}

/** 重连指数退避 1s→2s→…→30s 封顶，±20% 抖动（03 §5.2）。rng 可注入（测试确定性）。 */
export function backoffMs(attempt: number, rng: () => number = Math.random): number {
  const base = Math.min(30_000, 1000 * 2 ** Math.max(0, attempt));
  const jitter = 0.8 + rng() * 0.4; // ±20%
  return Math.round(base * jitter);
}
