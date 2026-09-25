/** T-LTV-02 引导接线：直播页 ← obs-api WS 增量事件通道。
 *
 * 启动序列：token 取自 URL ?token=（03 §8.3）→ 并行加载 roster（T-ART-02）/ map_layout.json
 * （T-ART-03 第三挂载）/ GET /api/snapshot（Day 标签初值，03 §5.1）→ WS 连接（T-LTV-02）。
 * 字幕（T-LTV-03）/特写（T-LTV-04）/形态（T-LTV-05）/飘屏（T-LTV-06）视图模块各自接线。
 * `?mock=1` 走本地录制脚本驱动同一组模块（截图验收用，不触 WS）。
 */

import { loadRoster } from './lib/roster.js';
import { SubtitleQueue } from './lib/subtitle.js';
import { SubtitleView } from './ui/subtitle-view.js';
import { StreamWsClient } from './lib/ws-client.js';

const params = new URLSearchParams(location.search);
const TOKEN = params.get('token') || '';
const BASE = location.origin;

export const ctx = {
  params,
  token: TOKEN,
  base: BASE,
  roster: null,       // T-ART-02 Roster
  locationNames: new Map(), // location_id → 中文名（03 §3.1）
  simDay: null,       // Day 标签（snapshot.sim_day 起，time.day_summary 更新）
  lastSimTime: null,  // HH:MM 取最近事件 sim_time（T-LTV-03）
};

async function fetchJson(url) {
  const resp = await fetch(url);
  if (!resp.ok) throw new Error(`GET ${url} → ${resp.status}`);
  return resp.json();
}

/** 左上标签：`"<地点中文名> · Day <n> · HH:MM"`（原型形态）。 */
export function renderTag() {
  const locEl = document.getElementById('tag-loc');
  const timeEl = document.getElementById('tag-time');
  if (locEl && ctx.lastLocationId) {
    locEl.textContent = ctx.locationNames.get(ctx.lastLocationId) || ctx.lastLocationId; // B8 降级原文
  }
  if (timeEl) {
    const day = ctx.simDay != null ? `Day ${ctx.simDay}` : 'Day –';
    const hhmm = ctx.lastSimTime ? ctx.lastSimTime.slice(11, 16) : '--:--';
    timeEl.textContent = `${day} · ${hhmm}`;
  }
}

async function bootstrap() {
  const [roster, layout] = await Promise.all([
    loadRoster({ base: BASE, token: TOKEN }).catch((e) => {
      console.warn('[stream] roster 加载失败，全员兜底色：', e.message);
      return null;
    }),
    fetchJson(`${BASE}/map_layout.json`).catch((e) => {
      console.warn('[stream] map_layout.json 加载失败（B8 降级 location_id 原文）：', e.message);
      return null;
    }),
  ]);
  ctx.roster = roster;
  if (layout) {
    for (const site of layout.sites || []) {
      if (site.id && site.name) ctx.locationNames.set(site.id, site.name);
      for (const key of ['rooms', 'commons', 'zones']) {
        for (const r of site[key] || []) ctx.locationNames.set(r.id, r.name);
      }
    }
  }
  try {
    const snap = await fetchJson(`${BASE}/api/snapshot?token=${encodeURIComponent(TOKEN)}`);
    ctx.simDay = snap?.data?.sim_day ?? null;
  } catch (e) {
    console.warn('[stream] snapshot 加载失败：', e.message);
  }
  renderTag();
}

async function main() {
  await bootstrap();
  // T-LTV-03 字幕引擎接线：事件 → 队列（逐句 ÷倍率）→ 字幕条 DOM
  const subView = new SubtitleView(document.getElementById('subtitle'), ctx.roster);
  const queue = new SubtitleQueue({
    onLine: (line, seg) => subView.showLine(line, seg),
    onSegmentEnd: () => subView.hide(),
  });
  ctx.subtitleQueue = queue;
  ctx.handlers = [(_c, ev) => queue.push(ev)];
  if (params.get('mock') === '1') {
    const { runMock } = await import('./mock.js');
    runMock(ctx);
    return;
  }
  if (!TOKEN) {
    console.warn('[stream] 缺 ?token=（03 §8.3），仅静态画面');
    return;
  }
  const { dispatchEvent } = await import('./dispatch.js');
  const wsUrl = `${BASE.replace(/^http/, 'ws')}/ws`;
  const client = new StreamWsClient({
    url: wsUrl,
    token: TOKEN,
    onEvent: (ev) => dispatchEvent(ctx, ev),
    onWelcome: (w) => console.log('[stream] welcome', w.session_id, 'watermark', w.watermark_tick),
    onResync: async () => {
      // B9：resync 只重建标签态（snapshot），不回放缺口段字幕
      try {
        const snap = await fetchJson(`${BASE}/api/snapshot?token=${encodeURIComponent(TOKEN)}`);
        ctx.simDay = snap?.data?.sim_day ?? ctx.simDay;
        renderTag();
      } catch (e) { console.warn('[stream] resync snapshot 失败：', e.message); }
    },
    onStateChange: (s) => console.log('[stream] ws state:', s),
  });
  client.connect();
}

main().catch((e) => console.error('[stream] 启动失败：', e));
