/**
 * T-WEB-09 proto 层测试（03 §5 schema 逐字 + 06 §2 值形态 + 03 §7.2 前端零信任）。
 * 勾稽 checklist（验收 2）：03 §5.1（event/snapshot/envelope/usage/agents 系列）与 §5.2
 * （hello/welcome/subscribe/event/state_diff/health/ping/pong/resync_required）在 proto/ 均有导出，
 * 由下方 import 静态引用兜底（缺导出即编译错）。
 */
import { describe, expect, it } from 'vitest';
import {
  agentIdSchema,
  envelopeSchema,
  errorEnvelopeSchema,
  eventSchema,
  healthDataSchema,
  helloFrameSchema,
  parseEvent,
  payloadSchema,
  pingFrameSchema,
  pongFrameSchema,
  reflectionSchema,
  relationPairSchema,
  relationSnapshotsSchema,
  resyncFrameSchema,
  rippleDataSchema,
  rippleTodaySchema,
  snapshotSchema,
  stateDiffFrameSchema,
  subscribeFrameSchema,
  triggerSchema,
  usageDataSchema,
  welcomeFrameSchema,
} from './index';
import eventChat from './fixtures/event_chat.json';
import snapshotFx from './fixtures/snapshot.json';
import wsWelcome from './fixtures/ws_welcome.json';
import wsStateDiff from './fixtures/ws_state_diff.json';

function stripMeta(fx: Record<string, unknown>) {
  const { _source, ...rest } = fx;
  return rest;
}

describe('fixtures 过 zod（03 §5.1/§5.2 示例原样抽取 + 键集合互差为空）', () => {
  it('event fixture：键集合与 schema 输出一致', () => {
    const raw = stripMeta(eventChat);
    const parsed = eventSchema.parse(raw);
    expect(new Set(Object.keys(parsed))).toEqual(new Set(Object.keys(raw)));
  });
  it('snapshot fixture', () => {
    const raw = stripMeta(snapshotFx);
    const parsed = snapshotSchema.parse(raw);
    expect(new Set(Object.keys(parsed))).toEqual(new Set(Object.keys(raw)));
  });
  it('ws frames fixture', () => {
    expect(welcomeFrameSchema.parse(stripMeta(wsWelcome)).op).toBe('welcome');
    expect(stateDiffFrameSchema.parse(stripMeta(wsStateDiff)).data.agents[0].id).toBe('A07');
  });
});

describe('06 §2/§1.1 值形态', () => {
  it('test_trigger_includes_system', () => {
    for (const t of ['autonomous', 'world', 'director', 'gift', 'vote', 'system']) {
      expect(triggerSchema.parse(t)).toBe(t);
    }
    expect(() => triggerSchema.parse('admin')).toThrow();
  });
  it('test_agent_id_regex：A01/A40 过，ag07/A41 拒', () => {
    expect(agentIdSchema.parse('A01')).toBe('A01');
    expect(agentIdSchema.parse('A40')).toBe('A40');
    expect(() => agentIdSchema.parse('ag07')).toThrow();
    expect(() => agentIdSchema.parse('A41')).toThrow();
  });
  it('test_caused_by_bare_seq_string："1089" 过、"e1089" 拒', () => {
    expect(() => payloadSchema.parse({ caused_by: '1089' })).not.toThrow();
    expect(() => payloadSchema.parse({ caused_by: 'e1089' })).toThrow();
    expect(() => payloadSchema.parse({ cites: ['8001', '8002'] })).not.toThrow();
    expect(() => payloadSchema.parse({ cites: ['e8001'] })).toThrow();
    expect(() => payloadSchema.parse({ lines: [{ speaker: 'A01', text_display: 'x', at_offset_s: -1 }] })).toThrow();
    expect(() => payloadSchema.parse({ amount_cents: 12.5 })).toThrow();
  });
});

describe('03 §7.2 前端零信任', () => {
  it('test_text_raw_stripped_with_warning', () => {
    const raw = {
      ...stripMeta(eventChat),
      payload: { ...eventChat.payload, text_raw: 'RAW_SECRET', internal_eval: 0.9 },
    };
    const r = parseEvent(raw);
    expect(r.warnings.some((w) => w.includes('text_raw'))).toBe(true);
    expect(JSON.stringify(r.data)).not.toContain('RAW_SECRET');
  });
  it('未知顶层键剥除（strip）', () => {
    const r = parseEvent({ ...stripMeta(eventChat), zzz_unknown: 1 });
    expect('zzz_unknown' in r.data).toBe(false);
  });
});

describe('envelope 与其余 schema 导出勾稽', () => {
  it('envelope/error/usage/health/ripple/relations 导出可用', () => {
    const env = envelopeSchema(snapshotSchema).parse({ ok: true, data: stripMeta(snapshotFx), meta: { watermark_tick: 1 } });
    expect(env.meta.watermark_tick).toBe(1);
    expect(errorEnvelopeSchema.parse({ ok: false, error: { code: 'x', message: 'y' } }).error.code).toBe('x');
    expect(healthDataSchema).toBeTruthy();
    expect(rippleDataSchema).toBeTruthy();
    expect(rippleTodaySchema).toBeTruthy();
    expect(relationSnapshotsSchema).toBeTruthy();
    expect(relationPairSchema).toBeTruthy();
    expect(usageDataSchema).toBeTruthy();
    expect(reflectionSchema).toBeTruthy();
    expect(helloFrameSchema.parse({ op: 'hello', token: 't', client: 'obs/1.0.0', last_seq: 5 }).last_seq).toBe(5);
    expect(subscribeFrameSchema).toBeTruthy();
    expect(pingFrameSchema).toBeTruthy();
    expect(pongFrameSchema).toBeTruthy();
    expect(resyncFrameSchema.parse({ op: 'resync_required' }).op).toBe('resync_required');
  });
});
