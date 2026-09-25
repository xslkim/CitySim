/**
 * 涟漪 DAG 布局（03 §3.4/02 §7.4：D3-hierarchy 算布局 + SVG 自绘；传播代数=列、时间从左向右分层，
 * 禁纯力导向）。失真三档 ≤15% neutral / 15~40% warn / >40% negative（档值持有方 05 §3.7，引用）。
 */
import type { RippleData } from '../proto/ripple';

export type DistortionBand = 'neutral' | 'warn' | 'negative';

/** 失真三档（05 §3.7 唯一定义：≤0.15 / 0.15~0.40 / >0.40） */
export function distortionBand(d: number | null): DistortionBand {
  if (d == null || d <= 0.15) return 'neutral';
  if (d <= 0.4) return 'warn';
  return 'negative';
}

export const BAND_COLOR_VAR: Record<DistortionBand, string> = {
  neutral: 'var(--neutral)',
  warn: 'var(--warn)',
  negative: 'var(--negative)',
};

/** 传播边透明度：第 1 手实线、第 n 手透明度 100%−20%×n（02 §7.4） */
export function hopOpacity(hop: number): number {
  return Math.max(0.2, 1 - 0.2 * hop);
}

export interface DagNode {
  seq: number;
  hop: number;
  x: number;
  y: number;
  agentId: string; // 听者（hop≥1）；根节点 = 源事件主角
}

export interface DagEdge {
  fromSeq: number;
  toSeq: number;
  hop: number;
  distortion: number | null;
}

/** 分层布局：列 = hop（传播代数），列内按 sim_time 排序自上而下；根在列 0。 */
export function layoutRipple(data: RippleData, width = 560, rowH = 56): { nodes: DagNode[]; edges: DagEdge[] } {
  const edges: DagEdge[] = data.chain.map((c) => ({
    fromSeq: c.src_event_seq, toSeq: c.dst_event_seq, hop: c.hop, distortion: c.distortion,
  }));
  const byHop = new Map<number, typeof data.chain>();
  for (const c of data.chain) {
    const arr = byHop.get(c.hop) ?? [];
    arr.push(c);
    byHop.set(c.hop, arr);
  }
  const nodes: DagNode[] = [];
  const srcAgent = data.projections[0]?.agent_id ?? '';
  nodes.push({ seq: data.source_seq, hop: 0, x: 40, y: 60, agentId: srcAgent });
  const maxHop = Math.max(0, ...data.chain.map((c) => c.hop));
  const colW = maxHop > 0 ? (width - 80) / maxHop : width - 80;
  for (const [hop, arr] of [...byHop.entries()].sort((a, b) => a[0] - b[0])) {
    const sorted = [...arr].sort((a, b) => a.sim_time.localeCompare(b.sim_time));
    sorted.forEach((c, i) => {
      nodes.push({ seq: c.dst_event_seq, hop, x: 40 + hop * colW, y: 60 + i * rowH, agentId: c.listener_id });
    });
  }
  return { nodes, edges };
}

/** 导出选题卡（03 §3.4：纯文本摘要，供手剪切片） */
export function exportCardText(data: RippleData, sourceText: string): string {
  const lines = [
    `【选题卡】源事件 e${data.source_seq}：${sourceText}`,
    `覆盖 ${data.stats.covered_agents} 人 · ${data.stats.hops} 手传播 · 最大失真 ${(data.stats.max_distortion * 100).toFixed(0)}% · ${data.stats.followup_count} 条后续事件`,
    ...data.chain.map((c) => `  hop${c.hop} ${c.teller_id}→${c.listener_id}（失真 ${((c.distortion ?? 0) * 100).toFixed(0)}%，e${c.dst_event_seq}）`),
    ...data.relation_changes.map((r) =>
      `  关系 ${r.a_id}→${r.b_id} aff ${r.delta_affinity >= 0 ? '+' : ''}${r.delta_affinity} ten ${r.delta_tension >= 0 ? '+' : ''}${r.delta_tension}（e${r.event_seq}）`),
  ];
  return lines.join('\n');
}
