/**
 * timelineStore（03 §6.1）：事件环形缓冲（容量 3,000）/过滤条件/live-历史模式。
 * T-WEB-08 骨架：latestTick 供 LatencyBar；T-WEB-10 补齐合并规则（stores/merge.ts）。
 */
import { create } from 'zustand';
import type { ObsEvent } from '../proto/event';

export const TIMELINE_CAPACITY = 3000; // 环形缓冲容量（03 §6.3；实测回填复核 05 §5）

interface TimelineState {
  events: ObsEvent[];
  latestTick: number;
  live: boolean;
  append: (evs: ObsEvent[]) => void;
}

export const useTimelineStore = create<TimelineState>((set) => ({
  events: [],
  latestTick: 0,
  live: true,
  append: (evs) =>
    set((s) => {
      const merged = [...s.events, ...evs];
      return {
        events: merged.slice(-TIMELINE_CAPACITY),
        latestTick: Math.max(s.latestTick, ...evs.map((e) => e.tick), 0),
      };
    }),
}));
