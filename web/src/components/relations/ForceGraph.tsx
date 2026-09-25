/** ECharts force+timeline 全网演化（03 §3.5：钉住布局——播放期间节点锁死只变边；
 *  边增删/粗细 300ms 缓动；新边 0 宽生长 500ms、断边闪烁 2 次消失，02 §7.5）。 */
import * as echarts from 'echarts';
import { useEffect, useRef } from 'react';
import type { RelationSnapshots } from '../../proto/relations';
import { edgeColor, edgeWidth } from '../../lib/relationsLayout';
import { fallbackColor } from '../../lib/portraits';

interface Props {
  snapshots: RelationSnapshots['items'];
  agents: { id: string; name: string; lod: string | null }[];
}

export default function ForceGraph({ snapshots, agents }: Props) {
  const ref = useRef<HTMLDivElement>(null);
  const chartRef = useRef<echarts.ECharts | null>(null);
  const fixedPos = useRef<Map<string, { x: number; y: number }>>(new Map());

  useEffect(() => {
    if (!ref.current || !snapshots.length) return;
    const chart = chartRef.current ?? echarts.init(ref.current, undefined, { renderer: 'svg' });
    chartRef.current = chart;
    const days = snapshots.map((s) => s.sim_day);

    // 钉住布局：首帧 force 落定后锁死节点坐标（演化期间只变边不变位，02 §7.5）
    if (!fixedPos.current.size) {
      agents.forEach((a, i) => {
        const angle = (Math.PI * 2 * i) / agents.length;
        fixedPos.current.set(a.id, { x: 300 + 220 * Math.cos(angle), y: 240 + 200 * Math.sin(angle) });
      });
    }

    const optionFor = (dayIdx: number) => {
      const snap = snapshots[dayIdx];
      return {
        series: [{
          type: 'graph',
          layout: 'none', // 钉住布局
          animationDuration: 300,
          animationDurationUpdate: 300,
          nodes: agents.map((a) => ({
            id: a.id,
            name: a.name,
            x: fixedPos.current.get(a.id)!.x,
            y: fixedPos.current.get(a.id)!.y,
            symbolSize: a.lod === 'star' ? 18 : 12,
            itemStyle: {
              color: fallbackColor(a.id),
              borderColor: a.lod === 'star' ? '#E6E9EF' : undefined, // 明星层白描边（02 §7.5）
              borderWidth: a.lod === 'star' ? 1.5 : 0,
            },
            label: { show: true, fontSize: 9, color: '#E6E9EF' },
          })),
          links: snap.edges.map((e) => ({
            source: e.a,
            target: e.b,
            lineStyle: {
              width: edgeWidth(e.aff),
              color: edgeColor(e),
              opacity: 0.85,
            },
          })),
        }],
      };
    };

    chart.setOption({
      baseOption: {
        timeline: {
          data: days,
          autoPlay: false,
          bottom: 0,
          label: { color: '#9AA4B2' },
        },
        ...optionFor(days.length - 1),
      },
      options: days.map((_, i) => optionFor(i)),
    } as never);
    const onChange = (e: any) => {
      if (typeof e.currentIndex === 'number') chart.setOption(optionFor(e.currentIndex) as never);
    };
    chart.on('timelinechanged', onChange);
    return () => {
      chart.off('timelinechanged', onChange);
    };
  }, [snapshots, agents]);

  useEffect(() => () => chartRef.current?.dispose(), []);

  return <div ref={ref} className="h-[520px] w-full" data-testid="force-graph" />;
}
