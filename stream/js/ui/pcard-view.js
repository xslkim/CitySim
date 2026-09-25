/** T-LTV-04 人物特写浮层（DOM 绑定；原型 .pcard 结构，climax 双人栈位见 climax.css .pstack）。
 *
 * - 触发范围 = T-LTV-03 的 5 个 dialogue 类型；事件开播即弹当前句说话人卡，
 *   `lines[].speaker` 切换时 front 卡换人（入场动画沿用原型 pcard-in .45s）。
 * - normal 单人位（left:12 top:44）；climax 双人栈（front = 当前说话人，back = 对话另一方）。
 * - 收起 = 该事件字幕播完 + 3s 延迟（06 §7 B4 工程默认）；新 dialogue 开播即替换。
 * - 卡面只读已出站 payload 键（speaker/participants/teller/listener）与 roster 数据（红线 7）。
 */

import { chipTextColor } from './subtitle-view.js';

export const HIDE_DELAY_MS = 3000; // 播完延迟收起（06 §7 B4）

function cardNode(desc) {
  const fig = document.createElement('figure');
  fig.className = 'pcard';
  fig.style.setProperty('--sig', desc.color);
  fig.style.setProperty('--sigtext', chipTextColor(desc.color));
  if (desc.kind === 'portrait') {
    const img = document.createElement('img');
    img.src = desc.url;
    img.alt = `${desc.name}特写`;
    img.draggable = false;
    fig.appendChild(img);
  } else {
    const div = document.createElement('div');
    div.className = 'pcard-initial';
    const circle = document.createElement('i');
    circle.style.background = desc.color;
    circle.style.color = chipTextColor(desc.color);
    circle.textContent = desc.char;
    div.appendChild(circle);
    fig.appendChild(div);
  }
  const cap = document.createElement('figcaption');
  cap.textContent = desc.name;
  fig.appendChild(cap);
  return fig;
}

/** 对话另一方（双人栈 back）：participants 中非说话人；gossip 用 teller/listener。 */
export function otherParty(seg, speaker) {
  const pair = seg.teller && seg.listener ? [seg.teller, seg.listener] : seg.participants;
  return (pair || []).find((id) => id !== speaker) || null;
}

export class PcardView {
  /** @param mount  #pcard-mount；@param roster Roster（null → id 兜底）；
   *  @param getMode () => 'normal'|'climax'（T-LTV-05 mode 模块注入，缺省 normal） */
  constructor(mount, roster, { getMode, setTimeoutFn, clearTimeoutFn } = {}) {
    this.mount = mount;
    this.roster = roster;
    this.getMode = getMode || (() => 'normal');
    this._setTimeout = setTimeoutFn || ((fn, ms) => setTimeout(fn, ms));
    this._clearTimeout = clearTimeoutFn || ((id) => clearTimeout(id));
    this._speaker = null;
    this._seg = null;
    this._hideTimer = null;
  }

  _describe(id) {
    if (this.roster) return this.roster.describe(id);
    return { kind: 'initial', color: '#8A93A0', char: String(id).slice(0, 1), name: String(id) };
  }

  /** dialogue 段开播：弹当前句说话人卡（首句 speaker）。 */
  begin(seg) {
    if (this._hideTimer != null) { this._clearTimeout(this._hideTimer); this._hideTimer = null; }
    this._seg = seg;
    this._speaker = null;
    this.setSpeaker(seg.lines[0]?.speaker);
  }

  /** 说话人切换：front 卡换人；climax 双人栈 back = 另一方。 */
  setSpeaker(id) {
    if (!id || id === this._speaker || !this._seg) return;
    this._speaker = id;
    this.mount.textContent = '';
    const mode = this.getMode();
    const front = cardNode(this._describe(id));
    if (mode === 'climax') {
      const stack = document.createElement('div');
      stack.className = 'pstack';
      const other = otherParty(this._seg, id);
      if (other) {
        const back = cardNode(this._describe(other));
        back.classList.add('pcard-back');
        stack.appendChild(back);
      }
      front.classList.add('pcard-front');
      stack.appendChild(front);
      this.mount.appendChild(stack);
    } else {
      front.classList.add('pcard-single');
      this.mount.appendChild(front);
    }
  }

  /** 段播完：3s 后收起（B4）；新 dialogue 开播即 begin() 替换并取消计时。 */
  end() {
    if (this._hideTimer != null) this._clearTimeout(this._hideTimer);
    this._hideTimer = this._setTimeout(() => {
      this.mount.textContent = '';
      this._speaker = null;
      this._seg = null;
      this._hideTimer = null;
    }, HIDE_DELAY_MS);
  }
}
