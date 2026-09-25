/**
 * /lite/agent/:id 角色故事页（05 T-WEB-19；ui/web-lite/agent.html 原型形态：
 * hero 卡（头像/一句话人设/标签）+ 「TA 最近经历了什么」叙事时间线 + 「TA 的人际关系」人话卡 +
 * 双人亲近度走势）。数值全部降级为语言（narrativeMap）。
 */
import { useEffect, useMemo, useState } from 'react';
import { Link, useParams } from 'react-router-dom';
import { apiGet } from '../api/client';
import Avatar from '../components/common/Avatar';
import { agentDetailSchema, agentStateSchema, reflectionsSchema,
  type AgentDetail, type AgentState } from '../proto/agents';
import { envelopeEventsSchema } from '../proto';
import type { ObsEvent } from '../proto/event';
import { loadMapLayout, locationName } from '../lib/mapLayout';
import { useAgentsStore } from '../stores/agentsStore';
import { useWorldStore } from '../stores/worldStore';
import LiteShell from './LiteShell';
import { moodSentence, narrateEvent, relationPhrase } from './narrativeMap';

interface Edge { a: string; b: string; aff: number; ten: number; label: string[] }

export default function LiteAgentPage() {
  const { id = '' } = useParams();
  const profiles = useAgentsStore((s) => s.profiles);
  const snapshot = useWorldStore((s) => s.snapshot);
  const [detail, setDetail] = useState<AgentDetail | null>(null);
  const [state, setState] = useState<AgentState | null>(null);
  const [thoughts, setThoughts] = useState<string[]>([]);
  const [events, setEvents] = useState<ObsEvent[]>([]);
  const [edges, setEdges] = useState<Edge[]>([]);
  const [, setLayoutReady] = useState(false);

  useEffect(() => {
    loadMapLayout().then(() => setLayoutReady(true));
    if (!id) return;
    apiGet(`/api/agents/${id}`, agentDetailSchema).then((r) => setDetail(r.data)).catch(() => undefined);
    apiGet(`/api/agents/${id}/state`, agentStateSchema).then((r) => setState(r.data)).catch(() => undefined);
    apiGet(`/api/agents/${id}/reflections?limit=2`, reflectionsSchema)
      .then((r) => setThoughts(r.data.items.map((x) => x.content_display))).catch(() => undefined);
    apiGet(`/api/events?actor=${id}&limit=30`, envelopeEventsSchema)
      .then((r) => setEvents(r.data.items.filter((e) => e.payload.text_display).reverse().slice(0, 6)))
      .catch(() => undefined);
    apiGet(`/api/relations?agent=${id}`)
      .then((r) => setEdges((r.data as { items: Edge[] }).items.filter((e) => e.aff !== 0 || e.ten !== 0 || e.label.length).slice(0, 4)))
      .catch(() => undefined);
  }, [id]);

  const names = useMemo(() => new Map(profiles.map((p) => [p.id, p.name])), [profiles]);
  const nameOf = (x: string) => names.get(x) ?? x;
  const profile = profiles.find((p) => p.id === id);
  const current = snapshot?.agents.find((a) => a.id === id);

  if (!detail) return <LiteShell><div className="p-4 text-text-1">加载中…</div></LiteShell>;
  const pd = detail.persona_display;
  const colorName = ((pd.appearance as any)?.signature_color?.name) as string | undefined;

  return (
    <LiteShell>
      <div className="space-y-3 p-4" data-testid="lite-agent-page">
        <div className="card flex items-center gap-4">
          <Avatar id={id} name={detail.name} signatureColor={profile?.signature_color} size={72} />
          <div className="min-w-0">
            <div className="text-xl font-bold text-text-0">
              {detail.name}
              <span className="ml-2 text-aux font-normal text-text-1">
                {detail.age} 岁 · {detail.department ?? '—'} · 住 {detail.room_no ?? '—'}
              </span>
            </div>
            {pd.contrast_public && <div className="mt-1 text-body text-text-0">{pd.contrast_public}。</div>}
            <div className="mt-2 flex flex-wrap gap-2 text-aux">
              {pd.signature_quirk && <span className="rounded bg-bg-2 px-2 py-0.5 text-text-1">{pd.signature_quirk}</span>}
              {colorName && <span className="rounded bg-bg-2 px-2 py-0.5 text-text-1">签名色 · {colorName}</span>}
            </div>
          </div>
          <div className="ml-auto max-w-xs rounded-card bg-bg-2 p-3 text-body">
            她现在：{locationName(current?.location_id)}。
            <br />心情：<b className="text-positive">{moodSentence(state?.needs ?? null, state?.mood ?? null)}</b>
          </div>
        </div>

        <div className="grid grid-cols-2 gap-3">
          <div className="card">
            <div className="mb-2 text-title text-text-0">TA 最近经历了什么</div>
            {events.map((e) => {
              const n = narrateEvent(e, nameOf);
              return (
                <div key={e.seq} className="flex gap-2 border-b border-border py-2 last:border-0">
                  <span>{n.icon}</span>
                  <div className="text-body text-text-0">{n.text}</div>
                </div>
              );
            })}
            {thoughts.map((t, i) => (
              <div key={i} className="flex gap-2 border-b border-border py-2 last:border-0">
                <span>💭</span>
                <div className="text-body text-text-1">{t}</div>
              </div>
            ))}
          </div>
          <div className="card">
            <div className="mb-2 text-title text-text-0">TA 的人际关系</div>
            {edges.map((e) => {
              const ph = relationPhrase(e.label, e.aff, e.ten);
              return (
                <div key={`${e.a}-${e.b}`} className="flex items-center gap-2 border-b border-border py-2 last:border-0">
                  <Avatar id={e.b} name={nameOf(e.b)}
                    signatureColor={profiles.find((p) => p.id === e.b)?.signature_color} size={26} />
                  <div>
                    <div className="text-body text-text-0">
                      {nameOf(e.b)}
                      <span className={`ml-1 text-aux ${e.aff < 0 ? 'text-negative' : 'text-positive'}`}>
                        {e.aff < 0 ? '↘' : '↗'} {ph.phrase}
                      </span>
                    </div>
                  </div>
                  <span className="ml-auto rounded bg-bg-2 px-2 py-0.5 text-ts text-text-1">{ph.tag}</span>
                </div>
              );
            })}
          </div>
        </div>
        <div className="text-center text-aux">
          <Link to={`/lite/story${window.location.search}`} className="text-accent">去看今天的故事 →</Link>
        </div>
      </div>
    </LiteShell>
  );
}
