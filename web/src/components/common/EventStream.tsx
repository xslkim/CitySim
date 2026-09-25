/**
 * 事件流虚拟列表（03 §6.3：@tanstack/react-virtual，行高 44px，窗口 ±50 条）。
 * 点击跳 `/timeline?focus=e<seq>`（03 §1.1 反查原则；e<seq> 仅渲染层前缀，06 §2）。
 * 条目左 4px 色条标识 trigger（02 §7.3）；股价行红涨绿跌带图例（03 §5.3 例外区）。
 */
import { useVirtualizer } from '@tanstack/react-virtual';
import { useRef } from 'react';
import { useNavigate } from 'react-router-dom';
import type { ObsEvent } from '../../proto/event';
import { displaySeq, renderEvent, triggerColorVar } from '../../lib/eventText';
import { useUiStore } from '../../stores/uiStore';

export const ROW_HEIGHT = 44;   // 03 §6.3
export const OVERSCAN = 50;     // 窗口 ±50 条（03 §6.3）

interface Props {
  events: ObsEvent[];
  names?: Map<string, string>;
  height?: number;
}

function hhmm(simTime: string): string {
  const d = new Date(simTime);
  return `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;
}

export default function EventStream({ events, names, height = 480 }: Props) {
  const parentRef = useRef<HTMLDivElement>(null);
  const navigate = useNavigate();
  const debug = useUiStore((s) => s.debug);
  const nameOf = (id: string) => names?.get(id) ?? id;

  const visible = events.filter((e) => {
    const r = renderEvent(e, nameOf);
    if (r.kind === 'hidden') return debug; // internal 聚合仅调试态（06 §1.2 表头）
    if (r.kind === 'system') return debug || e.type === 'time.day_summary';
    return true;
  });

  const virtualizer = useVirtualizer({
    count: visible.length,
    getScrollElement: () => parentRef.current,
    estimateSize: () => ROW_HEIGHT,
    overscan: OVERSCAN,
    initialRect: { width: 600, height }, // jsdom/首帧无测量时的初始视口
  });

  const hasStock = visible.some((e) => e.type === 'economy.stock.tick');

  return (
    <div>
      {hasStock && (
        <div className="mb-1 text-ts text-text-1">
          图例：股价 <span className="text-negative">红涨</span>
          <span className="text-positive">绿跌</span>（金融组件例外区，02 §7.1 立法 4）
        </div>
      )}
      <div ref={parentRef} style={{ height, overflowY: 'auto' }} data-testid="event-stream">
        <div style={{ height: virtualizer.getTotalSize(), position: 'relative' }}>
          {virtualizer.getVirtualItems().map((vi) => {
            const e = visible[vi.index];
            const r = renderEvent(e, nameOf);
            const stockColor =
              r.stockDir === 'up' ? 'text-negative' : r.stockDir === 'down' ? 'text-positive' : '';
            return (
              <button
                key={e.seq}
                type="button"
                onClick={() => navigate(`/timeline?focus=${displaySeq(e.seq)}`)}
                className="absolute left-0 top-0 flex w-full items-center gap-2 border-b border-border text-left"
                style={{
                  transform: `translateY(${vi.start}px)`,
                  height: ROW_HEIGHT,
                  borderLeft: `4px solid ${triggerColorVar(e.trigger)}`,
                  ...(r.directorBorder ? { outline: '1px solid var(--director)' } : {}),
                }}
                data-seq={e.seq}
              >
                <span className="pl-1 text-ts text-text-1">{hhmm(e.sim_time)}</span>
                <span>{r.icon}</span>
                <span className={`truncate text-body ${r.kind === 'gray' ? 'text-text-1' : 'text-text-0'} ${stockColor}`}>
                  {r.text}
                </span>
                {r.grade === 'A' && <span className="text-ts text-warn">A级</span>}
              </button>
            );
          })}
        </div>
      </div>
    </div>
  );
}
