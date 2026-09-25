/** "今日热涟漪"卡片条（03 §3.4 推荐位：/map 右栏/顶部 + /ripple 无参落地页；点击直达 /ripple/:eventId）。 */
import { useEffect } from 'react';
import { apiGet } from '../../api/client';
import { rippleTodaySchema } from '../../proto/ripple';
import { useRippleStore } from '../../stores/rippleStore';
import { renderEvent } from '../../lib/eventText';

export default function TodayRipples({ onPick }: { onPick: (seq: number) => void }) {
  const today = useRippleStore((s) => s.today);
  const setToday = useRippleStore((s) => s.setToday);
  useEffect(() => {
    apiGet('/api/ripple/today', rippleTodaySchema)
      .then((r) => setToday(r.data))
      .catch(() => undefined); // 无 A 级日 = 空推荐位
  }, [setToday]);
  if (!today || !today.items.length) return null;
  return (
    <div className="mb-2 flex gap-2 overflow-x-auto" data-testid="today-ripples">
      {today.items.map((it) => (
        <button
          key={it.event.seq}
          type="button"
          onClick={() => onPick(it.event.seq)}
          className="card min-w-44 shrink-0 text-left hover:border-accent"
        >
          <div className="text-aux text-warn">A级 · 覆盖 {it.stats.covered_agents} 人</div>
          <div className="truncate text-body text-text-0">
            {renderEvent(it.event).text || it.event.type}
          </div>
        </button>
      ))}
    </div>
  );
}
