/**
 * 叙事化文案映射层（05 T-WEB-19；03 §1.1：事件类型 → 人话模板；数值降级为语言）。
 * 红线：输出不得出现事件类型名/trigger/tick/LOD/seq/压缩比/grade/autonomous 等工程词汇
 * （09 §5 E4 grep 走查）；数据复用同一 obs-api，不产生新数据需求。
 * 视觉/文案风格参照 ui/web-lite/ 原型（叙事卡片、人话标注）。
 */
import type { ObsEvent } from '../proto/event';

export interface Narrative {
  icon: string;
  tag: string;        // 卡片角标（人话）
  text: string;       // 人话叙事
  participants: string[]; // 涉及的人（头像条）
}

type NameOf = (id: string) => string;
const idName: NameOf = (id) => id;

function people(...ids: (string | undefined)[]): string[] {
  return ids.filter((x): x is string => !!x);
}

/** 事件 → 人话卡片（text_display 优先，缺省人话模板拼） */
export function narrateEvent(ev: ObsEvent, nameOf: NameOf = idName): Narrative {
  const p = ev.payload as Record<string, any>;
  const t = typeof p.text_display === 'string' ? p.text_display : '';
  switch (ev.type) {
    case 'dialogue.chat':
      return { icon: '💬', tag: '闲聊', participants: people(...(p.participants ?? [])),
        text: t || `${nameOf(p.participants?.[0])} 和 ${nameOf(p.participants?.[1])} 聊了一会儿。` };
    case 'dialogue.gossip':
      return { icon: '🗣', tag: '八卦', participants: people(p.teller, p.listener),
        text: t || `${nameOf(p.teller)} 跟 ${nameOf(p.listener)} 说了点悄悄话。` };
    case 'dialogue.argue':
      return { icon: '⚡', tag: '冲突', participants: people(...(p.participants ?? [])),
        text: t || `${nameOf(p.participants?.[0])} 和 ${nameOf(p.participants?.[1])} 吵了起来。` };
    case 'dialogue.confess':
      return { icon: '❤', tag: p.result === 'accepted' ? '名场面' : '心事', participants: people(...(p.participants ?? [])),
        text: t || (p.result === 'accepted' ? '有人表明了心意——成了。' : '有人表明了心意。') };
    case 'dialogue.apologize':
      return { icon: '🙇', tag: '和解', participants: people(...(p.participants ?? [])),
        text: t || '有人低头道了歉。' };
    case 'social.send_message':
      return { icon: '💌', tag: '捎话', participants: people(p.from, p.to),
        text: t || `${nameOf(p.from)} 给 ${nameOf(p.to)} 捎了句话。` };
    case 'social.invite':
      return { icon: '✉', tag: '邀约', participants: people(p.from, p.to),
        text: `${nameOf(p.from)} 约 ${nameOf(p.to)} ${p.activity ?? '出门'}。` };
    case 'social.refuse':
      return { icon: '✋', tag: '拒绝', participants: people(p.from, p.to),
        text: `${nameOf(p.to)} 没答应 ${nameOf(p.from)}。` };
    case 'social.give_gift':
      return { icon: '🎁', tag: '心意', participants: people(p.from, p.to),
        text: `${nameOf(p.from)} 给 ${nameOf(p.to)} 送了礼物。` };
    case 'social.help':
      return { icon: '🤝', tag: '帮忙', participants: people(p.from, p.to),
        text: `${nameOf(p.from)} 帮了 ${nameOf(p.to)} 一把${p.matter ? `：${p.matter}` : ''}。` };
    case 'social.borrow_money':
      return { icon: '💸', tag: '借钱', participants: people(p.from, p.to),
        text: `${nameOf(p.from)} 找 ${nameOf(p.to)} 借了钱${typeof p.amount_cents === 'number' ? `（¥${Math.abs(p.amount_cents) / 100}）` : ''}。` };
    case 'social.repay_money':
      return { icon: '💳', tag: '还钱', participants: people(p.from, p.to),
        text: `${nameOf(p.from)} 把钱还给了 ${nameOf(p.to)}。` };
    case 'agent.move':
      return { icon: '👣', tag: '动向', participants: people(...(p.actors ?? [])),
        text: '有人换了地方。' };
    case 'agent.reflection':
      return { icon: '💭', tag: '心事', participants: people(...(p.actors ?? [])),
        text: t || '有人想了想心事。' };
    case 'agent.promoted':
      return { icon: '★', tag: '焦点', participants: people(...(p.actors ?? [])),
        text: `${nameOf(p.actors?.[0])} 最近成了大家关注的焦点。` };
    case 'agent.demoted':
      return { icon: '○', tag: '平静', participants: people(...(p.actors ?? [])),
        text: `${nameOf(p.actors?.[0])} 的日子回归平静。` };
    case 'world.announce':
      return { icon: '📢', tag: '公告', participants: [], text: t || `${p.title ?? ''} ${p.body ?? ''}`.trim() || '楼里贴了张新公告。' };
    case 'world.overtime':
      return { icon: '🌃', tag: '加班', participants: people(...(p.participants ?? [])),
        text: t || '今晚有人要加班。' };
    case 'world.layoff_rumor':
      return { icon: '😰', tag: '传闻', participants: [], text: t || '公司里传起了裁员的消息。' };
    case 'economy.payroll':
      return { icon: '💰', tag: '发薪', participants: [], text: t || '发工资的日子到了。' };
    case 'economy.stock.tick':
      return { icon: '📈', tag: '股市', participants: [],
        text: t || `${p.symbol ?? '股票'} ${typeof p.r === 'number' ? (p.r >= 0 ? '涨了' : '跌了') : '波动'}。` };
    case 'director.intervene':
      return { icon: '🎬', tag: '转机', participants: [], text: t || '世界悄悄起了变化。' };
    case 'director.grade_revise':
      return { icon: '🎬', tag: '转机', participants: [], text: '一段插曲被重新掂量了分量。' };
    case 'time.day_summary':
      return { icon: '🌙', tag: '落幕', participants: [], text: '这一天结束了。' };
    default:
      if (t) return { icon: '📌', tag: '动态', participants: people(...(p.actors ?? [])), text: t };
      return { icon: '·', tag: '日常', participants: [], text: '平静的一刻。' };
  }
}

