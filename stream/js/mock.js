/** 本地录制脚本驱动（截图/验收用，不触 WS）：?mock=<scenario>。
 * 事件形态与 obs-api 出站一致（serde 序列化形态：seq/tick/sim_time/type/payload/ui）。
 * 同一组 dispatch/handler 链路，等价于 WS 到达（T-LTV-07 验收 2 允许注入等价事件）。
 */

import { dispatchEvent } from './dispatch.js';

const T0 = '2026-10-23T21:30:00+08:00';
let seq = 90000;

function ev(type, payload, ui = null, trigger = 'autonomous') {
  seq += 1;
  return {
    seq, tick: 12340 + seq, sim_time: T0, type, source: 'agent:A01', trigger,
    arc_id: null, ui, payload,
  };
}

function lines(speakers, texts, step = 3) {
  return texts.map((text_display, i) => ({
    speaker: speakers[i % speakers.length], text_display, at_offset_s: i * step,
  }));
}

const SCENARIOS = {
  // normal 单人位特写：林晚（有写实特写资产）
  chat: [ev('dialogue.chat', {
    location_id: 'apt.kitchen', participants: ['A01', 'A02'], mode: 'small', topic_ids: [], witnesses: [],
    lines: lines(['A01', 'A02'], [
      '汤快好了，你要不要先尝尝咸淡？',
      '好啊，反正今晚也不用加班。',
      '你别光喝，说说上次那个需求怎么样了。',
      '评审过了，明天就能上线。',
    ]),
    text_display: '林晚和周叙在厨房闲聊',
  })],
  // normal 单人位兜底：赵启（无特写资产 → 签名色 + 姓名首字）
  fallback: [ev('dialogue.chat', {
    location_id: 'apt.kitchen', participants: ['A07', 'A02'], mode: 'small', topic_ids: [], witnesses: [],
    lines: lines(['A07', 'A02'], [
      '叙哥，你那台旧显示器还在吗？',
      '在阳台吃灰呢，你要就搬走。',
      '谢了！改天请你喝奶茶。',
    ]),
    text_display: '赵启和周叙在厨房闲聊',
  })],
  // climax 双人栈：告白成功（dialogue.confess result=accepted，02 §7.6 触发②）
  confess: [ev('dialogue.confess', {
    location_id: 'apt.roof', participants: ['A02', 'A03'], result: 'accepted', witnesses: [],
    lines: lines(['A02', 'A03'], [
      '这句话，我憋了四十天。',
      '……你先别说话，让我缓一缓。',
      '苏蔓，我喜欢你。',
      '我也喜欢你。其实……我等这句话很久了。',
    ]),
    text_display: '天台告白',
  }, { grade: 'A' })],
  // climax：A 级事件触发①（ui.grade='A'）
  gradeA: [ev('world.perf_review', {
    location_id: 'corp.tech', agent_id: 'A01', manager_id: 'A21', grade: 'S', delta_salary: 300000,
  }, { grade: 'A' }, 'world')],
};

export function runMock(ctx) {
  const name = ctx.params.get('mock');
  const script = SCENARIOS[name] || SCENARIOS.chat;
  console.log(`[stream] mock scenario: ${name || 'chat'}（${script.length} 事件）`);
  script.forEach((event, i) => {
    setTimeout(() => dispatchEvent(ctx, event), i * 500);
  });
}
