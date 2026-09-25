/**
 * /agent/:id 角色详情页（05 T-WEB-15；03 §3.3 七区块：人设卡/LOD/需求雷达/今日日程/
 * 最新反思/关系列表/周目标 + ⏱ 调试钩子）。/location/:id 地点详情页（EventStream + 在场角色）。
 */
import { useEffect, useState } from 'react';
import { Link, useParams } from 'react-router-dom';
import { apiGet } from '../api/client';
import NeedsRadar from '../components/agent/NeedsRadar';
import DebugSourcePopover from '../components/agent/DebugSourcePopover';
import EventStream from '../components/common/EventStream';
import Avatar from '../components/common/Avatar';
import {
  agentDetailSchema, agentScheduleSchema, agentStateSchema, reflectionsSchema,
  type AgentDetail, type AgentState, type Reflection,
} from '../proto/agents';
import { agentScheduleSchema as scheduleSchema } from '../proto/agents';
import { envelopeEventsSchema } from '../proto';
import type { ObsEvent } from '../proto/event';
import { renderEvent } from '../lib/eventText';
import { locationName } from '../lib/mapLayout';
import { useAgentsStore } from '../stores/agentsStore';
import { useWorldStore } from '../stores/worldStore';

const BIG5_LABELS: Record<string, string> = {
  openness: '开放', conscientiousness: '尽责', extraversion: '外向',
  agreeableness: '宜人', neuroticism: '神经质',
};

