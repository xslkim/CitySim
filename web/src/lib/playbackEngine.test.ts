/** T-WEB-11 逐句播放引擎测试（06 §2 / 03 §3.1 / 05 文档 D16）。 */
import { describe, expect, it } from 'vitest';
import type { DialogueLine } from '../proto/event';
import { PlaybackEngine, progressDots, scheduleLines } from './playbackEngine';
import type { Clock } from './playbackEngine';

function fakeClock() {
  const timers: { fn: () => void; at: number; h: number }[] = [];
  let now = 0;
  let seq = 0;
  const clock: Clock = {
    setTimeout: (fn, ms) => {
      const h = ++seq;
      timers.push({ fn, at: now + ms, h });
      return h;
    },
    clearTimeout: (h) => {
      const i = timers.findIndex((t) => t.h === h);
      if (i >= 0) timers.splice(i, 1);
    },
    now: () => now,
  };
  const advance = (ms: number) => {
    const target = now + ms;
    for (;;) {
      const next = timers.filter((t) => t.at <= target).sort((a, b) => a.at - b.at)[0];
      if (!next) break;
      now = next.at;
      clock.clearTimeout(next.h);
      next.fn();
    }
    now = target;
  };
  return { clock, advance, pending: () => timers.length };
}

const LINES: DialogueLine[] = [
  { speaker: 'A01', text_display: '第一句', at_offset_s: 0 },
  { speaker: 'A02', text_display: '第二句', at_offset_s: 3 },
  { speaker: 'A01', text_display: '第三句', at_offset_s: 6.5 },
];

describe('playbackEngine（06 §2：客户端倍率唯一节拍权威）', () => {
  it('test_interval_divided_by_rate：差值 3s、2× → 1.5s', () => {
    expect(scheduleLines(LINES, 1)).toEqual([0, 3000, 3500]);
    expect(scheduleLines(LINES, 2)).toEqual([0, 1500, 1750]);
  });

  it('逐句推进：fake clock 按调度时刻出句', () => {
    const { clock, advance } = fakeClock();
    const got: string[] = [];
    const eng = new PlaybackEngine({ onLine: (l) => got.push(l.text_display) }, clock);
    eng.play(LINES, 1);
    expect(got).toEqual(['第一句']);
    advance(3000);
    expect(got).toEqual(['第一句', '第二句']);
    advance(3500);
    expect(got).toHaveLength(3);
    expect(eng.isPlaying()).toBe(false);
  });

  it('test_pause_resume_keeps_cursor：暂停保游标、剩余时间续播', () => {
    const { clock, advance, pending } = fakeClock();
    const got: string[] = [];
    const eng = new PlaybackEngine({ onLine: (l) => got.push(l.text_display) }, clock);
    eng.play(LINES, 1);
    advance(1000); // 距第二句还差 2s
    eng.pause();
    expect(eng.getCursor()).toBe(1);
    expect(pending()).toBe(0);
    advance(10_000); // 暂停期间不推进
    expect(got).toEqual(['第一句']);
    eng.resume();
    advance(2000);
    expect(got).toEqual(['第一句', '第二句']);
  });

  it('test_rate_above_2x_shows_full：>2× 整段直显（D16）', () => {
    const { clock } = fakeClock();
    const got: string[] = [];
    const eng = new PlaybackEngine({ onLine: (l) => got.push(l.text_display) }, clock);
    eng.play(LINES, 4);
    expect(got).toHaveLength(3);
    expect(eng.isPlaying()).toBe(false);
  });

  it('倍率切换立即生效（09 §5 E5）：1×→4× 剩余整段直显', () => {
    const { clock, advance } = fakeClock();
    const got: string[] = [];
    const eng = new PlaybackEngine({ onLine: (l) => got.push(l.text_display) }, clock);
    eng.play(LINES, 1);
    advance(1000);
    eng.setRate(4);
    expect(got).toHaveLength(3);
  });

  it('test_offsets_never_mutated：输入 lines 引用不被改写', () => {
    const lines = LINES.map((l) => ({ ...l }));
    const { clock, advance } = fakeClock();
    const eng = new PlaybackEngine({ onLine: () => {} }, clock);
    eng.play(lines, 2);
    advance(10_000);
    expect(lines.map((l) => l.at_offset_s)).toEqual([0, 3, 6.5]);
  });

  it('进度点 ●●○○○（03 §3.1）', () => {
    expect(progressDots(2, 5)).toBe('●●○○○');
  });
});
