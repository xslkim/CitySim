/**
 * ⏱ 调试钩子（03 §3.3）：数值旁弹层列最近 10 次变更来源事件
 * （state.needs_delta / relation.changed 的 payload.changes[].cause，05 §6 行；e<seq> 可点击跳 timeline）。
 */
import { useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import { apiGet } from '../../api/client';
import { envelopeEventsSchema } from '../../proto';
import { displaySeq } from '../../lib/eventText';

/** 从事件批提取某角色某字段的变更来源 seq（changes[].cause = 裸 seq 数字字符串，06 §2） */
export function extractCauseSeqs(events: { payload: Record<string, any> }[], agentId: string, field?: string): number[] {
  const seqs: number[] = [];
  for (const e of events) {
    for (const ch of (e.payload.changes as any[]) ?? []) {
      if (ch.agent_id && ch.agent_id !== agentId) continue;
      if (ch.a_id && ch.a_id !== agentId && ch.b_id !== agentId) continue;
      if (field && ch.need && ch.need !== field) continue;
      if (typeof ch.cause === 'string' && /^\d+$/.test(ch.cause)) seqs.push(Number(ch.cause));
    }
  }
  return [...new Set(seqs)].slice(-10).reverse();
}

export default function DebugSourcePopover({ agentId, field }: { agentId: string; field?: string }) {
  const [open, setOpen] = useState(false);
  const [seqs, setSeqs] = useState<number[]>([]);
  useEffect(() => {
    if (!open) return;
    apiGet(`/api/events?type=state.needs_delta,relation.changed&actor=${agentId}&limit=200`, envelopeEventsSchema)
      .then((r) => setSeqs(extractCauseSeqs(r.data.items, agentId, field)))
      .catch(() => undefined);
  }, [open, agentId, field]);
  return (
    <span className="relative inline-block">
      <button type="button" className="px-0.5 text-text-1" title="数值来源事件"
        onClick={() => setOpen(!open)}>
        ⏱
      </button>
      {open && (
        <div className="absolute z-20 w-44 rounded-card border border-border bg-bg-2 p-2 text-ts" data-testid="debug-popover">
          <div className="mb-1 text-text-1">最近 {seqs.length} 次变更来源（06 §2 裸 seq）</div>
          {seqs.length === 0 && <div className="text-text-1">暂无变更记录</div>}
          {seqs.map((s) => (
            <Link key={s} to={`/timeline?focus=e${s}`} className="block text-accent" data-seq={s}>
              {displaySeq(s)}
            </Link>
          ))}
        </div>
      )}
    </span>
  );
}
