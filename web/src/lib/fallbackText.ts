/**
 * 兜底句识别（T-ITER2-04③；与 server/worldsim/sanitize.py FALLBACK_* 句集对齐——
 * 展示侧消费点（lite feed / 直播字幕）用同一句集过滤兜底事件，不进观众面。
 * 注意：这里只做"是否兜底句"判定，清洗唯一实现仍在服务端 sanitize。
 */

export const FALLBACK_SENTENCES: readonly string[] = [
  '刚才走神了，没留下什么记录。',
  '这段内容没能完整记录下来。',
  '（这句没听清）',
];

/** 同一拍多条兜底事件的降采样卡片文案（≤1 张） */
export const FALLBACK_BATCH_MESSAGE = '有几段心事没能完整记录';

export function isFallbackText(t: unknown): boolean {
  return typeof t === 'string' && (FALLBACK_SENTENCES as readonly string[]).includes(t.trim());
}

interface WithTextDisplay {
  payload?: { text_display?: unknown } | null;
}

/** feed 过滤：拆成 [真实内容事件, 兜底事件计数]；兜底事件按 batch 文案降采样为一张卡 */
export function partitionFeed<T extends WithTextDisplay>(events: readonly T[]): [T[], number] {
  const real: T[] = [];
  let fallbackCount = 0;
  for (const e of events) {
    if (isFallbackText(e?.payload?.text_display)) fallbackCount += 1;
    else real.push(e);
  }
  return [real, fallbackCount];
}
