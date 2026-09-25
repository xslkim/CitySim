/** 地图 pawn（03 §3.1：头像占位 + 名牌（姓名+LOD 徽标）+ 情绪符号；跟拍 2px accent 外圈，02 §7.2）。
 *  名牌/气泡反缩放恒定字号由父层 `scale(1/k)` 处理。 */
import Avatar from '../common/Avatar';
import { LOD_BADGE } from '../common/AgentList';

export function moodEmoji(mood: number | null): string {
  if (mood == null) return '';
  if (mood >= 70) return '😊';
  if (mood >= 50) return '🙂';
  if (mood >= 30) return '😐';
  return '😟';
}

interface PawnProps {
  id: string;
  name: string;
  signatureColor?: string | null;
  lod?: string | null;
  mood?: number | null;
  x: number;
  y: number;
  following?: boolean;
  onClick?: () => void;
}

export default function Pawn({ id, name, signatureColor, lod, mood, x, y, following, onClick }: PawnProps) {
  return (
    <g transform={`translate(${x},${y})`} onClick={onClick} style={{ cursor: 'pointer' }} data-agent={id}>
      {following && <circle r={14} fill="none" stroke="var(--accent)" strokeWidth={2} />}
      <foreignObject x={-11} y={-11} width={22} height={22}>
        <Avatar id={id} name={name} signatureColor={signatureColor} size={22} />
      </foreignObject>
      {mood != null && (
        <text x={10} y={-8} fontSize={9} data-testid={`mood-${id}`}>
          {moodEmoji(mood)}
        </text>
      )}
      <text y={22} textAnchor="middle" fontSize={9} fill="var(--text-0)" className="select-none">
        {name} {LOD_BADGE[lod ?? ''] ?? ''}
      </text>
    </g>
  );
}
