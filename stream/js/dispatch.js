/** 事件派发（T-LTV-02 起逐任务接线）：标签态更新 + 按 type 路由到字幕/特写/形态模块。
 * 未注册 type 只记 console（03 §5.3 缺省规则精神，直播页无调试态）。
 */

import { renderTag } from './main.js';

export function dispatchEvent(ctx, ev) {
  if (!ev || typeof ev.type !== 'string') return;
  console.log('[stream] event', ev.seq, ev.type); // 验收留痕（seq 去重/重连补推断言依据）
  ctx.lastSimTime = ev.sim_time || ctx.lastSimTime;
  if (ev.payload?.location_id) ctx.lastLocationId = ev.payload.location_id;
  if (ev.type === 'time.day_summary' && Number.isFinite(ev.payload?.day)) {
    ctx.simDay = ev.payload.day; // Day 标签随日界更新（06 §1.2）
  }
  renderTag();
  for (const handler of ctx.handlers || []) handler(ctx, ev);
}
