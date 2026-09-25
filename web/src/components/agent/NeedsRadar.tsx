/** 需求六维雷达（03 §3.3：饥饿/精力/情绪/社交/成就/财富 0~100）+ 24 模拟小时趋势线（state.needs_delta 归约）。 */
import type { ObsEvent } from '../../proto/event';

export const NEED_DIMS = ['hunger', 'energy', 'mood', 'social', 'achievement', 'wealth'] as const;
export const NEED_LABELS: Record<string, string> = {
  hunger: '饥饿', energy: '精力', mood: '情绪', social: '社交', achievement: '成就', wealth: '财富',
};

export function radarPoints(needs: Record<string, number>, r = 60, cx = 70, cy = 70): string {
  return NEED_DIMS.map((d, i) => {
    const v = Math.max(0, Math.min(100, needs[d] ?? 0)) / 100;
    const angle = (Math.PI * 2 * i) / NEED_DIMS.length - Math.PI / 2;
    return `${cx + r * v * Math.cos(angle)},${cy + r * v * Math.sin(angle)}`;
  }).join(' ');
}

/** 24 模拟小时趋势线：needs_delta changes[].new_value 序列（cause 链见 ⏱ 调试钩子） */
export function needsTrend(events: ObsEvent[], dim: string): { t: string; v: number }[] {
  const pts: { t: string; v: number }[] = [];
  for (const e of events) {
    for (const ch of (e.payload.changes as any[]) ?? []) {
      if (ch.need === dim && typeof ch.new_value === 'number') {
        pts.push({ t: e.sim_time, v: ch.new_value });
      }
    }
  }
  return pts;
}

export default function NeedsRadar({ needs }: { needs: Record<string, number> | null }) {
  const n = needs ?? {};
  return (
    <div data-testid="needs-radar">
      <svg viewBox="0 0 140 140" className="h-36 w-36">
        <polygon points={radarPoints(n)} fill="var(--bg-2)" stroke="var(--accent)" strokeWidth={1.5} />
        {NEED_DIMS.map((d, i) => {
          const angle = (Math.PI * 2 * i) / NEED_DIMS.length - Math.PI / 2;
          return (
            <text key={d} x={70 + 66 * Math.cos(angle)} y={70 + 66 * Math.sin(angle)}
              fontSize={8} fill="var(--text-1)" textAnchor="middle">
              {NEED_LABELS[d]}
            </text>
          );
        })}
      </svg>
      <div className="grid grid-cols-3 gap-1 text-ts text-text-1">
        {NEED_DIMS.map((d) => (
          <span key={d}>
            {NEED_LABELS[d]} <b className="text-text-0">{n[d] ?? '—'}</b>
          </span>
        ))}
      </div>
    </div>
  );
}
