/**
 * timelineStore（03 §6.1/§6.2/§6.3）：事件环形缓冲（容量 3,000，溢出丢最旧段）、
 * 过滤条件态、live/历史模式；写入方 = WS event 帧 + REST 历史页。
 */
import { create } from 'zustand';
import type { ObsEvent } from '../proto/event';
import { insertEventsBySeq, truncateTimelineForSnapshot } from './merge';

export const TIMELINE_CAPACITY = 3000; // 03 §6.3（实测回填复核 05 §5，本值不改只登记建议）

export interface TimelineFilter {
  types: string[];
  actors: string[];
  locations: string[];
  triggers: string[];
  gradeAOnly: boolean;
  q: string;
}

interface TimelineState {
  events: ObsEvent[];       // seq 升序环形缓冲
  latestSeq: number;
  latestTick: number;
  live: boolean;            // live / 历史模式（03 §3.2 倒带）
  filter: TimelineFilter;
  append: (evs: ObsEvent[]) => void;
  truncateForSnapshot: (snapshotTick: number) => void;
  setLive: (live: boolean) => void;
  setFilter: (f: Partial<TimelineFilter>) => void;
}

export const EMPTY_FILTER: TimelineFilter = {
  types: [], actors: [], locations: [], triggers: [], gradeAOnly: false, q: '',
};

export const useTimelineStore = create<TimelineState>((set) => ({
  events: [],
  latestSeq: 0,
  latestTick: 0,
  live: true,
  filter: EMPTY_FILTER,
  append: (evs) =>
    set((s) => {
      const events = insertEventsBySeq(s.events, evs, TIMELINE_CAPACITY);
      return {
        events,
        latestSeq: events.length ? events[events.length - 1].seq : s.latestSeq,
        latestTick: Math.max(s.latestTick, ...evs.map((e) => e.tick), 0),
      };
    }),
  truncateForSnapshot: (tick) =>
    set((s) => ({ events: truncateTimelineForSnapshot(s.events, tick) })),
  setLive: (live) => set({ live }),
  setFilter: (f) => set((s) => ({ filter: { ...s.filter, ...f } })),
}));
