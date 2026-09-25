/** T-WEB-12 eventText 测试（03 §5.3 全表 + 缺省规则 + 06 §1.3 金额 + 例外区）。 */
import { render } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { describe, expect, it } from 'vitest';
import EventStream, { OVERSCAN } from '../components/common/EventStream';
import type { ObsEvent } from '../proto/event';
import { EVENT_TYPES } from '../proto/event_types';
import {
  EXPLICIT_TYPES,
  displaySeq,
  formatCents,
  renderEvent,
  triggerColorVar,
  unknownTypeLogCount,
} from './eventText';

function ev(type: string, payload: Record<string, unknown> = {}, trigger = 'autonomous'): ObsEvent {
  return {
    seq: 1, tick: 1, sim_time: '2026-10-12T19:38:11+08:00', type: type as never,
    source: 'agent:A01', trigger: trigger as never, arc_id: null, ui: null, payload,
  };
}

describe('03 §5.3 映射表', () => {
  it('06 §1.2 全类型遍历有渲染路径无 throw', () => {
    for (const t of EVENT_TYPES) {
      const r = renderEvent(ev(t, { text_display: '展示文本', day: 3, participants: ['A01', 'A02'] }));
      expect(r.kind).toBeTruthy();
    }
  });

  it('显式类型清单与 03 §5.3 表行数一致（23 显式 type）', () => {
    expect(EXPLICIT_TYPES).toHaveLength(23);
    // 显式清单 ⊆ 06 注册表
    for (const t of EXPLICIT_TYPES) {
      expect(EVENT_TYPES).toContain(t);
    }
  });

  it('test_unknown_type_gray_bar_logged：无 text_display 未知 type → 灰条 + 错误日志计数', () => {
    const before = unknownTypeLogCount();
    // proto 层已拒未注册 type；此处直调渲染器模拟协议被篡改注入（03 §7.2 兜底路径）
    const r = renderEvent(ev('ghost.unregistered' as never, { zzz: 1 }));
    expect(r.kind).toBe('gray');
    expect(unknownTypeLogCount()).toBe(before + 1);
  });

  it('未过审占位（03 §7.3）：已注册无 text_display → ▮内容审核中▮，事件可见', () => {
    const r = renderEvent(ev('economy.settle', { agent_id: 'A01', amount_cents: -100, reason: 'x' }));
    expect(r.kind).toBe('gray');
    expect(r.text).toBe('▮内容审核中▮');
    expect(r.text).not.toContain('amount_cents'); // 无原文泄漏面
  });

  it('已注册 + text_display 的未列 type → 通用社交行（不落灰条）', () => {
    const r = renderEvent(ev('social.help', { from: 'A01', to: 'A02', matter: '搬家' }));
    expect(r.kind).toBe('card');
    expect(r.text).toContain('搬家');
  });

  it('test_stock_red_up_green_down：股价红涨绿跌（例外区）', () => {
    const up = renderEvent(ev('economy.stock.tick', { symbol: '星澜科技', r: 0.021 }, 'world'));
    const down = renderEvent(ev('economy.stock.tick', { symbol: '星澜科技', r: -0.031 }, 'world'));
    expect(up.stockDir).toBe('up');
    expect(down.stockDir).toBe('down');
    expect(up.text).toContain('+2.1%');
    expect(down.text).toContain('-3.1%');
  });

  it('test_amount_cents_display：分→¥ 正入负出（06 §1.3）', () => {
    expect(formatCents(123456)).toBe('+¥1234.56');
    expect(formatCents(-5000)).toBe('-¥50');
    const r = renderEvent(ev('social.borrow_money', { from: 'A01', to: 'A02', amount_cents: 200000 }));
    expect(r.text).toContain('+¥2000');
  });

  it('director.intervene 紫色描边 + L 级文案；trigger 色条映射（02 §7.3）', () => {
    const r = renderEvent(ev('director.intervene', { level: 'L1', reason: '排弧线' }, 'director'));
    expect(r.directorBorder).toBe(true);
    expect(r.text).toContain('L1');
    expect(triggerColorVar('director')).toBe('var(--director)');
    expect(triggerColorVar('world')).toBe('var(--accent)');
    expect(triggerColorVar('autonomous')).toBe('var(--text-1)');
  });

  it('e<seq> 仅渲染层拼前缀', () => {
    expect(displaySeq(1102)).toBe('e1102');
  });

  it('internal 聚合事件非调试态不渲染', () => {
    expect(renderEvent(ev('state.needs_delta', { changes: [] }, 'system')).kind).toBe('hidden');
  });
});

describe('EventStream 虚拟列表（03 §6.3）', () => {
  it('灌 5,000 条 fixture：DOM 行数 ≤ 窗口上限', () => {
    const events: ObsEvent[] = Array.from({ length: 5000 }, (_, i) => ({
      seq: i + 1, tick: i + 1, sim_time: '2026-10-12T19:38:11+08:00',
      type: 'dialogue.chat', source: 'agent:A01', trigger: 'autonomous', arc_id: null,
      ui: null,
      payload: { participants: ['A01', 'A02'], lines: [], witnesses: [], text_display: `第${i + 1}段` },
    }));
    const { container } = render(
      <MemoryRouter>
        <EventStream events={events} />
      </MemoryRouter>,
    );
    const rows = container.querySelectorAll('[data-seq]');
    expect(rows.length).toBeGreaterThan(0);
    expect(rows.length).toBeLessThanOrEqual(2 * OVERSCAN + 20); // ±50 窗口 + 余量
    expect(rows.length).toBeLessThan(5000);
  });
});
