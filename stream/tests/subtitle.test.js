/** T-LTV-03 字幕引擎单测（node --test；06 §2 逐句节拍、B3/B4 工程默认、红线 7/9）。 */

import test from 'node:test';
import assert from 'node:assert/strict';

import {
  BACKLOG_RATE, BASE_RATE, eventToSegment, metaText,
  scheduleSegment, segmentDuration, SubtitleQueue,
} from '../js/lib/subtitle.js';

function chat7() {
  return {
    seq: 101, type: 'dialogue.chat',
    payload: {
      participants: ['A01', 'A02'],
      lines: Array.from({ length: 7 }, (_, i) => ({
        speaker: i % 2 ? 'A02' : 'A01',
        text_display: `第${i + 1}句`,
        at_offset_s: i * 3,
      })),
    },
  };
}

function makeEnv() {
  let now = 0;
  let nextId = 1;
  const timers = new Map();
  return {
    env: {
      now: () => now,
      setTimeout: (fn, ms) => { const id = nextId++; timers.set(id, { at: now + ms / 1000, fn }); return id; },
      clearTimeout: (id) => { timers.delete(id); },
    },
    tick(s) {
      const target = now + s;
      for (;;) {
        const due = [...timers.entries()].filter(([, t]) => t.at <= target).sort((a, b) => a[1].at - b[1].at);
        if (!due.length) break;
        const [id, t] = due[0];
        timers.delete(id);
        now = Math.max(now, t.at); // 逐定时器推进时钟（段内回调读到真实触发时刻）
        t.fn();
      }
      now = target;
    },
  };
}

test('7 句 dialogue.chat 时刻表 = at_offset_s ÷ 倍率（1.0 原值；1.5 ÷1.5）', () => {
  const seg = eventToSegment(chat7());
  const t1 = scheduleSegment(seg, 100, BASE_RATE).map((l) => l.at);
  assert.deepEqual(t1, [100, 103, 106, 109, 112, 115, 118]);
  const t15 = scheduleSegment(seg, 100, BACKLOG_RATE).map((l) => l.at);
  assert.deepEqual(t15, [100, 102, 104, 106, 108, 110, 112]);
});

test('两段对话串行：第二段开始时刻 ≥ 前段末句时刻', () => {
  const { env, tick } = makeEnv();
  const starts = [];
  const q = new SubtitleQueue({ env, onSegmentStart: (seg, rate) => starts.push([seg.seq, env.now(), rate]) });
  q.push(chat7());
  const seg2 = { ...chat7(), seq: 102 };
  tick(1);
  q.push(seg2);
  // 段 1：末句 at=18，驻留 4s → 段 2 在 t=22 开播（≥ 18）
  tick(30);
  assert.deepEqual(starts.map(([seq]) => seq), [101, 102]);
  assert.ok(starts[1][1] >= 18, `第二段开始 ${starts[1][1]} < 前段末句 18`);
  assert.equal(starts[1][1], 22);
});

test('积压 4 段自动 ×1.5 追平，清空后回落 1.0', () => {
  const { env, tick } = makeEnv();
  const rates = [];
  const q = new SubtitleQueue({ env, onSegmentStart: (seg, rate) => rates.push(rate) });
  const single = (seq) => ({ seq, type: 'world.announce', payload: { text_display: `公告${seq}` } });
  q.push(single(1)); // 立即开播（4s）
  q.push(single(2)); q.push(single(3)); q.push(single(4)); q.push(single(5)); // 积压 4
  tick(100);
  assert.deepEqual(rates, [BASE_RATE, BACKLOG_RATE, BASE_RATE, BASE_RATE, BASE_RATE]);
});

test('internal/无 text_display 事件零字幕项（红线 7）', () => {
  assert.equal(eventToSegment({ seq: 1, type: 'state.needs_delta', payload: {} }), null);
  assert.equal(eventToSegment({ seq: 2, type: 'relation.changed', payload: { changes: [] } }), null);
  assert.equal(eventToSegment({ seq: 3, type: 'world.announce', payload: { title: 't' } }), null);
  // internal 对话：conditional 键 text_display 已剥除 → lines 无展示文本 → null
  assert.equal(eventToSegment({
    seq: 4, type: 'dialogue.chat',
    payload: { participants: ['A01', 'A02'], lines: [{ speaker: 'A01', at_offset_s: 0 }] },
  }), null);
  const q = new SubtitleQueue({ env: makeEnv().env });
  assert.equal(q.push({ seq: 5, type: 'time.day_summary', payload: { day: 3 } }), false);
});

test('非对话 text_display 单条字幕 4s', () => {
  const { env, tick } = makeEnv();
  const lines = [];
  let ended = 0;
  const q = new SubtitleQueue({
    env, onLine: (l) => lines.push(l), onSegmentEnd: () => { ended += 1; },
  });
  q.push({ seq: 9, type: 'economy.payroll', payload: { text_display: '发工资了' } });
  tick(0.1);
  assert.equal(lines.length, 1);
  assert.equal(lines[0].speaker, null);
  assert.equal(lines[0].text, '发工资了');
  assert.equal(segmentDuration(eventToSegment({ type: 'x', payload: { text_display: 'y' } }), 1), 4);
  tick(4);
  assert.equal(ended, 1);
});

test('meta 行只放已出站键（B6：句序/type/result）', () => {
  const seg = eventToSegment({
    seq: 11, type: 'dialogue.confess',
    payload: { participants: ['A02', 'A03'], result: 'accepted', lines: [{ speaker: 'A03', text_display: '……我也是。', at_offset_s: 0 }] },
  });
  assert.equal(metaText(scheduleSegment(seg, 0, 1)[0], seg), '第 1/1 句 · dialogue.confess · result=accepted');
});

test('单句 >40 字告警但不截断（01 §7 由内核保证）', () => {
  const warnings = [];
  const { env, tick } = makeEnv();
  const lines = [];
  const q = new SubtitleQueue({ env, warn: (m) => warnings.push(m), onLine: (l) => lines.push(l) });
  const long = '字'.repeat(41);
  q.push({ seq: 12, type: 'dialogue.chat', payload: { lines: [{ speaker: 'A01', text_display: long, at_offset_s: 0 }] } });
  tick(0.1);
  assert.equal(warnings.length, 1);
  assert.equal(lines[0].text, long);
});
