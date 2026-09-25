/** 时间轴六维过滤条（03 §3.2：type/actor/location/trigger 多选 + 关键词 + A 级开关，全部下推 REST）。 */
import { EVENT_TYPES } from '../../proto/event_types';
import type { TimelineFilter } from '../../stores/timelineStore';

interface Props {
  filter: TimelineFilter;
  onChange: (f: Partial<TimelineFilter>) => void;
  actors: string[];
  locations: string[];
}

function MultiSelect({ label, options, values, onPick }: {
  label: string; options: string[]; values: string[]; onPick: (v: string[]) => void;
}) {
  return (
    <details className="relative">
      <summary className="cursor-pointer rounded bg-bg-2 px-2 py-1 text-aux text-text-1">
        {label}{values.length ? `(${values.length})` : ''}
      </summary>
      <div className="absolute z-10 mt-1 max-h-48 w-56 overflow-y-auto rounded-card border border-border bg-bg-1 p-1">
        {options.map((o) => (
          <label key={o} className="flex items-center gap-1 px-1 py-0.5 text-aux text-text-0">
            <input
              type="checkbox"
              checked={values.includes(o)}
              onChange={(e) => onPick(e.target.checked ? [...values, o] : values.filter((v) => v !== o))}
            />
            {o}
          </label>
        ))}
      </div>
    </details>
  );
}

export const TRIGGER_OPTIONS = ['autonomous', 'world', 'director', 'gift', 'vote', 'system']; // 06 §1.1 含 system

export function buildEventParams(f: TimelineFilter): string {
  const p = new URLSearchParams();
  if (f.types.length) p.set('type', f.types.join(','));
  if (f.actors.length) p.set('actor', f.actors.join(','));
  if (f.locations.length) p.set('location', f.locations.join(','));
  if (f.triggers.length) p.set('trigger', f.triggers.join(','));
  if (f.gradeAOnly) p.set('grade', 'A'); // 最新生效 grade（event_grade_view 口径，03 §5.1）
  if (f.q) p.set('q', f.q);
  return p.toString();
}

export default function FilterBar({ filter, onChange, actors, locations }: Props) {
  return (
    <div className="mb-2 flex flex-wrap items-center gap-2" data-testid="filter-bar">
      <MultiSelect label="类型" options={[...EVENT_TYPES]} values={filter.types}
        onPick={(types) => onChange({ types })} />
      <MultiSelect label="角色" options={actors} values={filter.actors}
        onPick={(v) => onChange({ actors: v })} />
      <MultiSelect label="地点" options={locations} values={filter.locations}
        onPick={(v) => onChange({ locations: v })} />
      <MultiSelect label="trigger" options={TRIGGER_OPTIONS} values={filter.triggers}
        onPick={(v) => onChange({ triggers: v })} />
      <input
        className="rounded bg-bg-2 px-2 py-1 text-aux text-text-0"
        placeholder="关键词"
        value={filter.q}
        onChange={(e) => onChange({ q: e.target.value })}
      />
      <label className="flex items-center gap-1 text-aux text-text-1">
        <input
          type="checkbox"
          checked={filter.gradeAOnly}
          onChange={(e) => onChange({ gradeAOnly: e.target.checked })}
        />
        A级
      </label>
    </div>
  );
}
