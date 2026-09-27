/**
 * /lite/home（05 T-WEB-19 追剧式重构：氛围 Hero + 夜景亮灯地图 + 真实台词气泡 + 居民状态横条）。
 * 呈现层减法：无地点树/无调试态/无延迟条/无压缩比；气泡=正在进行的对话（逐句播放真实台词）。
 */
import { useEffect, useMemo, useState } from 'react';
import { Link } from 'react-router-dom';
import { apiGet } from '../api/client';
import Avatar from '../components/common/Avatar';
import BubbleLayer from '../components/map/BubbleLayer';
import { ACTIVITY_LABEL } from '../components/map/NowPanel';
import { getLayout, tryLoadMapLayout, nodeRect, spreadOffsets, locationName, type Site } from '../lib/mapLayout';
import { envelopeEventsSchema } from '../proto';
import type { ObsEvent } from '../proto/event';
import type { SnapshotAgent } from '../proto/snapshot';
import { rippleTodaySchema } from '../proto/ripple';
import { useAgentsStore } from '../stores/agentsStore';
import { useRippleStore } from '../stores/rippleStore';
import { useWorldStore } from '../stores/worldStore';
import { FALLBACK_BATCH_MESSAGE, partitionFeed } from '../lib/fallbackText';
import { moodSentence, narrateEvent } from './narrativeMap';
import LiteShell from './LiteShell';

/** 时段（snapshot.sim_time 本地小时）→ 称谓与场景氛围 */
function dayPeriod(hour: number): string {
  if (hour >= 22 || hour < 6) return '深夜';
  if (hour < 8) return '清晨';
  if (hour < 11) return '上午';
  if (hour < 14) return '午后';
  if (hour < 18) return '下午';
  return '夜晚';
}

function isNight(hour: number): boolean {
  return hour >= 19 || hour < 6;
}

/** Hero 环境文案：对话 > 深夜熄灯 > 深夜亮窗 > 日间 */
function ambientLine(dialogueCount: number, litWindows: number, night: boolean): string {
  if (dialogueCount > 0) return '现在正有人在说话——凑近听听。';
  if (night && litWindows === 0) return '灯一盏盏灭了，404 公寓安静下来。';
  if (night) return `夜色落在 404 公寓，还剩 ${litWindows} 扇窗亮着。`;
  return '404 公寓的一天正在进行。';
}

