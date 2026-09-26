/**
 * 展示层时间格式化唯一入口（T-ITER2-05，round2 #5③）。
 *
 * 契约口径（docs/design/06-契约登记表.md v1.4）：API 序列化层不动——sim_time 恒为 UTC ISO（+00:00，
 * 无歧义）；展示层统一按 +08（Asia/Shanghai，模拟日聚合时区 01 §6 D4）本地化渲染。
 * 消灭散落的 toISOString / 裸 slice(11,16) / getHours() 本地时区直读。
 */

const SH_TZ = 'Asia/Shanghai';
const SH_OFFSET_MS = 8 * 3600_000;

const HHMM_FMT = new Intl.DateTimeFormat('zh-CN', {
  timeZone: SH_TZ, hour: '2-digit', minute: '2-digit', hour12: false,
});

const MMDD_PARTS = new Intl.DateTimeFormat('zh-CN', {
  timeZone: SH_TZ, month: '2-digit', day: '2-digit', hour12: false,
});

function mmdd(d: Date): string {
  const parts = Object.fromEntries(MMDD_PARTS.formatToParts(d).map((p) => [p.type, p.value]));
  return `${parts.month}-${parts.day}`;
}

/** ISO（任意带时区偏移）→ +08 "HH:MM"；非法输入回退裸切片兜底（06 §7 B8 同口径）。 */
export function formatSimHHMM(iso: string | null | undefined): string {
  if (!iso) return '—';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return String(iso).slice(11, 16);
  return HHMM_FMT.format(d);
}

/** ISO → +08 "MM-DD HH:MM"（详情页反思时间等日期+时刻场景）。 */
export function formatSimMMddHHMM(iso: string | null | undefined): string {
  if (!iso) return '—';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return String(iso).slice(0, 16).replace('T', ' ');
  return `${mmdd(d)} ${HHMM_FMT.format(d)}`;
}

/** ISO → +08 "YYYY-MM-DD"（sim_day 列同口径，跨日边界不再串档）。 */
export function formatSimYMD(iso: string | null | undefined): string {
  if (!iso) return '—';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return String(iso).slice(0, 10);
  const year = new Date(d.getTime() + SH_OFFSET_MS).getUTCFullYear();
  return `${year}-${mmdd(d)}`;
}
