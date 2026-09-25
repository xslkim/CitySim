/**
 * 头像组件（05 T-WEB-11）：有 portraits 资产（`ui/assets/portraits/<slug>.png`，映射表 =
 * `lib/portraits.ts` 构建产物，06 T-ART-02/00 §1 A17，禁止手改）用 img；
 * 无资产兜底 = 签名色圆底 + 姓名首字（形态参照 ui/web-lite `.av`；签名色唯一权威 =
 * `config/agents.yaml`，经 /api/agents 摘要下发）；签名色缺失按 id 稳定散列取池内一色。
 */
import { fallbackColor, portraitUrl } from '../../lib/portraits';

interface AvatarProps {
  id: string;
  name: string;
  signatureColor?: string | null;
  size?: number;
}

export function resolveAvatarColor(id: string, signatureColor?: string | null): string {
  return signatureColor ?? fallbackColor(id);
}

export default function Avatar({ id, name, signatureColor, size = 28 }: AvatarProps) {
  const url = portraitUrl(id);
  if (url) {
    return (
      <img
        src={url}
        alt={name}
        width={size}
        height={size}
        className="rounded-full object-cover"
        data-testid={`avatar-img-${id}`}
      />
    );
  }
  const color = resolveAvatarColor(id, signatureColor);
  return (
    <span
      className="flex items-center justify-center rounded-full font-bold text-bg-0"
      style={{ width: size, height: size, background: color, fontSize: size * 0.5 }}
      data-testid={`avatar-fallback-${id}`}
    >
      {name.slice(0, 1)}
    </span>
  );
}
