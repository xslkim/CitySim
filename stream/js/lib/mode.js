/** T-LTV-05 climax 形态触发判定 + 状态机（纯逻辑；02 §7.6 触发集、04 §6.6 有效 grade 口径、
 *  06 §2 裸 seq 数字字符串比较、00 §4 红线 2/3/5）。
 *
 * 触发集（事件经 WS 到达即评估，全部条件只用 06 注册键）：
 *  ① 任意事件 ui.grade='A'；或 director.grade_revise 且 payload.new_grade='A'（'B' 不触发）
 *  ② dialogue.confess 且 payload.result='accepted'
 *  ③ 打脸三选一：social.appointment.stood_up；world.perf_review 且 ui.grade='A'；
 *     dialogue.gossip 且 payload.about === payload.listener
 *  ④ economy.stock.tick 且 |payload.r| ≥ 3%（r 为小数收益率，阈值 0.03，口径 02 §7.6）；
 *     或 economy.payroll
 *  ⑤ dialogue.argue 且存在同源 relation.changed：payload.changes[] 中 cause = 该 argue 事件 seq
 *     （裸 seq 数字字符串比较）且 delta_tension ≥8
 * 状态机：触发即入 climax 并重置回落计时器；最后触发后 30s（06 §7 B4 工程默认）无新触发 → normal。
 */

export const FALLBACK_MS = 30_000; // 回落计时器（06 §7 B4）
export const STOCK_SHOCK_R = 0.03; // |r| ≥ 3%（02 §7.6；r 为小数收益率，economy.py 口径）
export const ARGUE_TENSION_MIN = 8;  // delta_tension ≥8（02 §7.6）
const ARGUE_MEMORY = 32;             // 同源匹配窗口（近 32 条 argue；工程默认）

/** 单事件触发判定（⑤ 需跨事件，由 ModeMachine 状态承载）。返回触发信息或 null。 */
export function evaluateTrigger(ev, recentArgues = new Set()) {
  const type = ev?.type;
  const p = ev?.payload || {};
  const gradeA = ev?.ui?.grade === 'A';
  // ① A 级（有效 grade 口径：初值 ui.grade / 改判 new_grade，04 §6.6）
  if (type === 'director.grade_revise') {
    if (p.new_grade === 'A') return { key: 'grade_revise', kind: null, label: '名场面 · 编剧升格' };
    return null; // new_grade='B' 不触发
  }
  // ② 告白成功（具体触发优先于 ① A 级通用项，label/滤镜取告白口径）
  if (type === 'dialogue.confess' && p.result === 'accepted') {
    return { key: 'confess', kind: 'warm', label: '名场面 · 告白成功' };
  }
  // ③ 打脸三选一
  if (type === 'social.appointment.stood_up') return { key: 'stood_up', kind: 'tense', label: '名场面 · 反转' };
  if (type === 'dialogue.gossip' && p.about && p.about === p.listener) {
    return { key: 'gossip_echo', kind: 'tense', label: '名场面 · 当面辟谣' };
  }
  // ④ 股价暴涨暴跌 / 发工资
  if (type === 'economy.stock.tick' && Number.isFinite(p.r) && Math.abs(p.r) >= STOCK_SHOCK_R) {
    return { key: 'stock_shock', kind: null, label: '名场面 · 股价剧震' };
  }
  if (type === 'economy.payroll') return { key: 'payroll', kind: null, label: '名场面 · 发薪日' };
  // ⑤ 争执升级：relation.changed 的 changes[].cause 命中近窗 argue seq（裸 seq 数字字符串）
  if (type === 'relation.changed' && Array.isArray(p.changes)) {
    const hit = p.changes.some((c) => recentArgues.has(String(c?.cause))
      && Number(c?.delta_tension) >= ARGUE_TENSION_MIN);
    if (hit) return { key: 'argue_tension', kind: 'tense', label: '名场面 · 争执升级' };
  }
  // ① A 级兜底（有效 grade 口径：初值 ui.grade / 改判 new_grade，04 §6.6）
  if (gradeA) return { key: 'grade_a', kind: null, label: '名场面 · A 级事件' };
  return null;
}

const DEFAULT_ENV = {
  setTimeout: (fn, ms) => setTimeout(fn, ms),
  clearTimeout: (id) => clearTimeout(id),
};

export class ModeMachine {
  /** @param opts.onChange(mode, info) mode='normal'|'climax'；info=触发信息（回落时 null） */
  constructor({ onChange, fallbackMs = FALLBACK_MS, env } = {}) {
    this.onChange = onChange || (() => {});
    this.fallbackMs = fallbackMs;
    this.env = { ...DEFAULT_ENV, ...(env || {}) };
    this.current = 'normal';
    this._recentArgues = []; // FIFO 窗口（插入序保序）
    this._argueSet = new Set();
    this._timer = null;
  }

  /** 事件喂入；触发 → 入 climax 并返回触发信息，否则 null。 */
  feed(ev) {
    if (ev?.type === 'dialogue.argue') {
      this._recentArgues.push(String(ev.seq));
      this._argueSet.add(String(ev.seq));
      if (this._recentArgues.length > ARGUE_MEMORY) {
        this._argueSet.delete(this._recentArgues.shift());
      }
    }
    const info = evaluateTrigger(ev, this._argueSet);
    if (info) this._enter(info);
    return info;
  }

  _enter(info) {
    if (this._timer != null) this.env.clearTimeout(this._timer);
    this._timer = this.env.setTimeout(() => this._fall(), this.fallbackMs);
    if (this.current !== 'climax') {
      this.current = 'climax';
      this.onChange('climax', info);
    }
  }

  _fall() {
    this._timer = null;
    this.current = 'normal';
    this.onChange('normal', null);
  }
}
