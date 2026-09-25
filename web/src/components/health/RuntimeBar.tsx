/** 运行指标条（03 §3.6：干预率 vs 红线 / ¥每模拟日 vs 熔断线 / 副本延迟；数值全 API 下发）。 */
import type { HealthData } from '../../proto/health';

export default function RuntimeBar({ runtime }: { runtime: HealthData['runtime'] }) {
  const rate = runtime.intervention_rate;
  const cap = runtime.intervention_rate_cap;
  const overCap = rate != null && cap != null && rate >= cap;
  const cost = runtime.cost_micro_cny;
  const costLimit = runtime.cost_limit_micro_cny;
  const alarmRatio = runtime.cost_breaker.alarm_ratio;
  const costAlarm = cost != null && costLimit != null && costLimit > 0 && alarmRatio != null
    && cost > costLimit * alarmRatio;
  return (
    <div className="card flex items-center gap-4 text-aux" data-testid="runtime-bar">
      <span className={overCap ? 'text-negative' : 'text-positive'}>
        干预率 {rate == null ? '—' : `${(rate * 100).toFixed(1)}%`}
        {cap != null && `（红线 ${(cap * 100).toFixed(0)}%）`}
      </span>
      <span className={costAlarm ? 'text-negative' : 'text-text-1'}>
        ¥/模拟日 {cost == null ? '—' : (cost / 1_000_000).toFixed(4)}
        {costLimit != null && costLimit > 0 && ` / 熔断线 ¥${(costLimit / 1_000_000).toFixed(2)}`}
        {costLimit === 0 && '（基线未配置/免费档 ¥0，A9 口径）'}
      </span>
      <span className="text-text-1">副本延迟 {runtime.replica_lag_s}s（M4 主库直连恒近零，05 D4）</span>
    </div>
  );
}
