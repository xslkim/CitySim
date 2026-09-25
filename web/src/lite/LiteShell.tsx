/** lite 观众版外壳（03 §1.1 呈现层减法；ui/web-lite 原型 topnav 形态；无调试态组件）。 */
import { NavLink } from 'react-router-dom';
import { simClockText } from '../components/common/AppShell';
import { useWorldStore } from '../stores/worldStore';

export default function LiteShell({ children }: { children: React.ReactNode }) {
  const snapshot = useWorldStore((s) => s.snapshot);
  const q = window.location.search;
  return (
    <div className="min-h-screen">
      <header className="flex h-12 items-center gap-4 border-b border-border bg-bg-1 px-4">
        <span className="text-title font-bold">
          <span className="text-accent">◆ WorldSim</span> · 404 公寓
        </span>
        <nav className="flex gap-1">
          <NavLink to={`/lite/home${q}`} className={({ isActive }) =>
            `rounded-card px-3 py-1 text-body ${isActive ? 'bg-bg-2 text-accent' : 'text-text-1'}`}>地图</NavLink>
          <NavLink to={`/lite/story${q}`} className={({ isActive }) =>
            `rounded-card px-3 py-1 text-body ${isActive ? 'bg-bg-2 text-accent' : 'text-text-1'}`}>故事</NavLink>
        </nav>
        <span className="ml-auto text-aux text-text-1" data-testid="lite-clock">
          {simClockText(snapshot?.sim_time ?? null, snapshot?.sim_day ?? null)}
        </span>
      </header>
      {children}
    </div>
  );
}
