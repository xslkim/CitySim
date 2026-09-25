/** T-LTV-05 mode.js 单测（node --test；02 §7.6 五类触发、04 §6.6 有效 grade、B4 回落计时）。 */

import test from 'node:test';
import assert from 'node:assert/strict';

import { evaluateTrigger, ModeMachine } from '../js/lib/mode.js';

function makeEnv() {
  let now = 0;
  let nextId = 1;
  const timers = new Map();
  return {
    env: {
      setTimeout: (fn, ms) => { const id = nextId++; timers.set(id, { at: now + ms, fn }); return id; },
      clearTimeout: (id) => { timers.delete(id); },
    },
    tick(ms) {
      const target = now + ms;
      for (;;) {
        const due = [...timers.entries()].filter(([, t]) => t.at <= target).sort((a, b) => a[1].at - b[1].at);
        if (!due.length) break;
        const [id, t] = due[0];
        timers.delete(id);
        now = Math.max(now, t.at);
        t.fn();
      }
      now = target;
    },
  };
}

test('① ui.grade=A 触发；director.grade_revise new_grade=A 触发、=B 不触发（04 §6.6）', () => {
  assert.equal(evaluateTrigger({ type: 'dialogue.chat', ui: { grade: 'A' }, payload: {} })?.key, 'grade_a');
  assert.equal(evaluateTrigger({ type: 'dialogue.chat', ui: { grade: 'B' }, payload: {} }), null);
  assert.equal(evaluateTrigger({ type: 'director.grade_revise', payload: { target_seq: '1089', new_grade: 'A', reason: 'r' } })?.key, 'grade_revise');
  assert.equal(evaluateTrigger({ type: 'director.grade_revise', payload: { target_seq: '1089', new_grade: 'B', reason: 'r' } }), null);
});

test('② confess result=accepted 触发（kind=warm）；rejected 不触发', () => {
  const t = evaluateTrigger({ type: 'dialogue.confess', payload: { result: 'accepted', lines: [] } });
  assert.equal(t?.key, 'confess');
  assert.equal(t?.kind, 'warm');
  assert.equal(evaluateTrigger({ type: 'dialogue.confess', payload: { result: 'rejected' } }), null);
});

test('③ 打脸三选一', () => {
  assert.equal(evaluateTrigger({ type: 'social.appointment.stood_up', payload: {} })?.key, 'stood_up');
  assert.equal(evaluateTrigger({ type: 'world.perf_review', ui: { grade: 'A' }, payload: {} })?.key, 'grade_a');
  assert.equal(evaluateTrigger({ type: 'dialogue.gossip', payload: { teller: 'A01', listener: 'A03', about: 'A03' } })?.key, 'gossip_echo');
  assert.equal(evaluateTrigger({ type: 'dialogue.gossip', payload: { teller: 'A01', listener: 'A03', about: 'A04' } }), null);
});

test('④ stock.tick |r|≥3% 触发（r 小数收益率）；payroll 触发', () => {
  assert.equal(evaluateTrigger({ type: 'economy.stock.tick', payload: { r: 0.031 } })?.key, 'stock_shock');
  assert.equal(evaluateTrigger({ type: 'economy.stock.tick', payload: { r: -0.05 } })?.key, 'stock_shock');
  assert.equal(evaluateTrigger({ type: 'economy.stock.tick', payload: { r: 0.029 } }), null);
  assert.equal(evaluateTrigger({ type: 'economy.payroll', payload: { amount_cents: 100 } })?.key, 'payroll');
});

test('⑤ argue + 同源 relation.changed（cause 裸 seq 字符串 + delta_tension≥8）触发；不匹配不触发', () => {
  const { env, tick } = makeEnv();
  const changes = [];
  const m = new ModeMachine({ env, onChange: (mode, info) => changes.push([mode, info?.key ?? null]) });
  m.feed({ seq: 500, type: 'dialogue.argue', payload: { lines: [] } });
  // cause 不匹配 → 不触发
  m.feed({ seq: 501, type: 'relation.changed', payload: { changes: [{ a_id: 'A01', b_id: 'A02', cause: '499', delta_tension: 9 }] } });
  assert.equal(m.current, 'normal');
  // delta_tension 不足 → 不触发
  m.feed({ seq: 502, type: 'relation.changed', payload: { changes: [{ a_id: 'A01', b_id: 'A02', cause: '500', delta_tension: 7 }] } });
  assert.equal(m.current, 'normal');
  // cause 匹配且 ≥8 → 触发
  m.feed({ seq: 503, type: 'relation.changed', payload: { changes: [{ a_id: 'A01', b_id: 'A02', cause: '500', delta_tension: 8 }] } });
  assert.equal(m.current, 'climax');
  assert.deepEqual(changes, [['climax', 'argue_tension']]);
  tick(30_001);
  assert.equal(m.current, 'normal');
});

test('状态机：触发入 climax；30s 静默回落 normal；新触发重置计时', () => {
  const { env, tick } = makeEnv();
  const modes = [];
  const m = new ModeMachine({ env, onChange: (mode) => modes.push(mode) });
  const a = { type: 'dialogue.confess', payload: { result: 'accepted' } };
  m.feed(a);
  assert.equal(m.current, 'climax');
  tick(20_000);
  m.feed(a); // 重置计时
  tick(29_999);
  assert.equal(m.current, 'climax');
  tick(2);
  assert.equal(m.current, 'normal');
  assert.deepEqual(modes, ['climax', 'normal']);
  // 已在 climax 时重复触发不重复 onChange
  m.feed(a);
  m.feed(a);
  assert.deepEqual(modes, ['climax', 'normal', 'climax']);
});
