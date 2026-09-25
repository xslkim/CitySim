/**
 * 事件文案渲染器（05 T-WEB-12；03 §5.3 映射表唯一实现 + 缺省规则 + 06 §1.3 金额口径）。
 *
 * - `text_display` 优先，缺省模板拼；金额展示由 `amount_cents`（分，正入负出）换算；
 * - 股价组件红涨绿跌（A 股惯例例外区，03 §5.3 N-P1-13；组件自带图例）；
 * - `director.intervene` 紫色描边（director token，02 §7.1）；trigger 左 4px 色条（02 §7.3）；
 * - `e<seq>` 仅渲染层拼前缀，值永远是 seq（06 §2）；
 * - 缺省规则（03 §5.3 N-P1-8）：已注册且带 text_display 的未列 type → 通用社交行；
 *   无 text_display 未知 type → 灰条原始 JSON + 前端错误日志计数；
 * - L3 无位（03 §5.3 表注）；internal 聚合事件（state.needs_delta / relation.changed）非调试态不渲染。
 */
import type { ObsEvent } from '../proto/event';
import { EVENT_TYPES } from '../proto/event_types';

export type RenderKind = 'bubble' | 'banner' | 'card' | 'divider' | 'system' | 'move' | 'gray' | 'hidden';

export interface RenderedEvent {
  kind: RenderKind;
  icon: string;
  text: string;
  /** 股价红涨绿跌（例外区）：'up'|'down'|null */
  stockDir?: 'up' | 'down' | null;
  /** director.intervene 紫色描边（02 §7.1 director token） */
  directorBorder?: boolean;
  grade?: string | null;
}

/** 03 §5.3 显式映射表（23 个显式 type；系统事件行走 SYSTEM_TYPES 前缀族） */
export const EXPLICIT_TYPES = [
  'dialogue.chat', 'dialogue.gossip', 'dialogue.argue', 'dialogue.confess', 'dialogue.apologize',
  'social.send_message', 'social.invite', 'social.refuse', 'social.give_gift', 'social.help',
  'social.borrow_money', 'social.repay_money',
  'agent.move', 'agent.reflection', 'agent.promoted', 'agent.demoted',
  'world.announce', 'economy.payroll', 'economy.stock.tick', 'world.overtime', 'world.layoff_rumor',
  'director.intervene', 'time.day_summary',
] as const;

/** 灰条系统族（03 §5.3 系统行：时间轴默认折叠，调试态展开） */
export const SYSTEM_PREFIXES = ['time.', 'system.'] as const;

/** internal 聚合事件（非调试态不渲染，06 §1.2 表头 / 05 §2.3） */
export const INTERNAL_ONLY_TYPES = ['state.needs_delta', 'relation.changed'] as const;

const unknownTypeLog: { count: number; types: Set<string> } = { count: 0, types: new Set() };

/** 前端错误日志（03 §5.3 N-P1-8 灰条计数；测试断言用） */
export function unknownTypeLogCount(): number {
  return unknownTypeLog.count;
}

function logUnknown(type: string): void {
  unknownTypeLog.count += 1;
  unknownTypeLog.types.add(type);
  console.warn(`[eventText] 未知/无展示文本 type 灰条兜底：${type}`);
}

export function displaySeq(seq: number): string {
  return `e${seq}`; // 渲染层拼前缀；值永远是 seq（06 §2）
}

/** 金额展示：amount_cents（分，正入负出）→ ¥ 字符串（06 §1.3） */
export function formatCents(cents: number): string {
  const yuan = Math.abs(cents) / 100;
  return `${cents < 0 ? '-' : '+'}¥${yuan % 1 === 0 ? yuan : yuan.toFixed(2)}`;
}

type NameOf = (id: string) => string;
const idName: NameOf = (id) => id;

