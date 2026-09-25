/**
 * /timeline 事件时间轴页（05 T-WEB-14；03 §3.2：倒带/变速/六维过滤/直方图/虚拟列表）。
 * 过滤全部下推 REST（03 §3.2）；速度档 0.5/1/2/4/8× 入 uiStore（与内核压缩比分别显示）；
 * `/timeline?focus=e<seq>` 入口解析定位（03 §1.1）。
 */
import { useCallback, useEffect, useMemo, useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import { apiGet } from '../api/client';
import DensityHistogram from '../components/timeline/DensityHistogram';
import FilterBar, { buildEventParams } from '../components/timeline/FilterBar';
import TimelineList, { parseFocus } from '../components/timeline/TimelineList';
import { envelopeEventsSchema } from '../proto';
import type { ObsEvent } from '../proto/event';
import { useAgentsStore } from '../stores/agentsStore';
import { EMPTY_FILTER, useTimelineStore, type TimelineFilter } from '../stores/timelineStore';
import { useUiStore, type PlaybackRate } from '../stores/uiStore';

const RATES: PlaybackRate[] = [0.5, 1, 2, 4, 8];

export default function TimelinePage() {
  const [params, setParams] = useSearchParams();
  const profiles = useAgentsStore((s) => s.profiles);
  const rate = useUiStore((s) => s.playbackRate);
  const setRate = useUiStore((s) => s.setPlaybackRate);
  const [filter, setFilter] = useState<TimelineFilter>(EMPTY_FILTER);
  const [items, setItems] = useState<ObsEvent[]>([]);
  const [cursor, setCursor] = useState<number | null>(null);
  const [fromSim, setFromSim] = useState<string | null>(null);
  const live = useTimelineStore((s) => s.live);
  const setLive = useTimelineStore((s) => s.setLive);

  const names = useMemo(() => new Map(profiles.map((p) => [p.id, p.name])), [profiles]);

  const load = useCallback(
    (cursor_: number | null, f: TimelineFilter, from: string | null) => {
      const qs = buildEventParams(f);
      const url = `/api/events?limit=500${qs ? `&${qs}` : ''}${cursor_ ? `&cursor=${cursor_}` : ''}${from ? `&from=${encodeURIComponent(from)}` : ''}`;
      apiGet(url, envelopeEventsSchema)
        .then((r) => {
          setItems((prev) => {
            const bySeq = new Map<number, ObsEvent>();
            for (const e of cursor_ ? prev : []) bySeq.set(e.seq, e);
            for (const e of r.data.items) bySeq.set(e.seq, e);
            return [...bySeq.values()].sort((a, b) => a.seq - b.seq);
          });
          setCursor(r.data.next_cursor);
        })
        .catch(() => undefined);
    },
    [],
  );

  useEffect(() => {
    setItems([]);
    load(null, filter, fromSim);
  }, [filter, fromSim, load]);

  // ?focus=e<seq> 入口定位（03 §1.1）
  const focusSeq = parseFocus(params.get('focus'));

  return (
    <div data-testid="timeline-page">
      <FilterBar
        filter={filter}
        onChange={(f) => setFilter((s) => ({ ...s, ...f }))}
        actors={profiles.map((p) => p.id)}
        locations={[...new Set(profiles.map((p) => p.room_no).filter(Boolean) as string[])]}
      />
      <DensityHistogram onPick={(sim) => { setFromSim(sim); setLive(false); }} />
      <div className="mb-1 flex items-center gap-2 text-aux text-text-1">
        {RATES.map((r) => (
          <button
            key={r}
            type="button"
            className={`rounded px-1.5 py-0.5 ${rate === r ? 'bg-bg-2 text-accent' : ''}`}
            onClick={() => setRate(r)}
          >
            {r}×
          </button>
        ))}
        <span className="text-text-1">（观察端播放倍率，与内核压缩比独立，03 §3.2）</span>
        {!live && (
          <button type="button" className="rounded bg-bg-2 px-2 text-accent"
            onClick={() => { setFromSim(null); setLive(true); }}>
            跳至 live
          </button>
        )}
        {focusSeq && <span className="text-accent">focus=e{focusSeq}</span>}
      </div>
      <TimelineList events={items} names={names} />
      {cursor && (
        <button type="button" className="mt-1 rounded bg-bg-2 px-3 py-1 text-aux text-text-1"
          onClick={() => load(cursor, filter, fromSim)}>
          加载更早（cursor={cursor}）
        </button>
      )}
    </div>
  );
}
