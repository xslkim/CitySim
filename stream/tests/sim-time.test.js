/** R3 #6 昼夜本地化单测（node --test）：+08 本地模拟时 → 日景/夜景判定矩阵。 */

import test from 'node:test';
import assert from 'node:assert/strict';

import { fmtSimHHMM, isDaySim } from '../js/lib/sim-time.js';

test('fmtSimHHMM 渲染 +08（与 web lib/simTime 同口径）', () => {
  // UTC 05:30 = 北京 13:30
  assert.equal(fmtSimHHMM('2026-10-16T05:30:00+00:00'), '13:30');
  assert.equal(fmtSimHHMM('2026-10-16T13:25:00+08:00'), '13:25');
});

test('isDaySim 验收矩阵：13:00/18:00 日景，19:00/02:00 夜景（+08 口径）', () => {
  // 直播报告原案：sim 13:25（下午）渲染星空月亮 = UTC 未本地化之误；+08 下应为日景
  assert.equal(isDaySim('2026-10-16T05:25:00+00:00'), true, 'UTC 05:25 = 北京 13:25 日景');
  assert.equal(isDaySim('2026-10-16T13:25:00+08:00'), true, '北京 13:25 日景');
  assert.equal(isDaySim('2026-10-16T18:00:00+08:00'), true, '北京 18:00 日景（边界含）');
  assert.equal(isDaySim('2026-10-16T18:59:59+08:00'), true);
  assert.equal(isDaySim('2026-10-16T19:00:00+08:00'), false, '北京 19:00 夜景（边界起）');
  assert.equal(isDaySim('2026-10-16T02:00:00+08:00'), false, '北京 02:00 夜景');
  assert.equal(isDaySim('2026-10-16T05:59:59+08:00'), false, '北京 05:59 夜景');
  assert.equal(isDaySim('2026-10-16T06:00:00+08:00'), true, '北京 06:00 日景（边界起）');
  assert.equal(isDaySim('bad-time'), true, '无时钟信号默认日景（B8 降级不闪变）');
});
