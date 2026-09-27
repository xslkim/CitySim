/**
 * 地图页右栏默认面板（2026-09-27 观察页内容丰富化）：未选中角色时的"此刻"总览——
 * 进行中对话（最新一句）+ 焦点角色动态（位置/活动/情绪）+ 实时事件流。
 * 此前右栏默认只有裸事件流，世界安静时整栏空白，观察页"没有内容"的主要体感来源。
 */
import type { ObsEvent } from '../../proto/event';
import type { SnapshotData } from '../../proto/snapshot';
import { locationName } from '../../lib/mapLayout';
import Avatar from '../common/Avatar';
import EventStream from '../common/EventStream';

interface Props {
  snapshot: SnapshotData | null;
  events: ObsEvent[];
  names: Map<string, string>;
  signatureColors: Map<string, string | null>;
  onSelectAgent: (id: string) => void;
}

/** snapshot.agents[].activity 是最近事件 type（技术串）→ 人话标签；未登记不显示（不露技术黑话） */
export const ACTIVITY_LABEL: Record<string, string> = {
  'agent.think': '沉思', 'agent.rest': '休息', 'agent.eat': '用餐', 'agent.move': '走动',
  'agent.work': '工作', 'agent.shop': '购物', 'agent.trade_stock': '炒股', 'agent.reflection': '反思',
  'dialogue.chat': '聊天', 'dialogue.argue': '争执', 'dialogue.gossip': '八卦',
  'dialogue.confess': '告白', 'dialogue.apologize': '道歉',
  'social.invite': '邀约', 'social.send_message': '捎话', 'social.give_gift': '送礼',
  'social.help': '帮忙', 'social.borrow_money': '借钱', 'social.repay_money': '还钱',
};

export default function NowPanel({ snapshot, events, names, signatureColors, onSelectAgent }: Props) {
  const nameOf = (id: string) => names.get(id) ?? id;
  const dialogues = snapshot?.active_dialogues ?? [];
  const focus = (snapshot?.agents ?? []).filter((a) => a.lod !== 'background');

  return (
    <div className="space-y-2" data-testid="now-panel">
      <section className="card">
        <div className="mb-1 text-body text-text-0">进行中对话</div>
        {dialogues.length === 0 && (
          <div className="text-aux text-text-1">此刻没有对话——世界安静中</div>
        )}
        {dialogues.map((d) => {
          const last = d.lines[d.lines.length - 1];
          return (
            <div key={d.event_seq} className="mb-1 rounded bg-bg-2 p-1.5 text-aux">
              <div className="text-text-0">
                💬 {d.participants.map(nameOf).join(' × ')}
                <span className="ml-1 text-text-1">{d.location_id ? `@${locationName(d.location_id)}` : ''}</span>
              </div>
              {last && (
                <div className="mt-0.5 truncate text-text-1">
                  {nameOf(last.speaker)}：{last.text_display}
                </div>
              )}
            </div>
          );
        })}
      </section>
      <section className="card">
        <div className="mb-1 text-body text-text-0">角色动态</div>
        {focus.length === 0 && <div className="text-aux text-text-1">快照加载中…</div>}
        <div className="space-y-0.5">
          {focus.map((a) => (
            <button
              key={a.id}
              type="button"
              onClick={() => onSelectAgent(a.id)}
              className="flex w-full items-center gap-1.5 rounded px-1 py-0.5 text-left text-aux hover:bg-bg-2"
            >
              <Avatar id={a.id} name={a.name} signatureColor={signatureColors.get(a.id)} size={18} />
              <span className="text-text-0">{a.lod === 'star' ? '★' : '◐'} {a.name}</span>
              <span className="truncate text-text-1">
                {a.location_id ? locationName(a.location_id) : '—'}
                {a.activity && ACTIVITY_LABEL[a.activity] ? ` · ${ACTIVITY_LABEL[a.activity]}` : ''}
              </span>
              {a.mood != null && <span className="ml-auto text-text-1">♥{Math.round(a.mood)}</span>}
            </button>
          ))}
        </div>
      </section>
      <section className="card">
        <div className="mb-1 text-body text-text-0">动态</div>
        <EventStream events={events.slice(-100)} names={names} height={320} />
      </section>
    </div>
  );
}
