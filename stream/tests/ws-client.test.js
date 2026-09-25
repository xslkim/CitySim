/** T-LTV-02 ws-client.js 单测（node --test；mock WebSocket + 手动时钟；03 §5.2 帧序列/退避/心跳/去重）。 */

import test from 'node:test';
import assert from 'node:assert/strict';

import { CLIENT_ID, StreamWsClient, createBackoff } from '../js/lib/ws-client.js';

/** 手动时钟 + 定时器注册表 + mock WebSocket 工厂。 */
function makeEnv({ rand = () => 0.5 } = {}) {
  let now = 0;
  let nextId = 1;
  const timers = new Map();
  const sockets = [];
  class MockWS {
    constructor(url) {
      this.url = url;
      this.sent = [];
      this.closed = false;
      sockets.push(this);
    }

    send(data) { this.sent.push(JSON.parse(data)); }

    close() { this.closed = true; }

    open() { this.onopen?.(); }

    receive(frame) { this.onmessage?.({ data: JSON.stringify(frame) }); }

    drop() { this.onclose?.(); }
  }
  const env = {
    WebSocket: MockWS,
    rand,
    now: () => now,
    setTimeout: (fn, ms) => { const id = nextId++; timers.set(id, { at: now + ms, fn }); return id; },
    clearTimeout: (id) => { timers.delete(id); },
    /** 推进时钟并触发到期定时器（链式触发安全）。 */
    tick(ms) {
      now += ms;
      for (;;) {
        const due = [...timers.entries()].filter(([, t]) => t.at <= now)
          .sort((a, b) => a[1].at - b[1].at);
        if (!due.length) break;
        const [id, t] = due[0];
        timers.delete(id);
        t.fn();
      }
    },
  };
  return { env, sockets };
}

function connectAndGreet(client, sockets, { receiveEvents = [] } = {}) {
  client.connect();
  const ws = sockets.at(-1);
  ws.open();
  for (const ev of receiveEvents) ws.receive({ op: 'event', seq: ev.seq, data: ev });
  ws.receive({ op: 'welcome', watermark_tick: 100, server_time: 't', session_id: 's_x' });
  return ws;
}

const NOOP_LOG = { warn: () => {}, info: () => {} };

test('hello → welcome → subscribe 帧序列（03 §5.2）', () => {
  const { env, sockets } = makeEnv();
  const c = new StreamWsClient({ url: 'ws://x/ws', token: 'dev_t', env, log: NOOP_LOG });
  const ws = connectAndGreet(c, sockets);
  assert.deepEqual(ws.sent[0], { op: 'hello', token: 'dev_t', client: CLIENT_ID });
  assert.deepEqual(ws.sent[1], {
    op: 'subscribe',
    channels: [{ name: 'events', filter: { types: [], actors: [], locations: [], grades: [] } }],
  });
});

test('重连 hello 携带 last_seq（resume-from-seq）', () => {
  const { env, sockets } = makeEnv();
  const c = new StreamWsClient({ url: 'ws://x/ws', token: 'dev_t', env, log: NOOP_LOG });
  connectAndGreet(c, sockets, { receiveEvents: [{ seq: 41 }, { seq: 42 }] });
  sockets.at(-1).drop();
  env.tick(1000); // 退避 1s（rand=0.5 → 抖动 1.0）
  assert.equal(sockets.length, 2);
  sockets.at(-1).open();
  assert.equal(sockets.at(-1).sent[0].last_seq, 42);
});

test('退避序列落在 [0.8,1.2]×(1,2,4,8,16,30,30…) 区间', () => {
  const expected = [1, 2, 4, 8, 16, 30, 30, 30];
  for (const rand of [() => 0, () => 0.5, () => 1]) {
    const b = createBackoff({ rand });
    expected.forEach((s, i) => {
      const ms = b.next();
      assert.ok(ms >= s * 1000 * 0.8 && ms <= s * 1000 * 1.2,
        `rand 档 ${i}: ${ms} 不在 [${s * 800},${s * 1200}]`);
    });
  }
});

test('心跳 30s ping；90s 无入站帧触发重连', () => {
  const { env, sockets } = makeEnv();
  const c = new StreamWsClient({ url: 'ws://x/ws', token: 'dev_t', env, log: NOOP_LOG });
  const ws = connectAndGreet(c, sockets);
  env.tick(30_000);
  assert.equal(ws.sent.at(-1).op, 'ping');
  ws.receive({ op: 'pong', t: 0 });
  env.tick(30_000); // t=60：pong 在 30 刷新 → 只 ping
  assert.equal(ws.sent.at(-1).op, 'ping');
  env.tick(30_000); // t=90
  env.tick(30_001); // t=120：距 lastRx(30) 90.001s > 90s → 断线
  assert.equal(sockets.length, 1); // 重连排在退避后
  env.tick(1000);
  assert.equal(sockets.length, 2); // 重连发生
});

test('重复 seq 事件去重（红线 2）', () => {
  const { env, sockets } = makeEnv();
  const seen = [];
  const c = new StreamWsClient({ url: 'ws://x/ws', token: 'dev_t', env, log: NOOP_LOG, onEvent: (e) => seen.push(e) });
  connectAndGreet(c, sockets);
  const ws = sockets.at(-1);
  ws.receive({ op: 'event', seq: 7, data: { seq: 7, type: 'dialogue.chat' } });
  ws.receive({ op: 'event', seq: 7, data: { seq: 7, type: 'dialogue.chat' } });
  ws.receive({ op: 'event', seq: 5, data: { seq: 5 } }); // 乱序旧帧
  ws.receive({ op: 'event', seq: 8, data: { seq: 8 } });
  assert.deepEqual(seen.map((e) => e.seq), [7, 8]);
  assert.equal(c.lastSeq, 8);
});

test('resync_required → onResync 回调（B9：引导层重建，不回放字幕）', () => {
  const { env, sockets } = makeEnv();
  let resynced = 0;
  const c = new StreamWsClient({ url: 'ws://x/ws', token: 'dev_t', env, log: NOOP_LOG, onResync: () => { resynced += 1; } });
  const ws = connectAndGreet(c, sockets);
  ws.receive({ op: 'resync_required' });
  assert.equal(resynced, 1);
});

test('未识别 op 只记日志不抛错（03 §5.3 缺省精神）', () => {
  const { env, sockets } = makeEnv();
  const c = new StreamWsClient({ url: 'ws://x/ws', token: 'dev_t', env, log: NOOP_LOG });
  const ws = connectAndGreet(c, sockets);
  ws.receive({ op: 'mystery' });
  ws.receive({ data: 'not json' });
  assert.equal(c.state, 'open');
});
