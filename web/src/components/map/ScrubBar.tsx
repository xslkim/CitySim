/** 底部 scrub bar（03 §3.1/§4.2：拖到历史 tick → /api/snapshot?tick= 静态重建；拖回/跳至 live 恢复）。 */
import { useState } from 'react';
import { useTimelineStore } from '../../stores/timelineStore';
import { useWorldStore } from '../../stores/worldStore';

interface Props {
  onScrub: (tick: number | null) => void; // null = 回 live
}

export default function ScrubBar({ onScrub }: Props) {
  const watermark = useWorldStore((s) => s.watermarkTick);
  const snapshotTick = useWorldStore((s) => s.snapshot?.tick ?? 0);
  const live = useTimelineStore((s) => s.live);
  const setLive = useTimelineStore((s) => s.setLive);
  const [value, setValue] = useState<number | null>(null);
  const max = Math.max(watermark, snapshotTick, 1);

  return (
    <div className="mt-1 flex items-center gap-2 text-aux text-text-1" data-testid="scrub-bar">
      <span>◀</span>
      <input
        type="range"
        min={0}
        max={max}
        value={value ?? max}
        onChange={(e) => {
          const v = Number(e.target.value);
          setValue(v);
          if (v >= max) {
            setLive(true);
            onScrub(null);
          } else {
            setLive(false);
            onScrub(v);
          }
        }}
        className="flex-1"
        aria-label="历史 tick scrub"
      />
      <span>▶</span>
      {!live && (
        <button
          type="button"
          className="rounded bg-bg-2 px-2 py-0.5 text-accent"
          onClick={() => {
            setValue(null);
            setLive(true);
            onScrub(null);
          }}
        >
          跳至 live
        </button>
      )}
      <span>{live ? 'live' : `历史 tick ${value}`}</span>
    </div>
  );
}
