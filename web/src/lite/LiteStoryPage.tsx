/**
 * /lite/story 传播链故事页（05 T-WEB-19；ui/web-lite/story.html 原型形态：
 * 一句话如何传遍全楼——逐手叙事 + 失真人话标注 + 涉及的人；无 DAG 图/无工程词汇）。
 * 入口：`?e=<seq>`（lite 内部链接参数；页面文案不暴露 seq 字样）。
 */
import { useEffect, useMemo, useState } from 'react';
import { Link, useSearchParams } from 'react-router-dom';
import { apiGet } from '../api/client';
import Avatar from '../components/common/Avatar';
import { rippleDataSchema, rippleTodaySchema, type RippleData } from '../proto/ripple';
import { useAgentsStore } from '../stores/agentsStore';
import LiteShell from './LiteShell';
import { distortionPhrase, narrateEvent } from './narrativeMap';

export default function LiteStoryPage() {
  const [params] = useSearchParams();
  const profiles = useAgentsStore((s) => s.profiles);
  const [data, setData] = useState<RippleData | null>(null);
  const [title, setTitle] = useState('');
  const [empty, setEmpty] = useState(false);
  const names = useMemo(() => new Map(profiles.map((p) => [p.id, p.name])), [profiles]);
  const nameOf = (x: string) => names.get(x) ?? x;
  const colorOf = (x: string) => profiles.find((p) => p.id === x)?.signature_color;

  useEffect(() => {
    const e = params.get('e');
    const load = (seq: number) =>
      apiGet(`/api/ripple/${seq}`, rippleDataSchema)
        .then((r) => {
          setData(r.data);
          setTitle(r.data.source ? narrateEvent(r.data.source, nameOf).text : '一件事传开了');
        })
        .catch(() => setEmpty(true));
    if (e && /^\d+$/.test(e)) {
      load(Number(e));
    } else {
      apiGet('/api/ripple/today', rippleTodaySchema)
        .then((r) => {
          const top = r.data.items[0];
          if (top) load(top.event.seq);
          else setEmpty(true);
        })
        .catch(() => setEmpty(true));
    }
  }, [params, names]);

  return (
    <LiteShell>
      <div className="mx-auto max-w-2xl space-y-3 p-4" data-testid="lite-story-page">
        <div className="card">
          <div className="text-title text-text-0">{title || '今天全楼都在说这件事'}</div>
        </div>
        {empty && <div className="card text-text-1">今天还没有特别热闹的事，去地图上看看吧。</div>}
        {data && (
          <>
            <div className="card">
              <div className="mb-2 text-aux text-text-1">这句话，一五一十地传开了</div>
              {data.projections.slice(0, 3).map((p) => (
                <div key={p.memory_id} className="flex items-center gap-2 py-1">
                  <Avatar id={p.agent_id} name={nameOf(p.agent_id)} signatureColor={colorOf(p.agent_id)} size={24} />
                  <span className="text-body text-text-0">{nameOf(p.agent_id)}</span>
                  <span className="text-aux text-text-1">{p.is_witness ? '亲眼看见了' : '当时就听到了'}</span>
                </div>
              ))}
            </div>
            {data.chain.length > 0 && (
              <div className="card">
                <div className="mb-2 text-aux text-text-1">后来呢？</div>
                {data.chain.map((c) => (
                  <div key={c.dst_event_seq} className="flex items-center gap-2 border-b border-border py-2 last:border-0"
                    data-testid="lite-hop">
                    <Avatar id={c.teller_id} name={nameOf(c.teller_id)} signatureColor={colorOf(c.teller_id)} size={24} />
                    <span className="text-body text-text-0">{nameOf(c.teller_id)}</span>
                    <span className="text-text-1">告诉了</span>
                    <Avatar id={c.listener_id} name={nameOf(c.listener_id)} signatureColor={colorOf(c.listener_id)} size={24} />
                    <span className="text-body text-text-0">{nameOf(c.listener_id)}</span>
                    <span className="ml-auto rounded bg-bg-2 px-2 py-0.5 text-ts text-text-1">
                      {distortionPhrase(c.distortion)}
                    </span>
                  </div>
                ))}
              </div>
            )}
            {data.relation_changes.length > 0 && (
              <div className="card">
                <div className="mb-2 text-aux text-text-1">这件事之后，有些关系悄悄变了</div>
                {data.relation_changes.map((r, i) => (
                  <div key={i} className="py-1 text-body text-text-0">
                    {nameOf(r.a_id)} 和 {nameOf(r.b_id)} 之间，
                    {r.delta_affinity < 0 ? '多了一点隔阂' : '更近了一步'}。
                  </div>
                ))}
              </div>
            )}
          </>
        )}
        <div className="text-center text-aux">
          <Link to={`/lite/home${window.location.search.replace(/[?&]e=\d+/, '')}`} className="text-accent">← 回到地图</Link>
        </div>
      </div>
    </LiteShell>
  );
}
