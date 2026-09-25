/**
 * /relations 关系结构演化页（05 T-WEB-17；03 §3.5：force+timeline 全网演化 + 任意两人双曲线
 * + 周变化率小卡（与 /health 同源，阈值 API 下发））。
 */
import { useEffect, useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import { apiGet } from '../api/client';
import ForceGraph from '../components/relations/ForceGraph';
import PairCurves from '../components/relations/PairCurves';
import { healthDataSchema, type HealthData } from '../proto/health';
import { relationPairSchema, relationSnapshotsSchema, type RelationPair, type RelationSnapshots } from '../proto/relations';
import { parsePair } from '../lib/relationsLayout';
import { useAgentsStore } from '../stores/agentsStore';

export function WeekChangeCard({ health }: { health: HealthData | null }) {
  const m = health?.metrics.find((x) => x.key === 'relation_graph_weekly_change_ratio');
  if (!m) return null;
  const color = m.color === 'green' ? 'text-positive' : m.color === 'yellow' ? 'text-warn' : m.color === 'red' ? 'text-negative' : 'text-text-1';
  return (
    <div className="card" data-testid="week-change-card">
      <div className="text-aux text-text-1">关系图周变化率（阈值 API 下发，01 §9 持有）</div>
      <div className={`text-title ${color}`}>
        {m.value == null ? '—' : `${(m.value * 100).toFixed(1)}%`}
      </div>
    </div>
  );
}

export default function RelationsPage() {
  const [params] = useSearchParams();
  const profiles = useAgentsStore((s) => s.profiles);
  const [snapshots, setSnapshots] = useState<RelationSnapshots['items']>([]);
  const [pair, setPair] = useState<RelationPair | null>(null);
  const [health, setHealth] = useState<HealthData | null>(null);
  const pairParam = parsePair(params.get('pair'));
  const [selA, setSelA] = useState(pairParam?.[0] ?? 'A01');
  const [selB, setSelB] = useState(pairParam?.[1] ?? 'A02');

  useEffect(() => {
    apiGet('/api/relations/snapshots', relationSnapshotsSchema)
      .then((r) => setSnapshots(r.data.items)).catch(() => undefined);
    apiGet('/api/health', healthDataSchema).then((r) => setHealth(r.data)).catch(() => undefined);
  }, []);

  useEffect(() => {
    if (selA && selB && selA !== selB) {
      apiGet(`/api/relations/pair?a=${selA}&b=${selB}`, relationPairSchema)
        .then((r) => setPair(r.data)).catch(() => setPair(null));
    }
  }, [selA, selB]);

  return (
    <div className="grid grid-cols-3 gap-2" data-testid="relations-page">
      <div className="card col-span-2">
        <div className="mb-1 text-aux text-text-1">全网关系演化（按模拟日步进；节点钉住只变边，02 §7.5）</div>
        <ForceGraph snapshots={snapshots} agents={profiles} />
      </div>
      <div className="space-y-2">
        <WeekChangeCard health={health} />
        <div className="card">
          <div className="mb-1 text-aux text-text-1">双人对比（?pair=A,B 入口，03 §3.3）</div>
          <div className="mb-1 flex gap-1">
            {[selA, selB].map((v, i) => (
              <select key={i} value={v} className="rounded bg-bg-2 px-1 py-0.5 text-aux text-text-0"
                onChange={(e) => (i === 0 ? setSelA(e.target.value) : setSelB(e.target.value))}>
                {profiles.map((p) => (
                  <option key={p.id} value={p.id}>{p.name}（{p.id}）</option>
                ))}
              </select>
            ))}
          </div>
          {pair && <PairCurves pair={pair} />}
        </div>
      </div>
    </div>
  );
}
