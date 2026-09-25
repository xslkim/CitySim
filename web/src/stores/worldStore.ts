/**
 * worldStore（03 §6.1）：当前世界投影（agents 位置/情绪/需求、经济、活跃对话、watermark_tick、副本延迟）。
 * T-WEB-08 骨架：snapshot 整体替换；T-WEB-10 补 state_diff 增量合并。
 */
import { create } from 'zustand';
import type { SnapshotData } from '../proto/snapshot';

interface WorldState {
  snapshot: SnapshotData | null;
  watermarkTick: number;
  setSnapshot: (s: SnapshotData) => void;
  setWatermarkTick: (t: number) => void;
}

export const useWorldStore = create<WorldState>((set) => ({
  snapshot: null,
  watermarkTick: 0,
  setSnapshot: (snapshot) => set({ snapshot }),
  setWatermarkTick: (watermarkTick) => set({ watermarkTick }),
}));
