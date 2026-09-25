/**
 * /ripple[/:eventId] 涟漪追踪页（05 T-WEB-16；03 §3.4 五段全渲染 + 无参落地页推荐位 + 导出选题卡）。
 */
import { useEffect, useState } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import { apiGet } from '../api/client';
import RippleDag from '../components/ripple/RippleDag';
import TodayRipples from '../components/ripple/TodayRipples';
import { renderEvent } from '../lib/eventText';
import { exportCardText } from '../lib/rippleLayout';
import { rippleDataSchema, type RippleData } from '../proto/ripple';
import { useAgentsStore } from '../stores/agentsStore';

export default function RipplePage() {
  const { eventId } = useParams();
  const [input, setInput] = useState('');
  const [data, setData] = useState<RippleData | null>(null);
  const [error, setError] = useState<string | null>(null);
  const navigate = useNavigate();
  const profiles = useAgentsStore((s) => s.profiles);
  const nameOf = (id: string) => profiles.find((p) => p.id === id)?.name ?? id;

  useEffect(() => {
    if (!eventId) {
      setData(null);
      return;
    }
    apiGet(`/api/ripple/${eventId}`, rippleDataSchema)
      .then((r) => { setData(r.data); setError(null); })
      .catch((e) => { setData(null); setError(String(e)); });
  }, [eventId]);

  if (!eventId) {
    // 无参落地页 = TodayRipples Top5 + 源事件检索框（03 §3.4）
    return (
      <div data-testid="ripple-landing">
        <div className="card mb-2">
          <div className="mb-1 text-aux text-text-1">源事件检索（e1102 或裸 seq 1102）</div>
          <input
            className="w-full rounded bg-bg-2 px-2 py-1 text-body text-text-0"
            placeholder="例如 e1024 或 1024"
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter' && /^e?\d+$/.test(input)) navigate(`/ripple/${input}`);
            }}
          />
        </div>
        <TodayRipples onPick={(seq) => navigate(`/ripple/e${seq}`)} />
      </div>
    );
  }

  if (error) return <div className="card text-negative">涟漪查询失败：{error}</div>;
  if (!data) return <div className="card text-text-1">加载中…</div>;
  const sourceText = data.source ? renderEvent(data.source, nameOf).text : '';
  const sourceGrade = (data.source?.ui?.grade as string) ?? null;

  return (
    <div data-testid="ripple-page">
      <div className="card mb-2 flex items-center gap-2">
        <span className="text-title text-text-0">
          源事件 e{data.source_seq}
          {sourceText && <span className="ml-2 text-body font-normal">「{sourceText}」</span>}
          {data.source && <span className="ml-2 text-aux text-text-1">{data.source.sim_time.slice(0, 16).replace('T', ' ')}</span>}
          {sourceGrade === 'A' && <span className="ml-1 text-warn">★A级</span>}
        </span>
        <button type="button" className="ml-auto rounded bg-bg-2 px-2 py-1 text-aux text-accent"
          onClick={() => {
            navigator.clipboard?.writeText(exportCardText(data, sourceText));
          }}>
          导出选题卡
        </button>
      </div>
      {/* 统计条 */}
      <div className="card mb-2 text-aux text-text-1" data-testid="ripple-stats">
        覆盖 {data.stats.covered_agents}/40 人 · {data.stats.hops} 手 · 最大失真{' '}
        {(data.stats.max_distortion * 100).toFixed(0)}% · {data.stats.followup_count} 条后续事件
      </div>
      <div className="card mb-2">
        <RippleDag data={data} sourceGrade={sourceGrade} />
      </div>
      {/* 投影列表（当事/目击标注） */}
      <div className="card mb-2">
        <div className="mb-1 text-aux text-text-1">直接投影（{data.projections.length} 人）</div>
        {data.projections.map((p) => (
          <div key={p.memory_id} className="py-0.5 text-aux">
            <span className="text-text-0">{nameOf(p.agent_id)}</span>
            <span className="ml-1 rounded bg-bg-2 px-1 text-ts">{p.is_witness ? '目击' : '当事'}</span>
            <span className="ml-2 text-text-1">{p.content_display}</span>
          </div>
        ))}
      </div>
      {/* 关系边变化迷你卡 */}
      <div className="card">
        <div className="mb-1 text-aux text-text-1">关系边变化</div>
        {data.relation_changes.map((r, i) => (
          <div key={i} className="py-0.5 text-aux" data-testid="relation-change-mini">
            <span className="text-text-0">{nameOf(r.a_id)}→{nameOf(r.b_id)}</span>
            <span className={r.delta_affinity >= 0 ? 'ml-2 text-positive' : 'ml-2 text-negative'}>
              aff {r.delta_affinity >= 0 ? '+' : ''}{r.delta_affinity}
            </span>
            <span className={r.delta_tension > 0 ? 'ml-2 text-warn' : 'ml-2 text-text-1'}>
              ten {r.delta_tension >= 0 ? '+' : ''}{r.delta_tension}
            </span>
            <span className="ml-2 text-ts text-text-1">e{r.event_seq}</span>
          </div>
        ))}
      </div>
    </div>
  );
}
