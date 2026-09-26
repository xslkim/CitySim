/** T-ITER2-05（round2 #5③）：展示层时间格式化唯一入口——UTC ISO 输入 → +08 显示。 */

import { describe, expect, it } from 'vitest';
import { formatSimHHMM, formatSimMMddHHMM } from './simTime';

describe('formatSimHHMM（+00:00 → +08 展示）', () => {
  it('UTC 22:55 → 次日 +08 06:55（round2 时区差 8h 样本）', () => {
    expect(formatSimHHMM('2026-10-12T22:55:00+00:00')).toBe('06:55');
  });

  it('UTC 16:25 → +08 00:25（跨日边界）', () => {
    expect(formatSimHHMM('2026-10-12T16:25:00+00:00')).toBe('00:25');
  });

  it('已带 +08 偏移的输入同样按 +08 渲染（无时区双漂移）', () => {
    expect(formatSimHHMM('2026-10-12T13:25:00+08:00')).toBe('13:25');
  });

  it('空/非法输入兜底', () => {
    expect(formatSimHHMM(null)).toBe('—');
    expect(formatSimHHMM(undefined)).toBe('—');
    expect(formatSimHHMM('not-a-time')).toBe(''); // 裸切片兜底：短字符串切片为空（06 §7 B8 同口径）
  });
});

describe('formatSimMMddHHMM（日期+时刻）', () => {
  it('UTC 22:55 → "10-13 06:55"', () => {
    expect(formatSimMMddHHMM('2026-10-12T22:55:00+00:00')).toBe('10-13 06:55');
  });
});
