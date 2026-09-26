/** 展示层时间唯一入口（T-ITER2-05 + R3 #6；与 web/src/lib/simTime 同口径：UTC ISO → +08 本地渲染）。
 *
 * stream 侧原内联于 main.js（fmtSimHHMM）；R3 #6 昼夜判定同一入口派生（不新起炉灶），
 * 并独立成模块供 node --test 直测（main.js 顶层有 DOM 依赖不可直引）。
 */

const SH_HHMM = new Intl.DateTimeFormat('zh-CN', {
  timeZone: 'Asia/Shanghai', hour: '2-digit', minute: '2-digit', hour12: false,
});

/** 模拟墙钟 HH:MM（出站 sim_time 为 UTC ISO，渲染一律 +08）。 */
export function fmtSimHHMM(simTime) {
  const d = new Date(simTime);
  if (Number.isNaN(d.getTime())) return String(simTime).slice(11, 16);
  return SH_HHMM.format(d);
}

const SH_HOUR = new Intl.DateTimeFormat('zh-CN', {
  timeZone: 'Asia/Shanghai', hour: 'numeric', hour12: false,
});

/** 昼夜判定（R3 #6）：06:00~19:00 日景、19:00~次日 06:00 夜景。
 *  验收矩阵：13:00/18:00 日景，19:00/02:00 夜景。无时钟信号默认日景（B8 降级不闪变）。 */
export function isDaySim(simTime) {
  const d = new Date(simTime);
  if (Number.isNaN(d.getTime())) return true;
  const m = SH_HOUR.format(d).match(/\d+/);
  const h = (m ? Number(m[0]) : 12) % 24; // zh-CN 午夜边缘可能给 "24"
  return h >= 6 && h < 19;
}
