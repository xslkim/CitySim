/** 涟漪 DAG SVG（02 §7.4 逐字：源 24px accent 脉冲 / 投影节点 12px 签名色 70% / 边 = 有向箭头
 *  第 1 手实线第 2 手起虚线透明度递减 / 失真标签边中点 11px 三档着色 / 后续事件菱形跳 timeline）。 */
import { useMemo } from 'react';
import { useNavigate } from 'react-router-dom';
import type { RippleData } from '../../proto/ripple';
import { BAND_COLOR_VAR, distortionBand, hopOpacity, layoutRipple } from '../../lib/rippleLayout';
import { displaySeq } from '../../lib/eventText';
import { fallbackColor } from '../../lib/portraits';

export default function RippleDag({ data, sourceGrade }: { data: RippleData; sourceGrade: string | null }) {
  const navigate = useNavigate();
  const { nodes, edges } = useMemo(() => layoutRipple(data), [data]);
  const nodePos = new Map(nodes.map((n) => [n.seq, n]));
  const height = Math.max(160, ...nodes.map((n) => n.y + 60));
  const pulseDuration = sourceGrade === 'A' ? '0.8s' : '1.5s'; // A 级脉冲加速（02 §7.6）

  return (
    <svg viewBox={`0 0 600 ${height}`} className="w-full" data-testid="ripple-dag">
      <defs>
        <marker id="arrow" markerWidth="6" markerHeight="6" refX="5" refY="3" orient="auto">
          <path d="M0,0 L6,3 L0,6" fill="none" stroke="var(--text-1)" strokeWidth={1} />
        </marker>
      </defs>
      {/* 传播边 */}
      {edges.map((e) => {
        const a = nodePos.get(e.fromSeq);
        const b = nodePos.get(e.toSeq);
        if (!a || !b) return null;
        const band = distortionBand(e.distortion);
        return (
          <g key={`${e.fromSeq}-${e.toSeq}`}>
            <line
              x1={a.x} y1={a.y} x2={b.x} y2={b.y}
              stroke={BAND_COLOR_VAR[band]}
              strokeWidth={1.5}
              strokeDasharray={e.hop >= 2 ? '4 3' : undefined}
              opacity={hopOpacity(e.hop)}
              markerEnd="url(#arrow)"
            />
            {e.distortion != null && (
              <text x={(a.x + b.x) / 2} y={(a.y + b.y) / 2 - 4} fontSize={11}
                fill={BAND_COLOR_VAR[band]} textAnchor="middle">
                失真 {(e.distortion * 100).toFixed(0)}%
              </text>
            )}
          </g>
        );
      })}
      {/* 源事件节点（24px accent 实心圆脉冲） */}
      {nodes.filter((n) => n.hop === 0).map((n) => (
        <g key={n.seq} transform={`translate(${n.x},${n.y})`}>
          <circle r={12} fill="var(--accent)">
            <animate attributeName="r" values="12;16;12" dur={pulseDuration} repeatCount="indefinite" />
          </circle>
          <text y={26} textAnchor="middle" fontSize={10} fill="var(--text-0)">{displaySeq(n.seq)}</text>
        </g>
      ))}
      {/* 投影/传播节点（12px 签名色 70% 透明） */}
      {nodes.filter((n) => n.hop > 0).map((n) => (
        <g key={n.seq} transform={`translate(${n.x},${n.y})`}
          onClick={() => navigate(`/timeline?focus=e${n.seq}`)} style={{ cursor: 'pointer' }}>
          <circle r={6} fill={fallbackColor(n.agentId)} opacity={0.7} />
          <text y={20} textAnchor="middle" fontSize={9} fill="var(--text-1)">
            {n.agentId} · hop{n.hop}
          </text>
        </g>
      ))}
      {/* 后续事件菱形（bg-2 + text-0 描边，跳 /timeline?focus=） */}
      {data.followups.map((f, i) => (
        <g key={f.seq} transform={`translate(${40 + i * 70},${height - 40})`}
          onClick={() => navigate(`/timeline?focus=e${f.seq}`)} style={{ cursor: 'pointer' }}>
          <rect x={-8} y={-8} width={16} height={16} transform="rotate(45)"
            fill="var(--bg-2)" stroke="var(--text-0)" strokeWidth={2} />
          <text y={22} textAnchor="middle" fontSize={9} fill="var(--text-1)">{displaySeq(f.seq)}</text>
        </g>
      ))}
    </svg>
  );
}