export function renderEvent(ev: ObsEvent, nameOf: NameOf = idName): RenderedEvent {
  const p = ev.payload as Record<string, any>;
  const text = typeof p.text_display === 'string' ? p.text_display : '';
  const grade = (ev.ui?.grade as string) ?? null;
  const base = { grade };

  switch (ev.type) {
    case 'dialogue.chat':
      return { ...base, kind: 'bubble', icon: '💬', text: text || `${nameOf(p.participants?.[0])} 和 ${nameOf(p.participants?.[1])} 聊了聊` };
    case 'dialogue.gossip':
      return { ...base, kind: 'bubble', icon: '🗣', text: `${nameOf(p.teller)} 跟 ${nameOf(p.listener)} 咬耳朵：${text}` };
    case 'dialogue.argue':
      return { ...base, kind: 'bubble', icon: '⚡', text: text || `${nameOf(p.participants?.[0])} 和 ${nameOf(p.participants?.[1])} 吵起来了` };
    case 'dialogue.confess':
      return { ...base, kind: 'bubble', icon: '❤', text: text || '告白' };
    case 'dialogue.apologize':
      return { ...base, kind: 'bubble', icon: '🙇', text: text || '道歉' };
    case 'social.send_message':
      return { ...base, kind: 'card', icon: '💌', text: `${nameOf(p.from)} 给 ${nameOf(p.to)} 捎了句话：${text || p.content_hint || ''}` };
    case 'social.invite':
      return { ...base, kind: 'card', icon: '✉', text: `${nameOf(p.from)} 约 ${nameOf(p.to)} ${p.activity ?? ''}` };
    case 'social.refuse':
      return { ...base, kind: 'card', icon: '✋', text: `${nameOf(p.to)} 拒绝了 ${nameOf(p.from)}` };
    case 'social.give_gift':
      return { ...base, kind: 'card', icon: '🎁', text: `${nameOf(p.from)} 送 ${nameOf(p.to)} 礼物（${p.tier} 档）${typeof p.amount_cents === 'number' ? ` ${formatCents(-p.amount_cents)}` : ''}` };
    case 'social.help':
      return { ...base, kind: 'card', icon: '🤝', text: `${nameOf(p.from)} 帮 ${nameOf(p.to)}：${p.matter ?? ''}` };
    case 'social.borrow_money':
      return { ...base, kind: 'card', icon: '💸', text: `${nameOf(p.from)} 向 ${nameOf(p.to)} 借钱${typeof p.amount_cents === 'number' ? ` ${formatCents(p.amount_cents)}` : ''}` };
    case 'social.repay_money':
      return { ...base, kind: 'card', icon: '💳', text: `${nameOf(p.from)} 还 ${nameOf(p.to)} 钱${typeof p.amount_cents === 'number' ? ` ${formatCents(p.amount_cents)}` : ''}` };
    case 'agent.move':
      return { ...base, kind: 'move', icon: '👣', text: '' }; // 无气泡（pawn 动画）
    case 'agent.reflection':
      return { ...base, kind: 'system', icon: '💭', text }; // 灰虚线气泡（调试态）
    case 'agent.promoted':
      return { ...base, kind: 'card', icon: '★', text: `${nameOf(p.actors?.[0] ?? '')} 成为焦点角色` };
    case 'agent.demoted':
      return { ...base, kind: 'card', icon: '○', text: `${nameOf(p.actors?.[0] ?? '')} 退出焦点` };
    case 'world.announce':
      return { ...base, kind: 'banner', icon: '📢', text: text || `${p.title ?? ''} ${p.body ?? ''}`.trim() };
    case 'economy.payroll':
      return { ...base, kind: 'banner', icon: '💰', text: text || `发工资${typeof p.amount_cents === 'number' ? ` ${formatCents(p.amount_cents)}` : ''}` };
    case 'economy.stock.tick': {
      const r = typeof p.r === 'number' ? p.r : null;
      const dir = r == null ? null : r >= 0 ? 'up' : 'down'; // 红涨绿跌（例外区）
      return { ...base, kind: 'banner', icon: '📈', stockDir: dir,
        text: text || `${p.symbol ?? ''} ${r == null ? '' : `${r >= 0 ? '+' : ''}${(r * 100).toFixed(1)}%`}` };
    }
    case 'world.overtime':
      return { ...base, kind: 'banner', icon: '🌃', text: text || '加班夜' };
    case 'world.layoff_rumor':
      return { ...base, kind: 'banner', icon: '😰', text: text || '裁员传闻' };
    case 'director.intervene':
      return { ...base, kind: 'banner', icon: '🎬', text: `编剧：${text || p.reason || ''}（${p.level ?? ''}）`, directorBorder: true };
    case 'time.day_summary':
      return { ...base, kind: 'divider', icon: '🌙', text: `第 ${p.day ?? '?'} 天结束` };
    default:
      break;
  }

  if (INTERNAL_ONLY_TYPES.includes(ev.type as never)) {
    return { ...base, kind: 'hidden', icon: '', text: '' }; // internal 聚合：非调试态不渲染
  }
  if (SYSTEM_PREFIXES.some((pre) => ev.type.startsWith(pre))) {
    return { ...base, kind: 'system', icon: '⚙', text: text || ev.type }; // 灰色系统条（调试态）
  }
  if ((EVENT_TYPES as readonly string[]).includes(ev.type) && text) {
    // 缺省规则：已注册 + text_display → 通用社交行（卡片横幅 + 域图标）
    return { ...base, kind: 'card', icon: '📌', text };
  }
  logUnknown(ev.type); // 无 text_display 未知 type → 灰条原始 JSON + 错误日志（N-P1-8）
  return { ...base, kind: 'gray', icon: '⚙', text: JSON.stringify(ev.payload) };
}

/** trigger 左 4px 色条（02 §7.3）：autonomous=text-1 / world=accent / director=director / 其余=neutral */
export function triggerColorVar(trigger: string): string {
  switch (trigger) {
    case 'autonomous':
      return 'var(--text-1)';
    case 'world':
      return 'var(--accent)';
    case 'director':
      return 'var(--director)';
    default:
      return 'var(--neutral)';
  }
}
