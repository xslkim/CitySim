/** 时间轴虚拟列表 + 详情抽屉（trigger 徽标 / grade_revise 改判标记 / caused_by 链，03 §3.2）。 */
import { useVirtualizer } from '@tanstack/react-virtual';
import { useRef, useState } from 'react';
import type { ObsEvent } from '../../proto/event';
import { displaySeq, renderEvent, triggerColorVar } from '../../lib/eventText';
import { useUiStore } from '../../stores/uiStore';

export const ROW_H = 44;

/** `?focus=e<seq>` 解析（03 §1.1）；非法 → null */
export function parseFocus(focus: string | null): number | null {
  if (!focus) return null;
  const m = /^e(\d+)$/.exec(focus);
  return m ? Number(m[1]) : null;
}

function DetailDrawer({ ev, onClose }: { ev: ObsEvent; onClose: () => void }) {
  const p = ev.payload as Record<string, any>;
  return (
    <div className="card mt-2 text-aux" data-testid="event-detail">
      <div className="flex items-center gap-2">
        <span className="text-text-0">{displaySeq(ev.seq)}</span>
        <span className="rounded px-1" style={{ background: triggerColorVar(ev.trigger) }}>
          {ev.trigger}
        </span>
        <span className="text-text-1">{ev.type} · tick {ev.tick} · {ev.sim_time}</span>
        <button type="button" className="ml-auto text-text-1" onClick={onClose}>✕</button>
      </div>
      {ev.type === 'director.grade_revise' && (
        <div className="mt-1 text-director" data-testid="grade-revise-badge">
          改判标记：{displaySeq(Number(p.target_seq))} → {p.new_grade}（{p.reason}）
        </div>
      )}
      {p.caused_by && (
        <div className="mt-1 text-text-1">
          因果链：← <a className="text-accent" href={`/timeline?focus=e${p.caused_by}`}>{displaySeq(Number(p.caused_by))}</a>
        </div>
      )}
      <pre className="mt-1 max-h-40 overflow-auto rounded bg-bg-0 p-2 text-ts text-text-1">
        {JSON.stringify(ev.payload, null, 2)}
      </pre>
    </div>
  );
}

export default function TimelineList({ events, names }: { events: ObsEvent[]; names: Map<string, string> }) {
  const parentRef = useRef<HTMLDivElement>(null);
  const debug = useUiStore((s) => s.debug);
  const [open, setOpen] = useState<number | null>(null);
  const nameOf = (id: string) => names.get(id) ?? id;
  const visible = events.filter((e) => {
    const k = renderEvent(e, nameOf).kind;
    if (k === 'hidden') return debug;
    if (k === 'system') return debug || e.type === 'time.day_summary';
    return true;
  });
  const virtualizer = useVirtualizer({
    count: visible.length,
    getScrollElement: () => parentRef.current,
    estimateSize: () => ROW_H,
    overscan: 50,
    initialRect: { width: 600, height: 520 },
  });
  return (
    <>
      <div ref={parentRef} style={{ height: 520, overflowY: 'auto' }} data-testid="timeline-list">
        <div style={{ height: virtualizer.getTotalSize(), position: 'relative' }}>
          {virtualizer.getVirtualItems().map((vi) => {
            const e = visible[vi.index];
            const r = renderEvent(e, nameOf);
            return (
              <button
                key={e.seq}
                type="button"
                onClick={() => setOpen(open === e.seq ? null : e.seq)}
                className={`absolute left-0 top-0 flex w-full items-center gap-2 border-b border-border text-left ${
                  e.ui?.grade === 'A' ? 'border-l-[6px]' : 'border-l-4' // A 级色条加粗（02 §7.6）
                }`}
                style={{ transform: `translateY(${vi.start}px)`, height: ROW_H,
                  borderLeftColor: triggerColorVar(e.trigger) }}
                data-seq={e.seq}
              >
                <span className="pl-1 text-ts text-text-1">{e.sim_time.slice(11, 16)}</span>
                <span>{r.icon}</span>
                <span className={`truncate text-body ${r.kind === 'gray' ? 'text-text-1' : 'text-text-0'}`}>
                  {r.text}
                </span>
                {e.type === 'director.grade_revise' && (
                  <span className="rounded bg-bg-2 px-1 text-ts text-director">改判</span>
                )}
                <span className="ml-auto pr-1 text-ts text-text-1">{displaySeq(e.seq)}</span>
              </button>
            );
          })}
        </div>
      </div>
      {open != null && visible.find((e) => e.seq === open) && (
        <DetailDrawer ev={visible.find((e) => e.seq === open)!} onClose={() => setOpen(null)} />
      )}
    </>
  );
}
