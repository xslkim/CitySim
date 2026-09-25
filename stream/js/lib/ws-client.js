/** T-LTV-02 直播页 WS 客户端（零依赖原生 WebSocket；03 §5.2 全帧型 + 03 §8.3 token 鉴权）。
 *
 * 纯逻辑模块：帧编解码 + 连接状态机 + 心跳/看门狗 + 指数退避，不触 DOM。
 * 环境注入（env）便于 node --test 用 mock WebSocket 与手动时钟驱动。
 *
 * 协议要点（03 §5.2）：
 * - 连接后首帧 hello（token + client + 可选 last_seq）；welcome 后 subscribe events 频道（空过滤=全量；
 *   不订阅 world_state——直播页不渲染 pawn，06 §2）。
 * - event 帧按 seq 去重（00 §4 红线 2：续传/去重只用单调 seq）。
 * - resync_required → onResync 回调（引导层走 GET /api/snapshot 重建标签态，不回放缺口段字幕，06 §7 B9）。
 * - 心跳：每 30s ping；90s 无任何入站帧判定断线 → 重连；退避 1s→2s→…→30s 封顶 ±20% 抖动。
 */

export const CLIENT_ID = 'stream/1.0.0';
export const PING_INTERVAL_MS = 30_000;   // 03 §5.2
export const HEARTBEAT_TIMEOUT_MS = 90_000; // 03 §5.2

/** 指数退避计算器：base→×2→…→max 封顶，±jitter 抖动（03 §5.2 数字原样）。 */
export function createBackoff({ baseMs = 1000, maxMs = 30_000, jitter = 0.2, rand = Math.random } = {}) {
  let attempt = 0;
  return {
    next() {
      const raw = Math.min(baseMs * 2 ** attempt, maxMs);
      attempt += 1;
      return Math.round(raw * (1 + (rand() * 2 - 1) * jitter));
    },
    reset() { attempt = 0; },
  };
}

const DEFAULT_ENV = {
  WebSocket: typeof WebSocket !== 'undefined' ? WebSocket : undefined,
  setTimeout: (fn, ms) => setTimeout(fn, ms),
  clearTimeout: (id) => clearTimeout(id),
  now: () => Date.now(),
};

export class StreamWsClient {
  /** @param opts.url   ws(s)://…/ws
   *  @param opts.token  dev token（03 §8.3；页面取自 URL ?token=）
   *  @param opts.onEvent(ev)   去重后的事件帧 data（含 seq/tick/sim_time/type/payload/ui）
   *  @param opts.onResync()    resync_required 回调（引导层重建标签态）
   *  @param opts.onWelcome(w)  welcome 帧（watermark_tick/server_time/session_id）
   *  @param opts.onStateChange(state) 'connecting'|'open'|'closed'
   *  @param opts.env    {WebSocket,setTimeout,clearTimeout,now}（测试注入）
   *  @param opts.log    console 风格 logger */
  constructor({ url, token, onEvent, onResync, onWelcome, onStateChange, env, log } = {}) {
    this.url = url;
    this.token = token;
    this.onEvent = onEvent || (() => {});
    this.onResync = onResync || (() => {});
    this.onWelcome = onWelcome || (() => {});
    this.onStateChange = onStateChange || (() => {});
    this.env = { ...DEFAULT_ENV, ...(env || {}) };
    this.log = log || console;
    this.lastSeq = 0;
    this.state = 'closed';
    this.backoff = createBackoff({ rand: (env || {}).rand });
    this._ws = null;
    this._pingTimer = null;
    this._watchTimer = null;
    this._reconnectTimer = null;
    this._lastRx = 0;
    this._closedByUser = false;
  }

  connect() {
    this._closedByUser = false;
    this._setState('connecting');
    const WS = this.env.WebSocket;
    const ws = new WS(this.url);
    this._ws = ws;
    ws.onopen = () => this._onOpen();
    ws.onmessage = (msg) => this._onMessage(msg);
    ws.onclose = () => this._onClose();
    ws.onerror = () => {}; // 关闭事件紧随，统一走 onclose 重连
  }

  close() {
    this._closedByUser = true;
    this._clearTimers();
    if (this._ws) {
      this._ws.onclose = null;
      this._ws.close();
    }
    this._setState('closed');
  }

  _setState(s) {
    this.state = s;
    this.onStateChange(s);
  }

  _onOpen() {
    const hello = { op: 'hello', token: this.token, client: CLIENT_ID };
    if (this.lastSeq > 0) hello.last_seq = this.lastSeq; // resume-from-seq（03 §5.2）
    this._send(hello);
    this._lastRx = this.env.now();
  }

  _onMessage(msg) {
    this._lastRx = this.env.now();
    let frame;
    try {
      frame = JSON.parse(msg.data);
    } catch {
      this.log.warn('[ws] 非 JSON 帧，忽略');
      return;
    }
    switch (frame.op) {
      case 'welcome':
        this._setState('open');
        this.backoff.reset();
        this.onWelcome(frame);
        this._send({
          op: 'subscribe',
          channels: [{ name: 'events', filter: { types: [], actors: [], locations: [], grades: [] } }],
        });
        this._startHeartbeat();
        break;
      case 'event': {
        const seq = Number(frame.seq ?? frame.data?.seq);
        if (!Number.isFinite(seq)) return;
        if (seq <= this.lastSeq) return; // seq 去重（红线 2）
        this.lastSeq = seq;
        this.onEvent(frame.data ?? frame);
        break;
      }
      case 'pong':
        break; // _lastRx 已刷新
      case 'resync_required':
        this.log.warn('[ws] resync_required：缺口超环缓冲，引导层重建（B9 不回放缺口段字幕）');
        this.onResync();
        break;
      case 'error':
        this.log.warn('[ws] error 帧', frame.error);
        break;
      default:
        this.log.warn('[ws] 未识别 op，忽略', frame.op);
    }
  }

  _onClose() {
    if (this._closedByUser) return;
    if (this._reconnectTimer != null) return; // 重连已排程，去重（watchdog+onclose 双触发）
    this._clearTimers();
    this._setState('closed');
    const delay = this.backoff.next();
    this.log.warn(`[ws] 断线，${delay}ms 后重连（last_seq=${this.lastSeq}）`);
    this._reconnectTimer = this.env.setTimeout(() => {
      this._reconnectTimer = null;
      this.connect();
    }, delay);
  }

  _startHeartbeat() {
    this._clearTimers();
    const loop = () => {
      if (this.env.now() - this._lastRx > HEARTBEAT_TIMEOUT_MS) {
        this.log.warn('[ws] 90s 无入站帧，判定断线');
        try { this._ws?.close(); } catch { /* mock 容错 */ }
        this._onClose(); // close() 异步 onclose 双触发由重连定时器去重
        return;
      }
      this._send({ op: 'ping', t: this.env.now() });
      this._pingTimer = this.env.setTimeout(loop, PING_INTERVAL_MS);
    };
    this._pingTimer = this.env.setTimeout(loop, PING_INTERVAL_MS);
  }

  _clearTimers() {
    for (const key of ['_pingTimer', '_watchTimer', '_reconnectTimer']) {
      if (this[key] != null) {
        this.env.clearTimeout(this[key]);
        this[key] = null;
      }
    }
  }

  _send(frame) {
    if (!this._ws) return;
    this._ws.send(JSON.stringify(frame));
  }
}
