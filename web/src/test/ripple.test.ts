/** T-WEB-16 涟漪页测试（02 §7.4 视觉语言 / 03 §3.4 导出选题卡）。 */
import { describe, expect, it } from 'vitest';
import {
  distortionBand,
  exportCardText,
  hopOpacity,
  layoutRipple,
} from '../lib/rippleLayout';
import type { RippleData } from '../proto/ripple';

const DATA: RippleData = {
  source_seq: 100,
  projections: [
    { agent_id: 'A02', memory_id: 1, importance: 8, is_witness: true, content_display: '目击', sim_time: '2026-10-12T19:00:00+08:00' },
  ],
  chain: [
    { dst_event_seq: 101, src_event_seq: 100, teller_id: 'A02', listener_id: 'A04', hop: 1, distortion: 0.12, sim_time: '2026-10-12T20:00:00+08:00' },
    { dst_event_seq: 102, src_event_seq: 101, teller_id: 'A04', listener_id: 'A05', hop: 2, distortion: 0.34, sim_time: '2026-10-12T21:00:00+08:00' },
  ],
  followups: [],
  relation_changes: [
    { a_id: 'A01', b_id: 'A03', delta_affinity: -4, delta_tension: 8, labels_added: null, labels_removed: null, event_seq: 103, sim_time: '2026-10-12T22:00:00+08:00' },
  ],
  stats: { covered_agents: 3, hops: 2, max_distortion: 0.34, followup_count: 0 },
};

describe('T-WEB-16', () => {
  it('test_distortion_three_bands：0.14→neutral / 0.2→warn / 0.41→negative（05 §3.7 档）', () => {
    expect(distortionBand(0.14)).toBe('neutral');
    expect(distortionBand(0.15)).toBe('neutral');
    expect(distortionBand(0.2)).toBe('warn');
    expect(distortionBand(0.4)).toBe('warn');
    expect(distortionBand(0.41)).toBe('negative');
    expect(distortionBand(null)).toBe('neutral');
  });

  it('test_hop_opacity_decay：第 n 手透明度 100%−20%×n（02 §7.4）', () => {
    expect(hopOpacity(1)).toBeCloseTo(0.8);
    expect(hopOpacity(2)).toBeCloseTo(0.6);
    expect(hopOpacity(4)).toBeCloseTo(0.2);
  });

  it('布局：传播代数=列、列内按时间分层（禁纯力导向，02 §7.4）', () => {
    const { nodes, edges } = layoutRipple(DATA);
    expect(nodes.find((n) => n.seq === 100)?.x).toBe(40);
    expect(nodes.find((n) => n.seq === 102)!.x).toBeGreaterThan(nodes.find((n) => n.seq === 101)!.x);
    expect(edges).toHaveLength(2);
    expect(edges[1].distortion).toBe(0.34);
  });

  it('test_export_card_text：选题卡纯文本摘要（源事件+传播路径+关系变化）', () => {
    const text = exportCardText(DATA, '李梅当众拒绝王强');
    expect(text).toContain('e100');
    expect(text).toContain('李梅当众拒绝王强');
    expect(text).toContain('hop1 A02→A04');
    expect(text).toContain('aff -4');
  });
});
