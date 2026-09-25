/**
 * 气泡层（03 §3.1：💬双人连线 / 💭灰虚线（调试态全文）/ 📢公告波及；逐句播放接 playbackEngine，
 * 进度点 ●●○○○；历史时刻显整段文本不重播，03 §4.2 边界 2）。
 */
import { useEffect, useState } from 'react';
import type { DialogueLine } from '../../proto/event';
import { PlaybackEngine, progressDots } from '../../lib/playbackEngine';
import { useUiStore } from '../../stores/uiStore';

export interface BubbleDialogue {
  eventSeq: number;
  locationId: string | null;
  participants: string[];
  lines: DialogueLine[];
}

interface Props {
  dialogues: BubbleDialogue[];
  anchorOf: (agentId: string) => { x: number; y: number } | null;
  historical: boolean; // 历史时刻：整段直显不重播（03 §4.2 边界 2）
}

/** 单对话气泡：逐句播放（客户端倍率唯一节拍权威，06 §2） */
function DialogueBubble({ d, anchorOf, historical }: { d: BubbleDialogue; historical: boolean } & Pick<Props, 'anchorOf'>) {
  const rate = useUiStore((s) => s.playbackRate);
  const debug = useUiStore((s) => s.debug);
  const [cursor, setCursor] = useState(0);

  useEffect(() => {
    if (historical || rate > 2 || !d.lines.length) {
      setCursor(d.lines.length); // 历史/高速档：整段直显（D16）
      return;
    }
    setCursor(0);
    const eng = new PlaybackEngine({
      onLine: (_l, i) => setCursor(i + 1),
    });
    eng.play(d.lines, rate);
    return () => eng.pause();
  }, [d.eventSeq, rate, historical, d.lines]);

  if (!d.lines.length) return null;
  const anchor = anchorOf(d.participants[0]);
  if (!anchor) return null;
  const line = d.lines[Math.min(Math.max(0, cursor - 1), d.lines.length - 1)];
  return (
    <g transform={`translate(${anchor.x},${anchor.y - 26})`}>
      <foreignObject x={-70} y={-44} width={140} height={42}>
        <div
          className="rounded-card border bg-bg-2 px-1.5 py-0.5 text-center text-ts text-text-0"
          style={{ borderColor: 'var(--border)', borderStyle: debug ? 'dashed' : 'solid' }}
        >
          {line?.text_display}
          <div className="text-accent">{progressDots(cursor, d.lines.length)}</div>
        </div>
      </foreignObject>
      {/* 💬 双人连线（02 §7.2 对话连线层虚线） */}
      {d.participants[1] && anchorOf(d.participants[1]) && (
        <line
          x1={0}
          y1={26}
          x2={anchorOf(d.participants[1])!.x - anchor.x}
          y2={anchorOf(d.participants[1])!.y - anchor.y + 26}
          stroke="var(--neutral)"
          strokeDasharray="3 2"
          strokeWidth={1}
        />
      )}
    </g>
  );
}

export default function BubbleLayer({ dialogues, anchorOf, historical }: Props) {
  return (
    <g data-testid="bubble-layer">
      {dialogues.map((d) => (
        <DialogueBubble key={d.eventSeq} d={d} anchorOf={anchorOf} historical={historical} />
      ))}
    </g>
  );
}
