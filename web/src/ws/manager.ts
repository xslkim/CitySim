/**
 * WS 连接管理器（03 §0.2 原生 WebSocket 自封装；03 §5.2 协议；React 外直写 store）。
 * - hello（token + 可选 last_seq）→ welcome；subscribe events/world_state/health；
 * - 断线重连指数退避 1s→2s→…→30s 封顶、±20% 抖动（merge.backoffMs）；重连 hello 带 last_seq；
 * - resync_required → 丢内存态走 /api/snapshot 重建（worldStore 整体替换 + timeline 截断，03 §6.2.3）；
 * - 心跳：每 30s ping（03 §5.2）。
 */
import { apiGet, getToken } from '../api/client';
import { snapshotSchema } from '../proto/snapshot';
import { serverFrameSchema } from '../proto/ws';
import { backoffMs } from '../stores/merge';
import { useTimelineStore } from '../stores/timelineStore';
import { useWorldStore } from '../stores/worldStore';

export const PING_INTERVAL_MS = 30_000; // 03 §5.2

export interface WsManagerOptions {
  url?: string;
  token?: string;
  backoff?: (attempt: number) => number; // 测试注入（确定性）
  pingIntervalMs?: number;
  onFrame?: (frame: unknown) => void;
}

export class WsManager {
  private ws: WebSocket | null = null;
  private attempt = 0;
  private pingTimer: ReturnType<typeof setInterval> | null = null;
  private stopped = false;
  private readonly url: string;
  private readonly token: string;
  private readonly backoff: (attempt: number) => number;
  private readonly pingIntervalMs: number;
  private readonly onFrame?: (frame: unknown) => void;

  constructor(opts: WsManagerOptions = {}) {
    const proto = window.location.protocol === 'https:' ? 'wss' : 'ws';
    this.url = opts.url ?? `${proto}://${window.location.host}/ws`;
    this.token = opts.token ?? getToken();
    this.backoff = opts.backoff ?? backoffMs;
    this.pingIntervalMs = opts.pingIntervalMs ?? PING_INTERVAL_MS;
    this.onFrame = opts.onFrame;
  }

  start(): void {
    this.stopped = false;
    this.connect();
  }

  stop(): void {
    this.stopped = true;
    if (this.pingTimer) clearInterval(this.pingTimer);
    this.ws?.close();
  }

  private connect(): void {
    const ws = new WebSocket(this.url);
    this.ws = ws;
    ws.onopen = () => {
      const lastSeq = useTimelineStore.getState().latestSeq;
      ws.send(JSON.stringify({
        op: 'hello',
        token: this.token,
        client: 'obs/1.0.0',
        ...(lastSeq > 0 ? { last_seq: lastSeq } : {}),
      }));
    };
    ws.onmessage = (m) => this.handle(m.data as string);
    ws.onclose = () => {
      if (this.pingTimer) clearInterval(this.pingTimer);
      if (this.stopped) return;
      const wait = this.backoff(this.attempt++);
      setTimeout(() => this.connect(), wait);
    };
    ws.onerror = () => ws.close();
  }

  private async handle(raw: string): Promise<void> {
    let parsed: unknown;
    try {
      parsed = JSON.parse(raw);
    } catch {
      return;
    }
    this.onFrame?.(parsed);
    const r = serverFrameSchema.safeParse(parsed);
    if (!r.success) return;
    const frame = r.data;
    switch (frame.op) {
      case 'welcome':
        useWorldStore.getState().setWatermarkTick(frame.watermark_tick);
        this.subscribeAll();
        this.pingTimer = setInterval(() => {
          this.ws?.send(JSON.stringify({ op: 'ping', t: Date.now() }));
        }, this.pingIntervalMs);
        break;
      case 'event':
        if (useTimelineStore.getState().live) {
          useTimelineStore.getState().append([frame.data]);
        } else {
          useTimelineStore.getState().append([frame.data]); // 历史模式也入缓冲（seq 幂等去重）
        }
        break;
      case 'state_diff':
        useWorldStore.getState().applyDiff(frame.data);
        break;
      case 'health':
        break; // health 页按需拉 REST；WS health 帧当前只作存活信号
      case 'pong':
        break;
      case 'resync_required':
        await this.resync();
        break;
    }
  }

  private subscribeAll(): void {
    this.ws?.send(JSON.stringify({
      op: 'subscribe',
      channels: [
        { name: 'events', filter: { types: [], actors: [], locations: [], grades: [] } },
        { name: 'world_state' },
        { name: 'health' },
      ],
    }));
  }

  /** resync：丢内存态 → /api/snapshot 全量重建（03 §5.2/§6.2.3）。 */
  async resync(): Promise<void> {
    const { data, meta } = await apiGet('/api/snapshot', snapshotSchema);
    useWorldStore.getState().setSnapshot(data);
    useWorldStore.getState().setWatermarkTick(meta.watermark_tick);
    useTimelineStore.getState().truncateForSnapshot(data.tick);
  }
}

export const wsManager = new WsManager();
