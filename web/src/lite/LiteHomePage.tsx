/**
 * /lite/home（05 T-WEB-19；ui/web-lite/index.html 原型形态：地图（公寓/公司 tab）+ 今日看点叙事卡片）。
 * 呈现层减法：无地点树/无调试态/无延迟条/无压缩比；气泡=正在进行的对话。
 */
import { useEffect, useMemo, useState } from 'react';
import { Link } from 'react-router-dom';
import { apiGet } from '../api/client';
import Avatar from '../components/common/Avatar';
import { getLayout, loadMapLayout, nodeRect, type Site } from '../lib/mapLayout';
import { envelopeEventsSchema } from '../proto';
import type { ObsEvent } from '../proto/event';
import { rippleTodaySchema } from '../proto/ripple';
import { useAgentsStore } from '../stores/agentsStore';
import { useRippleStore } from '../stores/rippleStore';
import { useWorldStore } from '../stores/worldStore';
import { narrateEvent } from './narrativeMap';
import LiteShell from './LiteShell';

function LiteMap({ site, onPick }: { site: Site; onPick: (id: string) => void }) {
  const snapshot = useWorldStore((s) => s.snapshot);
  const agents = (snapshot?.agents ?? []).filter((a) =>
    site.id === 'apt' ? a.location_id?.startsWith('apt.') : a.location_id?.startsWith('corp.'));
  const dialogues = snapshot?.active_dialogues ?? [];
  const talkingPairs = new Set(dialogues.flatMap((d) => d.participants));
  return (
    <svg viewBox="0 0 400 520" className="h-full w-full">
      {(site.rooms ?? []).map((r) => {
        const rect = nodeRect(site, r.id)!;
        return (
          <g key={r.id}>
            <rect x={rect.x} y={rect.y} width={rect.w} height={rect.h} rx={6}
              fill="var(--bg-2)" stroke="var(--border)" />
            <text x={rect.x + 5} y={rect.y + 14} fontSize={10} fill="var(--text-1)">{r.name}</text>
          </g>
        );
      })}
      {(site.commons ?? []).map((c) => {
        const rect = nodeRect(site, c.id);
        if (!rect) return null;
        return (
          <g key={c.id}>
            <rect x={rect.x} y={rect.y} width={rect.w} height={rect.h} rx={6}
              fill="var(--bg-2)" stroke="var(--border)" />
            <text x={rect.x + 5} y={rect.y + 14} fontSize={10} fill="var(--text-1)">{c.name}</text>
          </g>
        );
      })}
      {agents.map((a, i) => {
        const rect = a.location_id ? nodeRect(site, a.location_id) : null;
        if (!rect) return null;
        const x = rect.x + rect.w / 2 + (i % 2) * 12 - 6;
        const y = rect.y + rect.h / 2 + 6;
        return (
          <g key={a.id} transform={`translate(${x},${y})`} onClick={() => onPick(a.id)}
            style={{ cursor: 'pointer' }} data-testid={`lite-pawn-${a.id}`}>
            <circle r={9} fill="var(--bg-2)" stroke={talkingPairs.has(a.id) ? 'var(--accent)' : 'var(--border)'}
              strokeWidth={talkingPairs.has(a.id) ? 2 : 1} />
            <foreignObject x={-8} y={-8} width={16} height={16}>
              <Avatar id={a.id} name={a.name} size={16} />
            </foreignObject>
            {talkingPairs.has(a.id) && (
              <text y={-12} textAnchor="middle" fontSize={10} fill="var(--accent)">💬</text>
            )}
            <text y={20} textAnchor="middle" fontSize={9} fill="var(--text-0)">{a.name}</text>
          </g>
        );
      })}
    </svg>
  );
}

export default function LiteHomePage() {
  const [layout, setLayout] = useState(getLayout());
  const [tab, setTab] = useState('apt');
  const [feed, setFeed] = useState<ObsEvent[]>([]);
  const profiles = useAgentsStore((s) => s.profiles);
  const today = useRippleStore((s) => s.today);
  const setToday = useRippleStore((s) => s.setToday);
  const names = useMemo(() => new Map(profiles.map((p) => [p.id, p.name])), [profiles]);
  const nameOf = (id: string) => names.get(id) ?? id;

  useEffect(() => {
    loadMapLayout().then(setLayout);
    apiGet('/api/ripple/today', rippleTodaySchema).then((r) => setToday(r.data)).catch(() => undefined);
    apiGet('/api/events?limit=30', envelopeEventsSchema)
      .then((r) => setFeed(r.data.items.filter((e) => e.payload.text_display).reverse()))
      .catch(() => undefined);
  }, [setToday]);

  if (!layout) return <LiteShell><div className="p-4 text-text-1">加载中…</div></LiteShell>;
  const site = layout.sites.find((s) => s.id === tab)!;

  return (
    <LiteShell>
      <div className="grid grid-cols-3 gap-3 p-4">
        <div className="card col-span-2 flex flex-col">
          <div className="mb-2 flex items-center gap-2">
            {layout.sites.slice(0, 2).map((s) => (
              <button key={s.id} type="button"
                className={`rounded-card px-3 py-1 text-body ${tab === s.id ? 'bg-bg-2 text-accent' : 'text-text-1'}`}
                onClick={() => setTab(s.id)}>
                {s.name === '单身公寓' ? '公寓' : s.name}
              </button>
            ))}
            <span className="ml-auto text-aux text-text-1">气泡 = 正在进行的对话</span>
          </div>
          <div className="min-h-0 flex-1" data-testid="lite-map">
            <LiteMap site={site} onPick={(id) => (window.location.href = `/lite/agent/${id}${window.location.search}`)} />
          </div>
        </div>
        <div className="space-y-3 overflow-y-auto" data-testid="lite-feed">
          <div className="text-title text-text-0">今日看点</div>
          {(today?.items ?? []).map((it) => {
            const n = narrateEvent(it.event, nameOf);
            return (
              <Link key={it.event.seq} to={`/lite/story?e=${it.event.seq}${window.location.search ? '&' + window.location.search.slice(1) : ''}`}
                className="card block hover:border-accent" data-testid="lite-story-card">
                <div className="flex items-center gap-2 text-aux">
                  <span className="rounded bg-bg-2 px-2 py-0.5 text-warn">{n.icon} {n.tag}</span>
                </div>
                <div className="mt-1 text-body text-text-0">{n.text}</div>
                <div className="mt-2 flex items-center gap-1">
                  {n.participants.map((id) => (
                    <Avatar key={id} id={id} name={nameOf(id)}
                      signatureColor={profiles.find((p) => p.id === id)?.signature_color} size={22} />
                  ))}
                </div>
              </Link>
            );
          })}
          {feed.slice(0, 8).map((e) => {
            const n = narrateEvent(e, nameOf);
            return (
              <div key={e.seq} className="card">
                <div className="text-aux text-text-1">{n.icon} {n.tag}</div>
                <div className="mt-1 text-body text-text-0">{n.text}</div>
              </div>
            );
          })}
        </div>
      </div>
    </LiteShell>
  );
}
