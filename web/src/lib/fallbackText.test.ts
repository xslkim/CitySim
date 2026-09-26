/** T-ITER2-04③：兜底句识别 + feed 过滤/降采样（lite 今日看点消费侧）。 */

import { describe, expect, it } from 'vitest';
import { FALLBACK_BATCH_MESSAGE, isFallbackText, partitionFeed } from './fallbackText';

describe('isFallbackText（与 server sanitize FALLBACK_* 句集对齐）', () => {
  it('识别三种兜底句（含首尾空白），放行真实内容', () => {
    expect(isFallbackText('刚才走神了，没留下什么记录。')).toBe(true);
    expect(isFallbackText(' 这段内容没能完整记录下来。 ')).toBe(true);
    expect(isFallbackText('（这句没听清）')).toBe(true);
    expect(isFallbackText('今天和林晚聊了连载的事。')).toBe(false);
    expect(isFallbackText('')).toBe(false);
    expect(isFallbackText(undefined)).toBe(false);
    expect(isFallbackText(null)).toBe(false);
  });
});

describe('partitionFeed（兜底事件不进 feed，计数降采样一张卡）', () => {
  it('过滤兜底事件并计数', () => {
    const ev = (seq: number, text: string) => ({ seq, payload: { text_display: text } });
    const [real, n] = partitionFeed([
      ev(1, '这段内容没能完整记录下来。'),
      ev(2, '真实事件一'),
      ev(3, '刚才走神了，没留下什么记录。'),
      ev(4, '真实事件二'),
    ]);
    expect(real.map((e) => e.seq)).toEqual([2, 4]);
    expect(n).toBe(2);
  });

  it('全兜底时 real 为空且计数正确（UI 侧渲染 ≤1 张 batch 卡）', () => {
    const [real, n] = partitionFeed([
      { payload: { text_display: '这段内容没能完整记录下来。' } },
      { payload: { text_display: '这段内容没能完整记录下来。' } },
    ]);
    expect(real).toEqual([]);
    expect(n).toBe(2);
    expect(FALLBACK_BATCH_MESSAGE).toBe('有几段心事没能完整记录');
  });
});
