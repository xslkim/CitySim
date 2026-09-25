/**
 * 全局三栏 shell（03 §2.1）：顶导（Logo/六项/sim 时钟/压缩比/延迟指示条槽位/token 显示）
 * + 左栏（地点树/角色列表容器，LOD 徽标 ★/◐/○）+ 中栏 Outlet + 右栏上下文槽。
 * /health 默认不进导航，调试态开关（快捷键 d）下出现（03 §3.6）。
 */
import { useEffect } from 'react';
import { NavLink, Outlet, useLocation } from 'react-router-dom';
import { apiGet } from '../../api/client';
import { agentListSchema } from '../../proto/agents';
import { snapshotSchema } from '../../proto/snapshot';
import { useAgentsStore } from '../../stores/agentsStore';
import { useUiStore } from '../../stores/uiStore';
import { useWorldStore } from '../../stores/worldStore';
import { wsManager } from '../../ws/manager';
import AgentList from './AgentList';
import LatencyBar from './LatencyBar';
import LocationTree from '../map/LocationTree';

const NAV = [
  { to: '/map', label: '地图' },
  { to: '/timeline', label: '时间轴' },
  { to: '/ripple', label: '涟漪' },
  { to: '/relations', label: '关系' },
];

export function simClockText(simTime: string | null, simDay: number | null): string {
  if (!simTime) return '—';
  const d = new Date(simTime);
  const hh = String(d.getHours()).padStart(2, '0');
  const mm = String(d.getMinutes()).padStart(2, '0');
  return `Day ${simDay ?? '?'} ${hh}:${mm}`;
}

export default function AppShell() {
  const debug = useUiStore((s) => s.debug);
  const toggleDebug = useUiStore((s) => s.toggleDebug);
  const snapshot = useWorldStore((s) => s.snapshot);
  const location = useLocation();
  const isLite = location.pathname.startsWith('/lite');
  const token = new URLSearchParams(window.location.search).get('token') ?? '';

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'd' && !(e.target instanceof HTMLInputElement)) toggleDebug();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [toggleDebug]);

  // 首屏引导（03 §6.3：首屏只拉 /api/snapshot + 角色档案；增量走 WS）
  useEffect(() => {
    apiGet('/api/agents', agentListSchema)
      .then((r) => useAgentsStore.getState().setProfiles(r.data.items))
      .catch(() => undefined);
    apiGet('/api/snapshot', snapshotSchema)
      .then((r) => {
        useWorldStore.getState().setSnapshot(r.data);
        useWorldStore.getState().setWatermarkTick(r.meta.watermark_tick);
      })
      .catch(() => undefined);
    wsManager.start();
    return () => wsManager.stop();
  }, []);

  if (isLite) {
    // lite 观众版：呈现层减法（03 §1.1），无三栏调试壳
    return (
      <div className="min-h-screen">
        <Outlet />
      </div>
    );
  }

  return (
    <div className="flex h-screen flex-col">
      <header className="flex h-11 items-center gap-4 border-b border-border bg-bg-1 px-3">
        <span className="text-title font-bold text-accent">WorldSim</span>
        <nav className="flex gap-1">
          {NAV.map((n) => (
            <NavLink
              key={n.to}
              to={n.to}
              className={({ isActive }) =>
                `rounded-card px-2 py-1 text-body ${isActive ? 'bg-bg-2 text-text-0' : 'text-text-1'}`
              }
            >
              {n.label}
            </NavLink>
          ))}
          {debug && (
            <NavLink
              to="/health"
              className={({ isActive }) =>
                `rounded-card px-2 py-1 text-body ${isActive ? 'bg-bg-2 text-text-0' : 'text-text-1'}`
              }
            >
              健康度
            </NavLink>
          )}
          <NavLink to="/lite/home" className="rounded-card px-2 py-1 text-body text-text-1">
            观众版
          </NavLink>
        </nav>
        <div className="ml-auto flex items-center gap-3 text-aux text-text-1">
          <span data-testid="sim-clock">
            sim: {simClockText(snapshot?.sim_time ?? null, snapshot?.sim_day ?? null)}
          </span>
          <span>压缩比 {snapshot?.compression_ratio ?? '—'}×</span>
          <LatencyBar />
          {token && <span title="访问 token">token:{token.slice(0, 10)}…</span>}
        </div>
      </header>
      <div className="flex min-h-0 flex-1">
        <aside className="w-60 overflow-y-auto border-r border-border bg-bg-1 p-2" data-testid="left-pane">
          <LocationTree />
          <div className="mt-3 border-t border-border pt-2">
            <AgentList />
          </div>
        </aside>
        <main className="min-w-0 flex-1 overflow-y-auto p-2">
          <Outlet />
        </main>
        <aside className="w-[360px] overflow-y-auto border-l border-border bg-bg-1 p-2" data-testid="right-pane">
          <div id="context-pane" className="text-aux text-text-1">
            右栏上下文（页面装载）
          </div>
        </aside>
      </div>
    </div>
  );
}