export default function AgentPage() {
  const { id = '' } = useParams();
  const profiles = useAgentsStore((s) => s.profiles);
  const [detail, setDetail] = useState<AgentDetail | null>(null);
  const [state, setState] = useState<AgentState | null>(null);
  const [reflections, setReflections] = useState<Reflection[]>([]);
  const [schedule, setSchedule] = useState<{ routine: Record<string, unknown>; events: ObsEvent[] } | null>(null);
  const [lodHistory, setLodHistory] = useState<ObsEvent[]>([]);
  const [edges, setEdges] = useState<{ a: string; b: string; aff: number; ten: number; label: string[] }[]>([]);

  useEffect(() => {
    if (!id) return;
    apiGet(`/api/agents/${id}`, agentDetailSchema).then((r) => setDetail(r.data)).catch(() => undefined);
    apiGet(`/api/agents/${id}/state`, agentStateSchema).then((r) => setState(r.data)).catch(() => undefined);
    apiGet(`/api/agents/${id}/reflections?limit=3`, reflectionsSchema)
      .then((r) => setReflections(r.data.items)).catch(() => undefined);
    apiGet(`/api/agents/${id}/schedule`, scheduleSchema).then((r) => setSchedule(r.data)).catch(() => undefined);
    apiGet(`/api/events?actor=${id}&type=agent.promoted,agent.demoted`, envelopeEventsSchema)
      .then((r) => setLodHistory(r.data.items)).catch(() => undefined);
    apiGet(`/api/relations?agent=${id}`)
      .then((r) => setEdges((r.data as { items: never[] }).items)).catch(() => undefined);
  }, [id]);

  if (!detail) return <div className="card text-text-1">角色加载中…</div>;
  const pd = detail.persona_display;
  const names = new Map(profiles.map((p) => [p.id, p.name]));
  const profile = profiles.find((p) => p.id === id);

  return (
    <div className="grid grid-cols-2 gap-2" data-testid="agent-page">
      {/* 人设卡（persona_display 六键，05 §3.6） */}
      <div className="card">
        <div className="flex items-center gap-2">
          <Avatar id={id} name={detail.name} signatureColor={profile?.signature_color} size={40} />
          <div>
            <div className="text-title text-text-0">{detail.name}</div>
            <div className="text-aux text-text-1">
              {detail.gender} · {detail.age} 岁 · {detail.room_no ?? '—'} · {detail.department ?? '—'} {detail.job_title ?? ''}
            </div>
          </div>
          <span className="ml-auto text-title">
            {detail.lod === 'star' ? '★' : detail.lod === 'secondary' ? '◐' : '○'}
          </span>
        </div>
        <div className="mt-2 space-y-1 text-aux">
          {Object.entries(pd.big_five ?? {}).map(([k, v]) => (
            <div key={k} className="flex items-center gap-2">
              <span className="w-10 text-text-1">{BIG5_LABELS[k] ?? k}</span>
              <div className="h-1.5 flex-1 rounded bg-bg-0">
                <div className="h-1.5 rounded bg-accent" style={{ width: `${v}%` }} />
              </div>
              <span className="w-8 text-right text-text-0">{v}</span>
            </div>
          ))}
          {pd.signature_quirk && <div className="text-text-1">标志：{pd.signature_quirk}</div>}
          {pd.contrast_public && <div className="text-text-1">公开面：{pd.contrast_public}</div>}
          {pd.speech_style_public && <div className="text-text-1">语言风格：{pd.speech_style_public}</div>}
        </div>
        {lodHistory.length > 0 && (
          <div className="mt-2 border-t border-border pt-1 text-ts text-text-1">
            升降格：{lodHistory.map((e) => `${e.type === 'agent.promoted' ? '★' : '○'}e${e.seq}`).join(' → ')}
          </div>
        )}
      </div>

      {/* 需求六维雷达 + ⏱ */}
      <div className="card">
        <div className="mb-1 flex items-center text-aux text-text-1">
          需求六维 <DebugSourcePopover agentId={id} />
        </div>
        <NeedsRadar needs={state?.needs ?? null} />
      </div>

      {/* 今日日程 */}
      <div className="card">
        <div className="mb-1 text-aux text-text-1">今日日程（sim）</div>
        <div className="text-aux text-text-0">
          {JSON.stringify((schedule?.routine as any)?.regular ?? {}, null, 0).replace(/[{}"]/g, '')}
        </div>
        <div className="mt-1 space-y-0.5 text-aux">
          {(schedule?.events ?? []).map((e) => (
            <div key={e.seq} className="flex gap-1">
              <span className="text-text-1">{e.sim_time.slice(11, 16)}</span>
              <span className="truncate text-text-0">{renderEvent(e, (x) => names.get(x) ?? x).text}</span>
            </div>
          ))}
        </div>
      </div>

      {/* 最新反思（仅展示通道，05 §3.2） */}
      <div className="card">
        <div className="mb-1 text-aux text-text-1">最新反思</div>
        {reflections.length === 0 && <div className="text-aux text-text-1">暂无</div>}
        {reflections.map((r) => (
          <blockquote key={r.memory_id} className="mb-1 border-l-2 border-border pl-2 text-body text-text-0">
            {r.content_display}
            <div className="text-ts text-text-1">{r.sim_time.slice(0, 16).replace('T', ' ')}</div>
          </blockquote>
        ))}
      </div>

      {/* 关系列表（|affinity| 排序，边色=张力） */}
      <div className="card">
        <div className="mb-1 flex items-center text-aux text-text-1">
          关系列表 <DebugSourcePopover agentId={id} />
        </div>
        {edges.map((e) => (
          <Link key={`${e.a}-${e.b}`} to={`/relations?pair=${e.a},${e.b}`}
            className="flex items-center gap-2 py-0.5 text-aux">
            <span className="text-text-0">→{names.get(e.b) ?? e.b}</span>
            <span className={e.aff >= 0 ? 'text-positive' : 'text-negative'}>aff {e.aff > 0 ? '+' : ''}{e.aff}</span>
            <span className={e.ten > 50 ? 'text-warn' : 'text-text-1'}>ten {e.ten}</span>
            {e.label.map((l) => <span key={l} className="rounded bg-bg-2 px-1 text-ts">{l}</span>)}
          </Link>
        ))}
      </div>

      {/* 周目标 + 受阻计数 + 挫败进度条 */}
      <div className="card">
        <div className="mb-1 text-aux text-text-1">周目标</div>
        {(state?.goals ?? []).map((g) => (
          <div key={g.goal} className="mb-1">
            <div className="text-body text-text-0">
              {g.goal}（受阻×{g.blocked_count}）
            </div>
            <div className="h-1.5 rounded bg-bg-0">
              <div className="h-1.5 rounded bg-warn" style={{ width: `${Math.min(100, g.frustration)}%` }} />
            </div>
          </div>
        ))}
        <div className="text-ts text-text-1">挫败值 {state?.frustration ?? 0}</div>
      </div>
    </div>
  );
}

/** /location/:id（03 §2.2/§1.2：该地点事件流 + 在场角色；上下文 query 保留） */
export function LocationPageBody({ id }: { id: string }) {
  const snapshot = useWorldStore((s) => s.snapshot);
  const profiles = useAgentsStore((s) => s.profiles);
  const [events, setEvents] = useState<ObsEvent[]>([]);
  useEffect(() => {
    apiGet(`/api/events?location=${encodeURIComponent(id)}&limit=200`, envelopeEventsSchema)
      .then((r) => setEvents(r.data.items)).catch(() => undefined);
  }, [id]);
  const present = (snapshot?.agents ?? []).filter((a) => a.location_id === id);
  const names = new Map(profiles.map((p) => [p.id, p.name]));
  return (
    <div data-testid="location-page">
      <div className="card mb-2">
        <div className="text-title text-text-0">{locationName(id)}</div>
        <div className="text-aux text-text-1">{id} · 在场 {present.length} 人</div>
        <div className="mt-1 flex gap-2">
          {present.map((a) => (
            <Link key={a.id} to={`/agent/${a.id}`} className="flex items-center gap-1 text-aux text-text-0">
              <Avatar id={a.id} name={a.name} size={18}
                signatureColor={profiles.find((p) => p.id === a.id)?.signature_color} />
              {a.name}
            </Link>
          ))}
        </div>
      </div>
      <EventStream events={events} names={names} height={480} />
    </div>
  );
}
