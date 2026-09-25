/** T-WEB-10 store 合并与退避纯函数测试（03 §0.2 测试策略：纯函数为主体）。 */
import { beforeEach, describe, expect, it } from 'vitest';
import type { ObsEvent } from '../proto/event';
import { AGENT_DETAIL_LRU, useAgentsStore } from './agentsStore';
import {
  applyStateDiffToAgents,
  backoffMs,
  insertEventsBySeq,
  truncateTimelineForSnapshot,
} from './merge';
import { TIMELINE_CAPACITY, useTimelineStore } from './timelineStore';
import { useWorldStore } from './worldStore';

function ev(seq: number, tick = seq): ObsEvent {
  return {
    seq, tick, sim_time: '2026-10-12T19:38:11+08:00', type: 'dialogue.chat', source: 'agent:A01',
    trigger: 'autonomous', arc_id: null, ui: null, payload: {},
  };
}

describe('03 §6.2 合并规则', () => {
  it('test_out_of_order_insert_by_seq：乱序（resume 补推与 live 交错）按 seq 插入排序', () => {
    const merged = insertEventsBySeq([ev(1), ev(5)], [ev(3), ev(2)], 100);
    expect(merged.map((e) => e.seq)).toEqual([1, 2, 3, 5]);
  });

  it('test_dup_seq_dedup：重复 seq 去重（append-only 幂等）', () => {
    const merged = insertEventsBySeq([ev(1), ev(2)], [ev(2), ev(3)], 100);
    expect(merged.map((e) => e.seq)).toEqual([1, 2, 3]);
  });

  it('test_ring_buffer_evicts_oldest：3001 入 → 3000 容量', () => {
    const merged = insertEventsBySeq(
      Array.from({ length: TIMELINE_CAPACITY }, (_, i) => ev(i + 1)), [ev(TIMELINE_CAPACITY + 1)],
      TIMELINE_CAPACITY,
    );
    expect(merged.length).toBe(TIMELINE_CAPACITY);
    expect(merged[0].seq).toBe(2);
  });

  it('test_state_diff_stale_tick_dropped：diff.tick <= 当前 tick 丢弃（03 §6.2.2）', () => {
    const agents = [{ id: 'A01', name: 'x', lod: 'star' as const, location_id: 'a', activity: null,
      mood: 50, needs: { hunger: 50 } }];
    const stale = applyStateDiffToAgents(agents, 10, { tick: 10, agents: [{ id: 'A01', mood: 99 }] });
    expect(stale).toBeNull();
    const fresh = applyStateDiffToAgents(agents, 10, {
      tick: 11, agents: [{ id: 'A01', mood: 99, location_id: 'b', needs: { hunger: 44 } }],
    });
    expect(fresh?.tick).toBe(11);
    expect(fresh?.agents[0].mood).toBe(99);
    expect(fresh?.agents[0].location_id).toBe('b');
    expect(fresh?.agents[0].needs).toEqual({ hunger: 44 });
  });

  it('test_snapshot_rebuild_truncates_timeline：只保留 ≥快照 tick 事件（03 §6.2.3）', () => {
    const kept = truncateTimelineForSnapshot([ev(1, 5), ev(2, 10), ev(3, 15)], 10);
    expect(kept.map((e) => e.seq)).toEqual([2, 3]);
  });
});

describe('agentsStore LRU', () => {
  beforeEach(() => useAgentsStore.setState({ profiles: [], details: new Map() }));

  it('test_agents_lru_20：缓存 21 人 → 最旧挤出', () => {
    const s = useAgentsStore.getState();
    for (let i = 1; i <= AGENT_DETAIL_LRU + 1; i++) {
      s.cacheDetail(`A${String(i).padStart(2, '0')}`, { state: undefined });
    }
    const details = useAgentsStore.getState().details;
    expect(details.size).toBe(AGENT_DETAIL_LRU);
    expect(details.has('A01')).toBe(false);
    expect(details.has('A21')).toBe(true);
  });

  it('getDetail touch 提升最近使用', () => {
    const s = useAgentsStore.getState();
    s.cacheDetail('A01', {});
    s.cacheDetail('A02', {});
    expect(s.getDetail('A01')).toBeTruthy();
    s.cacheDetail('A03', {});
    for (let i = 4; i <= AGENT_DETAIL_LRU + 1; i++) s.cacheDetail(`A${String(i).padStart(2, '0')}`, {});
    const details = useAgentsStore.getState().details;
    expect(details.has('A01')).toBe(true);   // 被 touch 过
    expect(details.has('A02')).toBe(false);  // 最旧未触 → 挤出
  });
});

describe('ws 退避', () => {
  it('test_backoff_schedule：1,2,4…≤30s 且抖动 ±20% 内（03 §5.2）', () => {
    const cases = [[0, 1000], [1, 2000], [2, 4000], [3, 8000], [4, 16000], [5, 30000], [9, 30000]];
    for (const [attempt, base] of cases) {
      const lo = backoffMs(attempt as number, () => 0);
      const hi = backoffMs(attempt as number, () => 0.9999);
      expect(lo).toBeGreaterThanOrEqual((base as number) * 0.8);
      expect(hi).toBeLessThanOrEqual((base as number) * 1.2);
    }
  });
});

describe('worldStore 快照替换 + diff apply', () => {
  it('setSnapshot 重置 tick；applyDiff 更新 agents', () => {
    useWorldStore.getState().setSnapshot({
      tick: 100, sim_time: '2026-10-12T19:42:00+08:00', sim_day: 12, compression_ratio: 3.2,
      agents: [{ id: 'A01', name: '林晚', lod: 'star', location_id: 'apt.L2.203', activity: null,
        mood: 60, needs: { hunger: 50 } }],
      economy: { stocks: [] }, active_dialogues: [],
    });
    useWorldStore.getState().applyDiff({ tick: 101, agents: [{ id: 'A01', location_id: 'corp.tech' }] });
    const s = useWorldStore.getState();
    expect(s.tick).toBe(101);
    expect(s.snapshot?.agents[0].location_id).toBe('corp.tech');
    useWorldStore.getState().applyDiff({ tick: 100, agents: [{ id: 'A01', location_id: 'xxx' }] });
    expect(useWorldStore.getState().snapshot?.agents[0].location_id).toBe('corp.tech'); // 旧帧丢弃
  });
});
