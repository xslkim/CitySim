/** 双人对比双曲线（03 §3.5：双向 affinity/tension 双 y 轴折线 + 交互事件标记点，点击跳 /timeline?focus=）。 */
import * as echarts from 'echarts';
import { useEffect, useRef } from 'react';
import { useNavigate } from 'react-router-dom';
import type { RelationPair } from '../../proto/relations';

export default function PairCurves({ pair }: { pair: RelationPair }) {
  const ref = useRef<HTMLDivElement>(null);
  const navigate = useNavigate();

  useEffect(() => {
    if (!ref.current) return;
    const chart = echarts.init(ref.current, undefined, { renderer: 'svg' });
    const days = [...new Set([...pair.series.forward, ...pair.series.backward].map((p) => p.sim_day))].sort();
    const pick = (series: { sim_day: string; affinity: number; tension: number }[], key: 'affinity' | 'tension') =>
      days.map((d) => series.find((p) => p.sim_day === d)?.[key] ?? null);
    const markPoints = pair.changes
      .map((c) => {
        const day = c.sim_time.slice(0, 10);
        const series = c.a === pair.a ? pair.series.forward : pair.series.backward;
        const y = series.find((p) => p.sim_day === day)?.affinity;
        return y == null ? null : { coord: [day, y], value: `e${c.event_seq}`, eventSeq: c.event_seq };
      })
      .filter(Boolean);
    chart.setOption({
      backgroundColor: 'transparent',
      xAxis: { type: 'category', data: days, axisLabel: { color: '#9AA4B2' } },
      yAxis: [
        { type: 'value', name: 'affinity', axisLabel: { color: '#2EFA8D' }, min: -100, max: 100 },
        { type: 'value', name: 'tension', axisLabel: { color: '#F75752' }, min: 0, max: 100 },
      ],
      legend: { textStyle: { color: '#9AA4B2' } },
      series: [
        { name: `${pair.a}→${pair.b} aff`, type: 'line', data: pick(pair.series.forward, 'affinity'),
          itemStyle: { color: '#2EFA8D' },
          markPoint: { data: markPoints, symbolSize: 30, label: { fontSize: 8 } } },
        { name: `${pair.b}→${pair.a} aff`, type: 'line', data: pick(pair.series.backward, 'affinity'),
          itemStyle: { color: '#5AC3ED' } },
        { name: `${pair.a}→${pair.b} ten`, type: 'line', yAxisIndex: 1, data: pick(pair.series.forward, 'tension'),
          itemStyle: { color: '#F75752' }, lineStyle: { type: 'dashed' } },
        { name: `${pair.b}→${pair.a} ten`, type: 'line', yAxisIndex: 1, data: pick(pair.series.backward, 'tension'),
          itemStyle: { color: '#FEAE3E' }, lineStyle: { type: 'dashed' } },
      ],
    } as never);
    chart.on('click', (p: any) => {
      const seq = p?.data?.eventSeq;
      if (seq) navigate(`/timeline?focus=e${seq}`);
    });
    return () => chart.dispose();
  }, [pair, navigate]);

  return <div ref={ref} className="h-[300px] w-full" data-testid="pair-curves" />;
}
