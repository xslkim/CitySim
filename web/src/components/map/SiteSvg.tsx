/**
 * 单 site SVG（03 §3.1：四 site tab 同屏只渲染一个；z-order 六层 02 §7.2；
 * 缩放 0.5×~4× 滚轮/拖拽/双击归位；名牌/气泡反缩放恒定字号；offsite 聚合区块角标 N-P1-9；
 * 调试叠加层 d 键：房间在场人数 + location_id tooltip）。
 */
import { useMemo, useRef, useState } from 'react';
import type { SnapshotAgent } from '../../proto/snapshot';
import { nodeRect, type Site } from '../../lib/mapLayout';
import { useUiStore } from '../../stores/uiStore';
import BubbleLayer, { type BubbleDialogue } from './BubbleLayer';
import Pawn from './Pawn';

interface Props {
  site: Site;
  agents: SnapshotAgent[];          // 本 site 的 agent（offsite 已聚合剔除）
  offsiteAgents?: SnapshotAgent[];  // site=offsite 时：驻留 NPC 列表
  dialogues: BubbleDialogue[];
  historical: boolean;
  names: Map<string, string>;
  signatureColors: Map<string, string | null>;
  onSelectAgent: (id: string) => void;
}

export const ZOOM_MIN = 0.5;
export const ZOOM_MAX = 4; // 03 §3.1

export default function SiteSvg({
  site, agents, offsiteAgents, dialogues, historical, names, signatureColors, onSelectAgent,
}: Props) {
  const debug = useUiStore((s) => s.debug);
  const follow = useUiStore((s) => s.followAgent);
  const [view, setView] = useState({ x: 0, y: 0, k: 1 });
  const drag = useRef<{ sx: number; sy: number; ox: number; oy: number } | null>(null);

  const positions = useMemo(() => {
    const m = new Map<string, { x: number; y: number }>();
    const perRoom = new Map<string, number>();
    for (const a of agents) {
      const rect = a.location_id ? nodeRect(site, a.location_id) : null;
      if (!rect) continue;
      const n = perRoom.get(a.location_id!) ?? 0;
      perRoom.set(a.location_id!, n + 1);
      m.set(a.id, {
        x: rect.x + rect.w / 2 + (n % 3) * 10 - 10,
        y: rect.y + rect.h / 2 + Math.floor(n / 3) * 10 - 6,
      });
    }
    return m;
  }, [agents, site]);

  const occupancy = useMemo(() => {
    const m = new Map<string, number>();
    for (const a of agents) {
      if (a.location_id) m.set(a.location_id, (m.get(a.location_id) ?? 0) + 1);
    }
    return m;
  }, [agents]);

  const nodes = [...(site.rooms ?? []), ...(site.commons ?? []), ...(site.zones ?? [])];
  const width = site.id === 'apt' ? 4 * 88 + 16 : 4 * 96 + 16;
  const height = site.id === 'apt' ? 6 * 72 + 60 : 3 * 72 + 16;

  return (
    <svg
      viewBox={`0 0 ${width} ${height}`}
      className="h-full w-full touch-none select-none"
      onWheel={(e) => {
        const k = Math.min(ZOOM_MAX, Math.max(ZOOM_MIN, view.k * (e.deltaY > 0 ? 0.9 : 1.1)));
        setView((v) => ({ ...v, k }));
      }}
      onPointerDown={(e) => {
        drag.current = { sx: e.clientX, sy: e.clientY, ox: view.x, oy: view.y };
      }}
      onPointerMove={(e) => {
        if (drag.current) {
          setView((v) => ({
            ...v,
            x: drag.current!.ox + (e.clientX - drag.current!.sx),
            y: drag.current!.oy + (e.clientY - drag.current!.sy),
          }));
        }
      }}
      onPointerUp={() => (drag.current = null)}
      onDoubleClick={() => setView({ x: 0, y: 0, k: 1 })}
      data-testid={`site-${site.id}`}
    >
      <g transform={`translate(${view.x},${view.y}) scale(${view.k})`}>
        {/* 1. 场景底图（z-order 02 §7.2） */}
        {site.type !== 'aggregate' ? (
          nodes.map((n) => {
            const rect = nodeRect(site, n.id);
            if (!rect) return null;
            return (
              <g key={n.id}>
                <rect
                  x={rect.x} y={rect.y} width={rect.w} height={rect.h}
                  fill="var(--bg-1)" stroke="var(--border)" rx={4}
                >
                  <title>{n.id}</title>
                </rect>
                <text x={rect.x + 4} y={rect.y + 12} fontSize={8} fill="var(--text-1)">
                  {n.name}
                  {debug && ` · ${n.id} · ${occupancy.get(n.id) ?? 0}人`}
                </text>
              </g>
            );
          })
        ) : (
          // offsite 聚合区块（03 §3.1 N-P1-9：人数角标；区块内无气泡与移动动画）
          <g data-testid="offsite-block">
            <rect x={8} y={8} width={width - 16} height={120} fill="var(--bg-1)"
              stroke="var(--border)" rx={8} />
            <text x={20} y={32} fontSize={12} fill="var(--text-0)">{site.name}</text>
            <text x={width - 44} y={32} fontSize={14} fill="var(--warn)" data-testid="offsite-badge">
              {offsiteAgents?.length ?? 0}
            </text>
            {(offsiteAgents ?? []).map((a, i) => (
              <text key={a.id} x={20 + (i % 6) * 56} y={56 + Math.floor(i / 6) * 18}
                fontSize={9} fill="var(--text-1)" onClick={() => onSelectAgent(a.id)}
                style={{ cursor: 'pointer' }}>
                {a.name}
              </text>
            ))}
          </g>
        )}
        {/* 4. 角色层 + 5. 气泡层 + 6. 名牌层（名牌在 Pawn 内最末绘制） */}
        {site.type !== 'aggregate' &&
          agents.map((a) => {
            const pos = positions.get(a.id);
            if (!pos) return null;
            return (
              <Pawn
                key={a.id}
                id={a.id}
                name={names.get(a.id) ?? a.name}
                signatureColor={signatureColors.get(a.id)}
                lod={a.lod}
                mood={a.mood}
                x={pos.x}
                y={pos.y}
                following={follow === a.id}
                onClick={() => onSelectAgent(a.id)}
              />
            );
          })}
        <BubbleLayer
          dialogues={dialogues.filter((d) => d.locationId && positions.has(d.participants[0]))}
          anchorOf={(id) => positions.get(id) ?? null}
          historical={historical}
        />
      </g>
    </svg>
  );
}