/** 需求六维 → 心情语句（03 §1.1 数值降级为语言示例口径） */
export function moodSentence(needs: Record<string, number> | null, mood: number | null): string {
  const m = mood ?? needs?.mood;
  const base = m == null ? '心情成谜'
    : m >= 70 ? '心情不错'
    : m >= 50 ? '心情还算平稳'
    : m >= 30 ? '有点低落'
    : '明显不太好';
  if (!needs) return `${base}。`;
  const lows: string[] = [];
  if ((needs.wealth ?? 100) < 30) lows.push('手头有点紧');
  if ((needs.hunger ?? 100) < 30) lows.push('肚子饿了');
  if ((needs.social ?? 100) < 30) lows.push('有点孤单');
  if ((needs.energy ?? 100) < 30) lows.push('很疲惫');
  if ((needs.achievement ?? 100) < 30) lows.push('有点泄气');
  if (!lows.length) return `${base}。`;
  if (m != null && m >= 50) return `${base}，但${lows[0]}。`;
  return `${base}，而且${lows.join('、')}。`;
}

/** 关系 → 人话（label + affinity/tension 降级为语言） */
export function relationPhrase(label: string[], affinity: number, tension: number): { tag: string; phrase: string } {
  if (label.length) {
    return { tag: label[0], phrase: label.join('、') };
  }
  if (affinity >= 40) return { tag: '亲近', phrase: tension > 50 ? '亲近，但最近有点火药味' : '很亲近' };
  if (affinity >= 10) return { tag: '亲近', phrase: '处得不错' };
  if (affinity <= -20 || tension > 60) return { tag: '对头', phrase: '见面有点别扭' };
  if (affinity < 0) return { tag: '疏远', phrase: '有点疏远' };
  return { tag: '相识', phrase: '点头之交' };
}

/** 失真度 → 人话（档值持有方 05 §3.7 三档语义，lite 不显示数字） */
export function distortionPhrase(d: number | null): string {
  if (d == null || d <= 0.15) return '和原话差不多';
  if (d <= 0.4) return '有点走样';
  return '面目全非';
}
