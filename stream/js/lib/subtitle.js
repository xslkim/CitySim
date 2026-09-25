/** T-LTV-03 字幕引擎调度器（纯逻辑，不触 DOM；06 §2 / 04 §6.4 / 00 §4 红线 7/9）。
 *
 * - 对话类事件（5 类型，均有 lines[]，06 §1.2）整段入队、逐句按 `at_offset_s ÷ 倍率` 播放；
 *   直播页倍率工程默认 1.0（06 §7 B3）；客户端倍率是播放唯一权威（红线 9）。
 * - 队列积压 >3 段自动提速 ×1.5 追平、清空后回落 1.0（06 §7 B4 工程默认）。
 * - 非对话事件仅当 `payload.text_display` 存在时单条播 4s（B4/B7：不做模板拼接兜底）。
 * - 无展示文本（internal 事件 conditional 键已剥除）→ 零字幕项（红线 7）。
 */

export const DIALOGUE_TYPES = new Set([
  'dialogue.chat', 'dialogue.gossip', 'dialogue.argue', 'dialogue.confess', 'dialogue.apologize',
]);
export const BASE_RATE = 1.0;          // 直播页倍率工程默认（06 §7 B3）
export const BACKLOG_THRESHOLD = 3;    // 积压阈值（段）（06 §7 B4）
export const BACKLOG_RATE = 1.5;       // 积压提速（06 §7 B4）
export const SINGLE_LINE_DURATION_S = 4; // 非对话单条字幕显示时长（06 §7 B4；对话末句驻留同口径）
export const LINE_MAX_LEN = 40;        // 单句 ≤40 字（01 §7 / 02 §6.2 共用约束；前端不截断，超出仅告警）

/** 事件 → 字幕段；无展示文本 → null（零渲染）。 */
export function eventToSegment(ev) {
  const p = ev?.payload || {};
  if (DIALOGUE_TYPES.has(ev.type)) {
    const lines = (Array.isArray(p.lines) ? p.lines : []).filter(
      (l) => l && typeof l.text_display === 'string' && l.text_display
        && Number.isFinite(l.at_offset_s));
    if (!lines.length) return null;
    return {
      kind: 'dialogue', seq: ev.seq, type: ev.type,
      result: typeof p.result === 'string' ? p.result : null,
      participants: Array.isArray(p.participants) ? p.participants : [],
      teller: typeof p.teller === 'string' ? p.teller : null,
      listener: typeof p.listener === 'string' ? p.listener : null,
      lines,
    };
  }
  if (typeof p.text_display === 'string' && p.text_display) {
    return { kind: 'single', seq: ev.seq, type: ev.type, text: p.text_display };
  }
  return null;
}

/** 逐句时刻表：segmentStart + at_offset_s ÷ rate（相邻差值 ÷ 倍率，唯一节拍权威）。 */
export function scheduleSegment(seg, startAt, rate) {
  if (seg.kind === 'dialogue') {
    return seg.lines.map((l, i) => ({
      speaker: typeof l.speaker === 'string' ? l.speaker : null,
      text: l.text_display,
      index: i, total: seg.lines.length,
      at: startAt + l.at_offset_s / rate,
    }));
  }
  return [{ speaker: null, text: seg.text, index: 0, total: 1, at: startAt }];
}

/** 段时长：末句时刻 + 末句驻留（与单条同 4s 口径，÷ 倍率）。 */
export function segmentDuration(seg, rate) {
  if (seg.kind === 'dialogue') {
    const last = seg.lines.at(-1);
    return (last.at_offset_s + SINGLE_LINE_DURATION_S) / rate;
  }
  return SINGLE_LINE_DURATION_S / rate;
}

const DEFAULT_ENV = {
  now: () => Date.now() / 1000,
  setTimeout: (fn, ms) => setTimeout(fn, ms),
  clearTimeout: (id) => clearTimeout(id),
};

export class SubtitleQueue {
  /** @param opts.onLine(line, seg)      逐句播出（speaker 可为 null=单条）
   *  @param opts.onSegmentStart(seg, rate)
   *  @param opts.onSegmentEnd(seg)
   *  @param opts.warn
   *  @param opts.env {now(秒),setTimeout,clearTimeout}（测试注入手动时钟） */
  constructor({ onLine, onSegmentStart, onSegmentEnd, warn, env } = {}) {
    this.onLine = onLine || (() => {});
    this.onSegmentStart = onSegmentStart || (() => {});
    this.onSegmentEnd = onSegmentEnd || (() => {});
    this.warn = warn || console.warn;
    this.env = { ...DEFAULT_ENV, ...(env || {}) };
    this.queue = [];
    this.playing = null;
    this._timers = new Set(); // 触发即摘除（长挂防积压，09 §6 E5）
  }

  _after(ms, fn) {
    const id = this.env.setTimeout(() => {
      this._timers.delete(id);
      fn();
    }, ms);
    this._timers.add(id);
    return id;
  }

  /** 当前倍率：积压 >3 段 ×1.5 追平，否则 1.0（段开播时刻取值）。 */
  currentRate() {
    return this.queue.length > BACKLOG_THRESHOLD ? BACKLOG_RATE : BASE_RATE;
  }

  /** 事件入队；无展示文本 → false（零字幕项）。 */
  push(ev) {
    const seg = eventToSegment(ev);
    if (!seg) return false;
    if (seg.kind === 'dialogue') {
      for (const l of seg.lines) {
        if (l.text_display.length > LINE_MAX_LEN) {
          this.warn(`[subtitle] 单句 ${l.text_display.length} 字 > ${LINE_MAX_LEN}（01 §7），seq=${seg.seq}`);
        }
      }
    }
    this.queue.push(seg);
    if (!this.playing) this._playNext();
    return true;
  }

  _playNext() {
    const rate = this.currentRate(); // 积压判定含待播段（shift 前取）
    const seg = this.queue.shift();
    if (!seg) return;
    const startAt = this.env.now();
    this.playing = seg;
    this.onSegmentStart(seg, rate);
    for (const line of scheduleSegment(seg, startAt, rate)) {
      const delayMs = Math.max(0, (line.at - this.env.now()) * 1000);
      this._after(delayMs, () => this.onLine(line, seg));
    }
    const endMs = segmentDuration(seg, rate) * 1000;
    this._after(Math.max(0, startAt * 1000 + endMs - this.env.now() * 1000), () => {
      this.playing = null;
      this.onSegmentEnd(seg);
      this._playNext();
    });
  }

  /** 清空（resync 等场景；B9 缺口段不入队故正常不会触发）。 */
  clear() {
    for (const t of this._timers) this.env.clearTimeout(t);
    this._timers.clear();
    this.queue = [];
    this.playing = null;
  }
}

/** 字幕 meta 行：只放已出站键（句序/type/result，06 §7 B6）。 */
export function metaText(line, seg) {
  const parts = [`第 ${line.index + 1}/${line.total} 句`, seg.type];
  if (seg.result) parts.push(`result=${seg.result}`);
  return parts.join(' · ');
}
