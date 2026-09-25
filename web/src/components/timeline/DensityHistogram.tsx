/** 密度直方图（03 §3.2：每模拟小时事件计数，A 级红柱叠加，点击定位时段）。 */
import { useEffect, useState } from 'react';
import { apiGet } from '../../api/client';
import { eventsHistogramSchema } from '../../proto';

interface Bucket {
  bucket_start: string;
  count: number;
  a_count: number;
}

export default function DensityHistogram({ onPick }: { onPick: (fromSim: string) => void }) {
  const [buckets, setBuckets] = useState<Bucket[]>([]);
  useEffect(() => {
    apiGet('/api/events/histogram', eventsHistogramSchema)
      .then((r) => setBuckets(r.data))
      .catch(() => undefined);
  }, []);
  const max = Math.max(1, ...buckets.map((b) => b.count));
  return (
    <div className="mb-2 flex h-14 items-end gap-px" data-testid="density-histogram">
      {buckets.map((b) => (
        <button
          key={b.bucket_start}
          type="button"
          title={`${b.bucket_start}：${b.count} 条（A级 ${b.a_count}）`}
          onClick={() => onPick(b.bucket_start)}
          className="relative flex-1"
          style={{ height: `${(b.count / max) * 100}%`, minHeight: 2 }}
        >
          <div className="absolute inset-0 rounded-t bg-bg-2" />
          {b.a_count > 0 && (
            <div className="absolute inset-x-0 bottom-0 rounded-t bg-negative"
              style={{ height: `${(b.a_count / b.count) * 100}%` }} />
          )}
        </button>
      ))}
    </div>
  );
}