function LiteMap({ site, night, onPick }: { site: Site; night: boolean; onPick: (id: string) => void }) {
  const snapshot = useWorldStore((s) => s.snapshot);
  const agents = (snapshot?.agents ?? []).filter((a) =>
    site.id === 'apt' ? a.location_id?.startsWith('apt.') : a.location_id?.startsWith('corp.'));
  const dialogues = snapshot?.active_dialogues ?? [];
  const talkingPairs = new Set(dialogues.flatMap((d) => d.participants));
  // T-ITER2-06：同房 pawn 分组 → 径向散开（叠名/叠头像修复）；名牌沿房间底边排开不再叠字
  const placed = useMemo(() => {
    const byRoom = new Map<string, typeof agents>();
    for (const a of agents) {
      const loc = a.location_id ?? '';
      if (!byRoom.has(loc)) byRoom.set(loc, []);
      byRoom.get(loc)!.push(a);
    }
    const out = new Map<string, { x: number; y: number; nameX: number; nameY: number; small: boolean }>();
    for (const [loc, list] of byRoom) {
      const rect = loc ? nodeRect(site, loc) : null;
      if (!rect) continue;
      const small = rect.h < 50;  // 公共区顶带 h=40：小头像、不画名牌（与房间名叠字修复）
      const offs = spreadOffsets(list.length).map((o) => small
        ? { dx: Math.round(o.dx * 0.55), dy: Math.round(o.dy * 0.3) }
        : o);
      list.forEach((a, i) => {
        const slot = Math.min(i, 3);  // 名牌底行最多 4 槽（fontSize 9 三人名不叠）
        const nameX = rect.x + rect.w / 2 + (slot - (Math.min(list.length, 4) - 1) / 2) * 26;
        out.set(a.id, {
          x: rect.x + rect.w / 2 + offs[i].dx,
          y: rect.y + rect.h / 2 + offs[i].dy + (small ? 4 : 0),
          nameX, nameY: rect.y + rect.h - 4,
          small,
        });
      });
    }
    return out;
  }, [agents, site]);

  const litRooms = useMemo(() => {
    const s = new Set<string>();
    for (const a of agents) if (a.location_id) s.add(a.location_id);
    return s;
  }, [agents]);

  const anchorOf = (agentId: string) => {
    const p = placed.get(agentId);
    return p ? { x: p.x, y: p.y } : null;
  };
  const bubbleDialogues = dialogues.map((d) => ({
    eventSeq: d.event_seq, locationId: d.location_id, participants: d.participants, lines: d.lines,
  }));

  const roomFill = (id: string) => {
    if (!night) return 'var(--bg-2)';
    return litRooms.has(id) ? 'rgba(255,209,102,0.16)' : '#252438';
  };
  const roomStroke = (id: string) => {
    if (!night) return 'var(--border)';
    return litRooms.has(id) ? 'rgba(255,209,102,0.55)' : '#33324A';
  };

  return (
    <svg viewBox="-4 -16 448 500" className="h-full w-full">
      <defs>
        <linearGradient id="lite-night-sky" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stopColor="#3B3A55" />
          <stop offset="100%" stopColor="#23222F" />
        </linearGradient>
      </defs>
      <rect x={-4} y={-16} width={448} height={500} rx={8}
        fill={night ? 'url(#lite-night-sky)' : 'var(--bg-0)'} />
      {night && (
        <g>
          <circle cx={412} cy={-4} r={9} fill="#F2E6E4" opacity={0.9} />
          <circle cx={408} cy={-7} r={2} fill="#D9CFCB" opacity={0.58} />
        </g>
      )}
      {[...(site.rooms ?? []), ...(site.commons ?? [])].map((n) => {
        const rect = nodeRect(site, n.id);
        if (!rect) return null;
        return (
          <g key={n.id}>
            <rect x={rect.x} y={rect.y} width={rect.w} height={rect.h} rx={6}
              fill={roomFill(n.id)} stroke={roomStroke(n.id)} />
            <text x={rect.x + 5} y={rect.y + 14} fontSize={10}
              fill={night ? 'rgba(230,233,239,0.72)' : 'var(--text-1)'}>{n.name}</text>
          </g>
        );
      })}
      {agents.map((a) => {
        const pos = placed.get(a.id);
        if (!pos) return null;
        const { x, y, nameX, nameY, small } = pos;
        const talking = talkingPairs.has(a.id);
        const r = small ? 10 : 14;
        return (
          <g key={a.id} transform={`translate(${x},${y})`} onClick={() => onPick(a.id)}
            style={{ cursor: 'pointer' }} data-testid={`lite-pawn-${a.id}`}>
            <title>{a.name}</title>
            {talking && (
              <circle r={r + 3} fill="none" stroke="var(--accent)" strokeWidth={1.5} opacity={0.7}>
                <animate attributeName="r" values={`${r + 1};${r + 5};${r + 1}`} dur="2s" repeatCount="indefinite" />
                <animate attributeName="opacity" values="0.7;0.2;0.7" dur="2s" repeatCount="indefinite" />
              </circle>
            )}
            <circle r={r} fill="var(--bg-1)"
              stroke={talking ? 'var(--accent)' : night ? 'rgba(255,209,102,0.4)' : 'var(--border)'}
              strokeWidth={talking ? 2 : 1} />
            <foreignObject x={-r + 2} y={-r + 2} width={r * 2 - 4} height={r * 2 - 4}>
              <Avatar id={a.id} name={a.name} size={r * 2 - 4} />
            </foreignObject>
            {!small && (
              <text x={nameX - x} y={nameY - y} textAnchor="middle" fontSize={9}
                fill={night ? '#E6E9EF' : 'var(--text-0)'}>{a.name}</text>
            )}
          </g>
        );
      })}
      {/* 真实台词气泡：逐句播放（客户端倍率节拍权威，06 §2） */}
      <BubbleLayer dialogues={bubbleDialogues} anchorOf={anchorOf} historical={false} />
    </svg>
  );
}

