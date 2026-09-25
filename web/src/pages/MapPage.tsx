/**
 * /map 空间地图页（05 T-WEB-13；03 §3.1/§2.2(a)/§4.2 scrub 历史重建）。
 * 布局：四 site tab + pawn/名牌/气泡 + 跟拍（右栏锁定摘要卡）+ 底部 ScrubBar +
 * 右栏（选中角色摘要卡 / 本地点·全局 EventStream）+ 顶部"今日热涟漪"卡片条（T-WEB-16 组件）。
 */
import { useEffect, useMemo, useState } from 'react';
import { createPortal } from 'react-dom';
import { Link, useNavigate } from 'react-router-dom';
import { apiGet } from '../api/client';
import EventStream from '../components/common/EventStream';
import Avatar from '../components/common/Avatar';
import ScrubBar from '../components/map/ScrubBar';
import SiteSvg from '../components/map/SiteSvg';
import TodayRipples from '../components/ripple/TodayRipples';
import { getLayout, loadMapLayout, locationName, siteOf, type Site } from '../lib/mapLayout';
import { snapshotSchema, type SnapshotData } from '../proto/snapshot';
import { useAgentsStore } from '../stores/agentsStore';
import { useTimelineStore } from '../stores/timelineStore';
import { useUiStore } from '../stores/uiStore';
import { useWorldStore } from '../stores/worldStore';

export default function MapPage() {
  const snapshot = useWorldStore((s) => s.snapshot);
  const profiles = useAgentsStore((s) => s.profiles);
  const events = useTimelineStore((s) => s.events);
  const debug = useUiStore((s) => s.debug);
  const follow = useUiStore((s) => s.followAgent);
  const setFollow = useUiStore((s) => s.setFollowAgent);
  const selectedAgent = useUiStore((s) => s.selectedAgent);
  const selectAgent = useUiStore((s) => s.selectAgent);
  const navigate = useNavigate();

  const [layout, setLayout] = useState(getLayout());
  const [siteId, setSiteId] = useState('apt');
  const [historical, setHistorical] = useState<SnapshotData | null>(null);

  useEffect(() => {
    loadMapLayout().then(setLayout);
  }, []);

  const view = historical ?? snapshot;
  const agents = view?.agents ?? [];

  // 跟拍：角色跨 site 自动切 tab（03 §3.1）
  useEffect(() => {
    if (!follow) return;
    const a = agents.find((x) => x.id === follow);
    const s = siteOf(a?.location_id);
    if (s && s !== siteId) setSiteId(s);
  }, [follow, agents, siteId]);

  const names = useMemo(
    () => new Map(profiles.map((p) => [p.id, p.name])),
    [profiles],
  );
  const signatureColors = useMemo(
    () => new Map(profiles.map((p) => [p.id, p.signature_color])),
    [profiles],
  );

  const onScrub = async (tick: number | null) => {
    if (tick === null) {
      setHistorical(null);
      return;
    }
    const { data } = await apiGet(`/api/snapshot?tick=${tick}`, snapshotSchema);
    setHistorical(data); // 历史静态重建（03 §4.2；不污染 worldStore 实时态）
  };

  if (!layout) return <div className="card text-text-1">地图布局加载中…</div>;
  const site: Site = layout.sites.find((s) => s.id === siteId) ?? layout.sites[0];
  const siteAgents = agents.filter((a) => siteOf(a.location_id) === site.id);
  const offsiteAgents = agents.filter((a) => a.location_id?.startsWith('home.'));
  const dialogues = (view?.active_dialogues ?? []).map((d) => ({
    eventSeq: d.event_seq,
    locationId: d.location_id,
    participants: d.participants,
    lines: d.lines,
  }));
  const selected = agents.find((a) => a.id === selectedAgent);

  const rightPane = document.getElementById('context-pane');

  return (
    <div data-testid="map-page">
      <TodayRipples onPick={(seq) => navigate(`/ripple/e${seq}`)} />
      <div className="mb-1 flex items-center gap-2">
        {layout.sites.map((s) => (
          <button
            key={s.id}
            type="button"
            className={`rounded-card px-2 py-1 text-body ${s.id === site.id ? 'bg-bg-2 text-accent' : 'text-text-1'}`}
            onClick={() => setSiteId(s.id)}
          >
            {s.name}
            {s.id === 'offsite' && (
              <span className="ml-1 rounded bg-bg-1 px-1 text-ts text-warn" data-testid="offsite-tab-badge">
                {offsiteAgents.length}
              </span>
            )}
          </button>
        ))}
        <label className="ml-auto flex items-center gap-1 text-aux text-text-1">
          <input
            type="checkbox"
            checked={follow !== null}
            onChange={(e) => setFollow(e.target.checked ? selectedAgent ?? agents[0]?.id ?? null : null)}
          />
          跟拍
        </label>
        {debug && <span className="text-aux text-warn">调试叠加层开（d 键）</span>}
        {historical && <span className="text-aux text-warn">历史 tick {historical.tick}（静态重建）</span>}
      </div>
      <div className="card h-[480px] p-1">
        <SiteSvg
          site={site}
          agents={siteAgents}
          offsiteAgents={offsiteAgents}
          dialogues={dialogues}
          historical={historical !== null}
          names={names}
          signatureColors={signatureColors}
          onSelectAgent={(id) => {
            selectAgent(id);
            if (follow) setFollow(id);
          }}
        />
      </div>
      <ScrubBar onScrub={onScrub} />
      {rightPane &&
        createPortal(
          selected ? (
            <div className="card" data-testid="agent-summary">
              <div className="flex items-center gap-2">
                <Avatar id={selected.id} name={selected.name}
                  signatureColor={signatureColors.get(selected.id)} size={32} />
                <div>
                  <div className="text-body text-text-0">
                    {selected.name} {selected.lod === 'star' ? '★' : selected.lod === 'secondary' ? '◐' : '○'}
                  </div>
                  <div className="text-aux text-text-1">
                    {locationName(selected.location_id)} · 情绪 {selected.mood ?? '—'}
                  </div>
                </div>
              </div>
              <Link to={`/agent/${selected.id}`} className="mt-2 block text-aux text-accent">
                完整详情 →
              </Link>
            </div>
          ) : (
            <EventStream events={events.slice(-100)} names={names} height={440} />
          ),
          rightPane,
        )}
    </div>
  );
}
