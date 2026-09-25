/** T-LTV-02 引导接线：直播页 ← obs-api WS 增量事件通道。
 *
 * 启动序列：token 取自 URL ?token=（03 §8.3）→ 并行加载 roster（T-ART-02）/ map_layout.json
 * （T-ART-03 第三挂载）/ GET /api/snapshot（Day 标签初值，03 §5.1）→ WS 连接（T-LTV-02）。
 * 字幕（T-LTV-03）/特写（T-LTV-04）/形态（T-LTV-05）/飘屏（T-LTV-06）视图模块各自接线。
 * `?mock=1` 走本地录制脚本驱动同一组模块（截图验收用，不触 WS）。
 */

import { loadRoster } from './lib/roster.js';
import { SubtitleQueue } from './lib/subtitle.js';
import { ModeMachine } from './lib/mode.js';
import { SubtitleView } from './ui/subtitle-view.js';
import { PcardView } from './ui/pcard-view.js';
import { FloatView } from './ui/float-view.js';
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

/** 模拟墙钟 HH:MM（Asia/Shanghai +08，time_engine/clock.py LOCAL_TZ 口径；出站 sim_time 为 UTC ISO）。 */
export function fmtSimHHMM(simTime) {
  const d = new Date(simTime);
  if (Number.isNaN(d.getTime())) return String(simTime).slice(11, 16);
  return new Date(d.getTime() + 8 * 3600_000).toISOString().slice(11, 16);
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
    const hhmm = ctx.lastSimTime ? fmtSimHHMM(ctx.lastSimTime) : '--:--';
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
  if (params.get('anim') === '0') document.documentElement.classList.add('noanim');
  await bootstrap();
  // T-LTV-03 字幕引擎接线：事件 → 队列（逐句 ÷倍率）→ 字幕条 DOM
  const subView = new SubtitleView(document.getElementById('subtitle'), ctx.roster);
  // T-LTV-04 特写浮层：dialogue 开播弹说话人卡，speaker 切换换人，播完 +3s 收起（B4）
  const pcard = new PcardView(document.getElementById('pcard-mount'), ctx.roster, {
    getMode: () => ctx.mode?.current ?? 'normal', // T-LTV-05 mode 模块注入前恒 normal
  });
  const queue = new SubtitleQueue({
    onLine: (line, seg) => {
      subView.showLine(line, seg);
      if (seg.kind === 'dialogue') pcard.setSpeaker(line.speaker);
      // 逐句节拍留痕（T-LTV-03 验收 2：实测节奏对拍 at_offset_s）
      console.log(`[stream] line ${line.index + 1}/${line.total} @${(performance.now() / 1000).toFixed(2)}s seq=${seg.seq}`);
    },
    onSegmentStart: (seg) => { if (seg.kind === 'dialogue') pcard.begin(seg); },
    onSegmentEnd: (seg) => {
      subView.hide();
      if (seg.kind === 'dialogue') pcard.end();
    },
  });
  ctx.subtitleQueue = queue;
  // T-LTV-05 climax 形态：触发集判定（02 §7.6）→ 底图/名场面标签/滤镜/字幕描边切换；30s 回落（B4）
  const els = {
    phone: document.getElementById('phone'),
    stageNormal: document.getElementById('stage-normal'),
    stageClimax: document.getElementById('stage-climax'),
    tagClimax: document.getElementById('tag-climax'),
    tagClimaxText: document.getElementById('tag-climax-text'),
    warm: document.getElementById('warm-filter'),
    tense: document.getElementById('tense-filter'),
  };
  ctx.mode = new ModeMachine({
    onChange: (mode, info) => {
      const climax = mode === 'climax';
      els.stageNormal.hidden = climax;
      els.stageClimax.hidden = !climax;
      els.tagClimax.hidden = !climax;
      els.warm.hidden = !(climax && info?.kind === 'warm');
      els.tense.hidden = !(climax && info?.kind === 'tense');
      els.phone.classList.toggle('climax', climax);
      subView.setClimax(climax);
      if (climax) els.tagClimaxText.textContent = info?.label || '名场面';
      console.log(`[stream] mode → ${mode}${info ? `（${info.key}）` : ''}`);
    },
  });
  // 形态判定先于字幕/特写（弹卡时读取的已是新形态）
  ctx.handlers = [(_c, ev) => ctx.mode.feed(ev), (_c, ev) => queue.push(ev)];
  // T-LTV-06 飘屏占位（?float=0 整组隐藏；不接数据源，04 §6.4 口径）
  new FloatView(document.getElementById('float-mount'), { visible: params.get('float') !== '0' });
  if (params.get('mock')) {
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
  // ?from=<seq> 验收钩子：hello 携带 last_seq 起补推（T-LTV-02 resume 语义，03 §5.2）
  const from = Number(params.get('from'));
  if (Number.isFinite(from) && from > 0) client.lastSeq = from;
  client.connect();
  ctx.wsClient = client;

  // ?soak=1 长挂打点（T-LTV-07 验收 6 / 09 §6 E5）：每 30s 输出 heap 与事件计数
  if (params.get('soak') === '1') {
    let events = 0;
    ctx.handlers.push(() => { events += 1; });
    setInterval(() => {
      const heap = performance.memory ? Math.round(performance.memory.usedJSHeapSize / 1024) : -1;
      console.log(`[soak] heapKB=${heap} events=${events} queue=${queue.queue.length} playing=${queue.playing ? 1 : 0}`);
    }, 30_000);
  }
}

main().catch((e) => console.error('[stream] 启动失败：', e));
