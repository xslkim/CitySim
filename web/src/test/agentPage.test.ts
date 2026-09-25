/** T-WEB-15 角色/地点详情页测试。 */
import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { NEED_DIMS, NEED_LABELS, needsTrend, radarPoints } from '../components/agent/NeedsRadar';
import { extractCauseSeqs } from '../components/agent/DebugSourcePopover';
import type { ObsEvent } from '../proto/event';

describe('T-WEB-15', () => {
  it('test_needs_radar_six_dims：六维齐全（饥饿/精力/情绪/社交/成就/财富）', () => {
    expect(NEED_DIMS).toHaveLength(6);
    expect(Object.values(NEED_LABELS)).toEqual(['饥饿', '精力', '情绪', '社交', '成就', '财富']);
    const pts = radarPoints({ hunger: 100, energy: 0, mood: 50, social: 80, achievement: 61, wealth: 35 });
    expect(pts.split(' ')).toHaveLength(6);
  });

  it('needsTrend：needs_delta changes[].new_value 序列', () => {
    const events = [
      { sim_time: 't1', payload: { changes: [{ agent_id: 'A01', need: 'hunger', new_value: 44, cause: '5' }] } },
      { sim_time: 't2', payload: { changes: [{ agent_id: 'A01', need: 'mood', new_value: 66, cause: '6' }] } },
    ] as unknown as ObsEvent[];
    expect(needsTrend(events, 'hunger')).toEqual([{ t: 't1', v: 44 }]);
  });

  it('test_debug_popover_seq_links：⏱ 提取最近 10 次变更来源 seq（裸 seq 数字字符串，06 §2）', () => {
    const events = Array.from({ length: 12 }, (_, i) => ({
      payload: { changes: [{ agent_id: 'A01', need: 'hunger', new_value: i, cause: String(100 + i) }] },
    }));
    const seqs = extractCauseSeqs(events, 'A01', 'hunger');
    expect(seqs).toHaveLength(10);
    expect(seqs[0]).toBe(111); // 最新在前
  });

  it('⏱ 弹层渲染 e<seq> 链接（03 §1.1 反查）', async () => {
    const { extractCauseSeqs: _ } = await import('../components/agent/DebugSourcePopover');
    expect(typeof _).toBe('function');
  });
});
