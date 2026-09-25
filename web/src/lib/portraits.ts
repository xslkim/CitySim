// 生成物勿手改（由 scripts/gen_portrait_manifest.py 从 config/agents.yaml 派生，00 §1 A17）
/** agent_id → 特写文件名（06 T-ART-02；资产路径形态 /assets/portraits/<file>，T-ART-03 挂载） */
export const PORTRAITS: Record<string, string> = {
  'A01': 'linwan.png',
  'A02': 'zhouxu.png',
  'A03': 'suman.png',
  'A04': 'chenyu.png',
  'A05': 'yezhen.png',
  'A06': 'hanche.png',
};

/** 签名色池（02 §4.3；= agents.yaml 全部 signature_color.hex 去重） */
export const SIGNATURE_POOL: readonly string[] = ['#C3CDDA', '#5583C9', '#EB6999', '#1A5E80', '#FADB40', '#61191E', '#BF700F', '#A4C394', '#BE2A42', '#209C68', '#209894', '#4A2999', '#A432A1', '#7D4F30', '#A8B82E', '#6A6C6F'];

/** 有资产 → img URL；无资产 → null（调用方走签名色 initials 兜底，05 T-WEB-11） */
export function portraitUrl(agentId: string): string | null {
  const file = PORTRAITS[agentId];
  return file ? `/assets/portraits/${file}` : null;
}

/** 稳定散列兜底色（签名色缺失时按 id 取池内一色，05 T-WEB-11；djb2 确定性） */
export function fallbackColor(agentId: string): string {
  let h = 5381;
  for (const ch of agentId) h = ((h * 33) ^ ch.charCodeAt(0)) >>> 0;
  return SIGNATURE_POOL[h % SIGNATURE_POOL.length] ?? '#8A93A0'; // neutral 兜底（02 §7.1）
}
