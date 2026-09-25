/** 角色列表（03 §2.1 左栏：40 人按 LOD 层分组徽标 ★明星/◐次要/○背景）。 */
import { useNavigate } from 'react-router-dom';
import { useAgentsStore } from '../../stores/agentsStore';
import { useUiStore } from '../../stores/uiStore';
import Avatar from './Avatar';

export const LOD_BADGE: Record<string, string> = { star: '★', secondary: '◐', background: '○' };

export default function AgentList() {
  const profiles = useAgentsStore((s) => s.profiles);
  const navigate = useNavigate();
  const selectAgent = useUiStore((s) => s.selectAgent);
  const selected = useUiStore((s) => s.selectedAgent);
  const groups: [string, string][] = [['star', '明星'], ['secondary', '次要'], ['background', '背景']];
  if (!profiles.length) return <div className="text-aux text-text-1">角色列表加载中…</div>;
  return (
    <div className="space-y-1">
      {groups.map(([tier, label]) => {
        const members = profiles.filter((p) => p.lod === tier);
        if (!members.length) return null;
        return (
          <details key={tier} open={tier !== 'background'}>
            <summary className="cursor-pointer text-body text-text-0">
              {LOD_BADGE[tier]} {label}（{members.length}）
            </summary>
            {members.map((p) => (
              <button
                key={p.id}
                type="button"
                className={`flex w-full items-center gap-1.5 rounded px-1.5 py-0.5 text-left text-aux ${selected === p.id ? 'bg-bg-2 text-accent' : 'text-text-1'}`}
                onClick={() => {
                  selectAgent(p.id);
                  navigate(`/agent/${p.id}`);
                }}
              >
                <Avatar id={p.id} name={p.name} signatureColor={p.signature_color} size={18} />
                <span className="truncate">{p.name}</span>
              </button>
            ))}
          </details>
        );
      })}
    </div>
  );
}
