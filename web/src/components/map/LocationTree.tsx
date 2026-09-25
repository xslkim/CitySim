/** 地点树（03 §2.1 左栏：公寓 L1~L6×4 房+公共区 / 公司各部门 / 外部 / 校外聚合区块）。 */
import { useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { getLayout, tryLoadMapLayout, type Site } from '../../lib/mapLayout';
import { useUiStore } from '../../stores/uiStore';
import { useWorldStore } from '../../stores/worldStore';

function NodeButton({ id, name }: { id: string; name: string }) {
  const navigate = useNavigate();
  const selectLocation = useUiStore((s) => s.selectLocation);
  const selected = useUiStore((s) => s.selectedLocation);
  return (
    <button
      type="button"
      className={`block w-full rounded px-2 py-0.5 text-left text-aux ${selected === id ? 'bg-bg-2 text-accent' : 'text-text-1'}`}
      onClick={() => {
        selectLocation(id);
        navigate(`/location/${encodeURIComponent(id)}`);
      }}
    >
      {name}
    </button>
  );
}

export default function LocationTree() {
  const [layout, setLayout] = useState(getLayout());
  const snapshot = useWorldStore((s) => s.snapshot);
  useEffect(() => {
    tryLoadMapLayout().then(setLayout);
  }, []);
  if (!layout) return <div className="text-aux text-text-1">地点树加载中…</div>;
  const offsiteN = (snapshot?.agents ?? []).filter((a) => a.location_id?.startsWith('home.')).length;
  return (
    <div className="space-y-2">
      {layout.sites.map((site: Site) => (
        <details key={site.id} open>
          <summary className="cursor-pointer text-body text-text-0">
            {site.name}
            {site.id === 'offsite' && (
              <span className="ml-1 rounded bg-bg-2 px-1 text-ts text-warn">{offsiteN}</span>
            )}
          </summary>
          <div className="ml-2">
            {(site.rooms ?? site.zones ?? []).map((n) => (
              <NodeButton key={n.id} id={n.id} name={n.name} />
            ))}
            {(site.commons ?? []).map((n) => (
              <NodeButton key={n.id} id={n.id} name={n.name} />
            ))}
            {site.type === 'aggregate' &&
              (site.zones ?? []).map((n) => <NodeButton key={n.id} id={n.id} name={n.name} />)}
          </div>
        </details>
      ))}
    </div>
  );
}
