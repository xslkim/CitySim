/** 健康度指标卡（03 §3.6：趋势 sparkline + 超阈值变色 + 建议动作；值/阈值/色全取 /api/health，零硬编码）。 */
import type { HealthMetric } from '../../proto/health';

export const COLOR_MAP = { green: 'positive', yellow: 'warn', red: 'negative' } as const; // 02 §7.1 语义色映射

export function metricColorClass(color: HealthMetric['color']): string {
  if (!color) return 'text-text-1';
  return `text-${COLOR_MAP[color]}`;
}

const METRIC_LABELS: Record<string, string> = {
  a_grade_event_interval_days: 'A级事件间隔',
  event_type_entropy_bits: '类型熵',
  appearance_gini: '出场基尼',
  dialogue_3gram_repeat_ratio: '3-gram 重复度',
  relation_graph_weekly_change_ratio: '关系周变化率',
  high_tension_edge_ratio: '高张力边占比',
  active_conflict_edges: '活跃冲突边密度',
};

export function metricLabel(key: string): string {
  return METRIC_LABELS[key] ?? key;
}

function Sparkline({ history }: { history: { value: number | null }[] }) {
  const vals = history.map((h) => h.value).filter((v): v is number => v != null);
  if (vals.length < 2) return <div className="h-6 text-ts text-text-1">—</div>;
  const max = Math.max(...vals, 1e-9);
  const pts = vals.map((v, i) => `${(i / (vals.length - 1)) * 100},${24 - (v / max) * 22}`).join(' ');
  return (
    <svg viewBox="0 0 100 24" className="h-6 w-full">
      <polyline points={pts} fill="none" stroke="var(--accent)" strokeWidth={1} />
    </svg>
  );
}

export default function MetricCard({ metric }: { metric: HealthMetric }) {
  const colorCls = metricColorClass(metric.color);
  return (
    <div className="card" data-testid={`metric-${metric.key}`}>
      <div className="text-aux text-text-1">{metricLabel(metric.key)}</div>
      <div className={`text-title ${colorCls}`}>
        {metric.value == null ? '—' : metric.unit === 'ratio' ? `${(metric.value * 100).toFixed(1)}%` : metric.value}
        <span className="ml-1 text-ts">{metric.color === 'red' ? '🔴' : metric.color === 'yellow' ? '🟡' : '🟢'}</span>
      </div>
      <Sparkline history={metric.history} />
      {metric.color !== 'green' && metric.advice && (
        <div className="mt-1 text-ts text-warn">建议：{metric.advice}</div>
      )}
    </div>
  );
}
