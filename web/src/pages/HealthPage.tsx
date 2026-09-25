/**
 * /health 叙事健康度调试态面板（05 T-WEB-18；03 §3.6：七卡 + 运行指标条；调试态归属——
 * 导航不出现，d 键开关可达）。前端零阈值硬编码：值/阈值/色全取 /api/health（01 §9 持有方）。
 */
import { useEffect, useState } from 'react';
import { apiGet } from '../api/client';
import MetricCard from '../components/health/MetricCard';
import RuntimeBar from '../components/health/RuntimeBar';
import { healthDataSchema, type HealthData } from '../proto/health';
import { useUiStore } from '../stores/uiStore';

export default function HealthPage() {
  const debug = useUiStore((s) => s.debug);
  const [health, setHealth] = useState<HealthData | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    apiGet('/api/health', healthDataSchema)
      .then((r) => setHealth(r.data))
      .catch((e) => setError(String(e)));
  }, []);

  if (!debug) {
    return (
      <div className="card text-text-1" data-testid="health-locked">
        /health 属调试态（03 §3.6）：按 d 键开启调试态后可见。
      </div>
    );
  }
  if (error) return <div className="card text-negative">/api/health 失败：{error}</div>;
  if (!health) return <div className="card text-text-1">加载中…</div>;

  return (
    <div data-testid="health-page">
      <div className="mb-1 text-aux text-text-1">
        健康度日结：{health.sim_day ?? '—'}；当日未定稿：事件 {health.today_partial.events_today} 条 /
        A级 {health.today_partial.a_grade_today} 条 / 干预率{' '}
        {(health.today_partial.intervention_rate_today * 100).toFixed(1)}%
      </div>
      <div className="grid grid-cols-4 gap-2">
        {health.metrics.map((m) => (
          <MetricCard key={m.key} metric={m} />
        ))}
      </div>
      <div className="mt-2">
        <RuntimeBar runtime={health.runtime} />
      </div>
    </div>
  );
}
