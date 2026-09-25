/** T-LTV-03 字幕条 DOM 绑定（原型 .subtitle/.sub-chip/.sub-body 结构；安全区上方固定）。 */

import { metaText } from '../lib/subtitle.js';

/** 深色签名色上文字用浅色，浅色签名色上用深色（亮度阈值工程口径；色值取 02 §7.1 token）。 */
export function chipTextColor(hex) {
  const n = parseInt(hex.slice(1), 16);
  const lum = 0.299 * ((n >> 16) & 255) + 0.587 * ((n >> 8) & 255) + 0.114 * (n & 255);
  return lum >= 140 ? '#0E1116' : '#E6E9EF'; // bg-0 / text-0
}

export class SubtitleView {
  /** @param root  .subtitle 元素；@param roster T-ART-02 Roster（可为 null → 全兜底） */
  constructor(root, roster) {
    this.root = root;
    this.roster = roster;
    this.chip = root.querySelector('#sub-chip');
    this.chipName = root.querySelector('#sub-chip-name');
    this.text = root.querySelector('#sub-text');
    this.meta = root.querySelector('#sub-meta-text');
    this.dot = root.querySelector('#sub-dot');
  }

  showLine(line, seg) {
    this.root.hidden = false;
    if (line.speaker && this.roster) {
      const color = this.roster.colorOf(line.speaker); // 已提亮 10%（02 §7.1 深色底口径）
      this.chip.hidden = false;
      this.chip.style.background = color;
      this.chipName.style.color = chipTextColor(color);
      this.chipName.textContent = this.roster.nameOf(line.speaker);
      if (this.dot) this.dot.style.background = color;
    } else {
      this.chip.hidden = true; // 非对话单条字幕无说话人 chip
      if (this.dot) this.dot.style.background = '';
    }
    this.text.textContent = line.text; // 只写 text_display 出站文本（红线 7）
    this.meta.textContent = metaText(line, seg);
  }

  hide() {
    this.root.hidden = true; // 无字幕不占位（T-LTV-01）
  }
}
