/**
 * 逐句播放引擎（05 T-WEB-11；06 §2：`lines[].at_offset_s` 唯一时间表、**客户端倍率是唯一节拍权威**；
 * 03 §3.1：2~4s/句、进度点 ●●○○○；03 §6.2.4：播放队列与事件队列分离，暂停/倒带不影响数据完整性）。
 *
 * - 对话事件只入队"剧本"（lines 引用，不复制不改动——`at_offset_s` 永不被改写）；
 * - 逐句实际间隔 = 相邻 `at_offset_s` 差值 ÷ 当前倍率；
 * - 倍率 >2× 整段直显（03 §3.1">2× 跳动画"档在逐句播放语境的适配，05 文档 D16）；
 * - 暂停/恢复保持游标（剩余时间按暂停点折算）。
 */
import type { DialogueLine } from '../proto/event';

export type Rate = number;

export interface PlaybackCallbacks {
  onLine: (line: DialogueLine, index: number, total: number) => void;
  onDone?: () => void;
}

export interface Clock {
  setTimeout: (fn: () => void, ms: number) => unknown;
  clearTimeout: (h: unknown) => void;
  now: () => number;
}

export const REAL_CLOCK: Clock = {
  setTimeout: (fn, ms) => setTimeout(fn, ms),
  clearTimeout: (h) => clearTimeout(h as never),
  now: () => Date.now(),
};

export const FULL_SHOW_RATE = 2; // >2× 整段直显（D16）

/** 纯函数：逐句调度时刻表（相邻差值 ÷ 倍率；输入 lines 只读，返回新数组）。 */
export function scheduleLines(lines: readonly DialogueLine[], rate: Rate): number[] {
  const times: number[] = [];
  let prev = 0;
  lines.forEach((line, i) => {
    const offset = line.at_offset_s;
    const gap = i === 0 ? 0 : Math.max(0, offset - prev);
    prev = offset;
    times.push((gap * 1000) / rate);
  });
  return times;
}

export class PlaybackEngine {
  private lines: readonly DialogueLine[] = [];
  private cursor = 0;
  private rate: Rate = 1;
  private timer: unknown = null;
  private startedAt = 0;
  private waitMs = 0;
  private playing = false;

  constructor(
    private readonly cb: PlaybackCallbacks,
    private readonly clock: Clock = REAL_CLOCK,
  ) {}

  /** 入队剧本并开始播放（事件到达只入队，不直接渲染，03 §6.2.4）。 */
  play(lines: readonly DialogueLine[], rate: Rate): void {
    this.stopTimer();
    this.lines = lines; // 引用持有，不改写（at_offset_s 不可变）
    this.cursor = 0;
    this.rate = rate;
    if (rate > FULL_SHOW_RATE) {
      // 整段直显（D16）
      lines.forEach((l, i) => this.cb.onLine(l, i, lines.length));
      this.playing = false;
      this.cb.onDone?.();
      return;
    }
    this.playing = true;
    this.emitNext();
  }

  setRate(rate: Rate): void {
    // 倍率切换立即生效（09 §5 E5）：剩余模拟时长 ÷ 新倍率重排
    if (this.playing && this.timer !== null) {
      const elapsedSimMs = (this.clock.now() - this.startedAt) * this.waitRateSnapshot;
      const gapSimMs = this.waitMs * this.waitRateSnapshot;
      const remainingSimMs = Math.max(0, gapSimMs - elapsedSimMs);
      this.rate = rate;
      this.stopTimer();
      if (rate > FULL_SHOW_RATE) {
        // 切入 >2×：余下整段直显（D16）
        while (this.cursor < this.lines.length) {
          this.cb.onLine(this.lines[this.cursor], this.cursor, this.lines.length);
          this.cursor += 1;
        }
        this.playing = false;
        this.cb.onDone?.();
        return;
      }
      this.arm(remainingSimMs / rate);
      return;
    }
    this.rate = rate;
  }

  private waitRateSnapshot = 1; // arm 时的倍率（剩余时长折算基准）

  private emitNext(): void {
    if (this.cursor >= this.lines.length) {
      this.playing = false;
      this.cb.onDone?.();
      return;
    }
    const line = this.lines[this.cursor];
    this.cb.onLine(line, this.cursor, this.lines.length);
    this.cursor += 1;
    if (this.cursor >= this.lines.length) {
      this.playing = false;
      this.cb.onDone?.();
      return;
    }
    const gap = Math.max(0, this.lines[this.cursor].at_offset_s - line.at_offset_s);
    this.arm((gap * 1000) / this.rate);
  }

  private arm(ms: number): void {
    this.waitMs = ms;
    this.waitRateSnapshot = this.rate;
    this.startedAt = this.clock.now();
    this.timer = this.clock.setTimeout(() => {
      this.timer = null;
      this.emitNext();
    }, ms);
  }

  pause(): void {
    if (!this.playing || this.timer === null) return;
    const elapsed = this.clock.now() - this.startedAt;
    this.waitMs = Math.max(0, this.waitMs - elapsed); // 保留剩余等待（游标不动）
    this.stopTimer();
    this.playing = false;
  }

  resume(): void {
    if (this.playing || this.cursor >= this.lines.length) return;
    this.playing = true;
    if (this.waitMs > 0) {
      this.arm(this.waitMs);
    } else {
      this.emitNext();
    }
  }

  getCursor(): number {
    return this.cursor;
  }

  isPlaying(): boolean {
    return this.playing;
  }

  private stopTimer(): void {
    if (this.timer !== null) {
      this.clock.clearTimeout(this.timer);
      this.timer = null;
    }
  }
}

/** 进度点（03 §3.1：●●○○○）。 */
export function progressDots(cursor: number, total: number): string {
  return '●'.repeat(cursor) + '○'.repeat(Math.max(0, total - cursor));
}
