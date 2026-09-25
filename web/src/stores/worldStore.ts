/**
 * worldStore（03 §6.1/§6.2.2）：当前世界投影——snapshot 整体替换 + state_diff 按 tick apply
 * （diff.tick <= 当前 tick 丢弃）；副本延迟 = watermark_tick − 最新事件 tick（03 §0.1）。
 */
import { create } from 'zustand';
import type { SnapshotData } from '../proto/snapshot';
import type { StateDiff } from '../proto/ws';
import { applyStateDiffToAgents } from './merge';

interface WorldState {
  snapshot: SnapshotData | null;
  tick: number;           // 当前世界 tick（snapshot.tick 或最近 apply 的 diff.tick）
  watermarkTick: number;
  setSnapshot: (s: SnapshotData) => void;
  applyDiff: (d: StateDiff) => void;
  setWatermarkTick: (t: number) => void;
}

export const useWorldStore = create<WorldState>((set, get) => ({
  snapshot: null,
  tick: 0,
  watermarkTick: 0,
  setSnapshot: (snapshot) => set({ snapshot, tick: snapshot.tick }),
  applyDiff: (d) => {
    const { snapshot, tick } = get();
    if (!snapshot) return;
    const r = applyStateDiffToAgents(snapshot.agents, tick, d);
    if (r) set({ snapshot: { ...snapshot, agents: r.agents }, tick: r.tick });
  },
  setWatermarkTick: (watermarkTick) => set({ watermarkTick }),
}));