/** 底部居民横条卡：头像 + 位置/活动 + 心情语句（数值降级为语言） */
function ResidentChip({ agent, nameOf, signatureColor }: {
  agent: SnapshotAgent; nameOf: (id: string) => string; signatureColor?: string | null;
}) {
  const act = agent.activity ? ACTIVITY_LABEL[agent.activity] : undefined;
  return (
    <Link
      to={`/lite/agent/${agent.id}${window.location.search}`}
      className="card flex min-w-[168px] items-center gap-2.5 hover:border-accent"
      data-testid={`lite-resident-${agent.id}`}
    >
      <Avatar id={agent.id} name={agent.name} signatureColor={signatureColor} size={40} />
      <div className="min-w-0">
        <div className="text-body font-bold text-text-0">
          {agent.name}
          {agent.lod === 'star' && <span className="ml-1 text-accent">★</span>}
        </div>
        <div className="truncate text-aux text-text-1">
          {agent.location_id ? locationName(agent.location_id) : '—'}
          {act ? ` · ${act}` : ''}
        </div>
        <div className="truncate text-aux text-text-1">{moodSentence(agent.needs, agent.mood)}</div>
      </div>
    </Link>
  );
}

export default function LiteHomePage() {
  const [layout, setLayout] = useState(getLayout());
  const [tab, setTab] = useState('apt');
  const [feed, setFeed] = useState<ObsEvent[]>([]);
  const [fallbackCount, setFallbackCount] = useState(0);  // T-ITER2-04③ 兜底事件计数（降采样一张卡）
  const profiles = useAgentsStore((s) => s.profiles);
  const snapshot = useWorldStore((s) => s.snapshot);
  const today = useRippleStore((s) => s.today);
  const setToday = useRippleStore((s) => s.setToday);
  const names = useMemo(() => new Map(profiles.map((p) => [p.id, p.name])), [profiles]);
  const nameOf = (id: string) => names.get(id) ?? id;

  useEffect(() => {
    tryLoadMapLayout().then(setLayout);
    apiGet('/api/ripple/today', rippleTodaySchema).then((r) => setToday(r.data)).catch(() => undefined);
    apiGet('/api/events?limit=30', envelopeEventsSchema)
      .then((r) => {
        const [real, nFallback] = partitionFeed(r.data.items.filter((e) => e.payload.text_display));
        setFeed(real);  // R1 #5 默认倒序：不再 reverse；T-ITER2-04③ 兜底事件不进 feed
        setFallbackCount(nFallback);
      })
      .catch(() => undefined);
  }, [setToday]);

  if (!layout) return <LiteShell><div className="p-4 text-text-1">加载中…</div></LiteShell>;
  const site = layout.sites.find((s) => s.id === tab)!;

  const simDate = snapshot?.sim_time ? new Date(snapshot.sim_time) : null;
  const hour = simDate ? simDate.getHours() : 12;
  const night = isNight(hour);
  const clock = simDate
    ? `${String(simDate.getHours()).padStart(2, '0')}:${String(simDate.getMinutes()).padStart(2, '0')}`
    : '--:--';
  const dialogues = snapshot?.active_dialogues ?? [];
  const aptAgents = (snapshot?.agents ?? []).filter((a) => a.location_id?.startsWith('apt.'));
  const litWindows = new Set(aptAgents.map((a) => a.location_id)).size;
  const residents = (snapshot?.agents ?? []).filter((a) => a.lod !== 'background');
  // 安静时段兜底：最新一条心事（agent.reflection 有 text_display 才进 feed）；与头条看点同事件时不重复展示
  const topStorySeq = today?.items?.[0]?.event.seq;
  const quietThought = dialogues.length === 0
    ? feed.find((e) => e.type === 'agent.reflection' && e.seq !== topStorySeq) ?? null
    : null;

  return (
    <LiteShell>
      <div className="space-y-3 p-4">
        {/* Hero 氛围条：第 N 天 · 时段 · 环境文案 */}
        <div
          className="card flex items-center gap-4"
          style={{
            background: night
              ? 'linear-gradient(135deg, #2A2940 0%, #3B3A55 60%, #4A4460 100%)'
              : 'linear-gradient(135deg, var(--bg-1) 0%, var(--bg-2) 100%)',
          }}
          data-testid="lite-hero"
        >
          <div>
            <div className="text-xl font-bold text-text-0">
              第 {snapshot?.sim_day ?? '—'} 天 · {dayPeriod(hour)} {clock}
            </div>
            <div className="mt-0.5 text-body text-text-1">
              {ambientLine(dialogues.length, litWindows, night)}
            </div>
          </div>
          {night && <span className="ml-auto text-2xl" aria-hidden>🌙</span>}
        </div>

        <div className="grid grid-cols-3 gap-3">
          <div className="card col-span-2 flex flex-col">
            <div className="mb-2 flex items-center gap-2">
              {layout.sites.slice(0, 2).map((s) => (
                <button key={s.id} type="button"
                  className={`rounded-card px-3 py-1 text-body ${tab === s.id ? 'bg-bg-2 text-accent' : 'text-text-1'}`}
                  onClick={() => setTab(s.id)}>
                  {s.name === '单身公寓' ? '公寓' : s.name}
                </button>
              ))}
              <span className="ml-auto text-aux text-text-1">点角色头像看 TA 的故事</span>
            </div>
            <div className="min-h-0 flex-1" data-testid="lite-map" style={{ height: 'min(66vh, 640px)' }}>
              <LiteMap site={site} night={night && tab === 'apt'}
                onPick={(id) => (window.location.href = `/lite/agent/${id}${window.location.search}`)} />
            </div>
          </div>

          <div className="space-y-3 overflow-y-auto" data-testid="lite-feed">
            {/* 正在发生：真实台词引用，非单行摘要 */}
            {dialogues.length > 0 && (
              <div className="space-y-2">
                <div className="text-title text-text-0">正在发生</div>
                {dialogues.map((d) => (
                  <div key={d.event_seq} className="card" data-testid="lite-live-dialogue">
                    <div className="text-aux text-text-1">
                      💬 {d.participants.map(nameOf).join(' × ')}
                      {d.location_id ? ` · ${locationName(d.location_id)}` : ''}
                    </div>
                    {d.lines.length === 0 && (
                      <div className="mt-1.5 text-body text-text-1">刚刚碰面，正要开口…</div>
                    )}
                    {d.lines.slice(-2).map((l, i) => (
                      <div key={i} className="mt-1.5 flex items-start gap-2">
                        <Avatar id={l.speaker} name={nameOf(l.speaker)}
                          signatureColor={profiles.find((p) => p.id === l.speaker)?.signature_color}
                          size={22} />
                        <div className="rounded-card bg-bg-2 px-2 py-1 text-body text-text-0">
                          {l.text_display}
                        </div>
                      </div>
                    ))}
                  </div>
                ))}
              </div>
            )}

            {/* 安静时段：心事卡撑场（不大留白） */}
            {quietThought && (() => {
              const qn = narrateEvent(quietThought, nameOf);
              return (
              <div className="card" data-testid="lite-quiet-thought"
                style={{ background: 'linear-gradient(135deg, var(--bg-1), #23222F)' }}>
                <div className="text-aux text-text-1">💭 此刻的心事</div>
                <div className="mt-2 text-title text-text-0">{qn.text}</div>
                <div className="mt-2 flex items-center gap-2 text-aux text-text-1">
                  {qn.participants.map((id) => (
                    <span key={id} className="flex items-center gap-1">
                      <Avatar id={id} name={nameOf(id)}
                        signatureColor={profiles.find((p) => p.id === id)?.signature_color} size={20} />
                      {nameOf(id)}
                    </span>
                  ))}
                </div>
              </div>
              );
            })()}

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
            {fallbackCount > 0 && (  // T-ITER2-04③：同一拍多条兜底降采样为 ≤1 张人话卡
              <div className="card text-body text-text-1" data-testid="lite-feed-fallback">
                {FALLBACK_BATCH_MESSAGE}
              </div>
            )}
          </div>
        </div>

        {/* 底部居民横条：头像 + 位置/活动 + 心情 */}
        <div>
          <div className="mb-2 text-title text-text-0">公寓居民</div>
          <div className="flex gap-2 overflow-x-auto pb-1" data-testid="lite-residents">
            {residents.map((a) => (
              <ResidentChip key={a.id} agent={a} nameOf={nameOf}
                signatureColor={profiles.find((p) => p.id === a.id)?.signature_color} />
            ))}
          </div>
        </div>
      </div>
    </LiteShell>
  );
}
