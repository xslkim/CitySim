/**
 * T-WEB-19 lite 观众版测试（03 §1.1 呈现层减法 + 09 §5 E4 零工程词汇）。
 * lint 口径：叙事层全部输出（全事件类型 × 文案函数）grep 工程词汇零命中。
 */
import { describe, expect, it } from 'vitest';
import type { ObsEvent } from '../proto/event';
import { EVENT_TYPES } from '../proto/event_types';
import { distortionPhrase, moodSentence, narrateEvent, relationPhrase } from '../lite/narrativeMap';

const FORBIDDEN = /\b(tick|trigger|grade|autonomous|seq|LOD)\b|压缩比|事件类型/;

function ev(type: string, payload: Record<string, unknown> = {}): ObsEvent {
  return {
    seq: 1, tick: 1, sim_time: '2026-10-12T19:38:11+08:00', type: type as never,
    source: 'agent:A01', trigger: 'autonomous', arc_id: null, ui: { grade: 'A' },
    payload: { text_display: '展示文本示例', ...payload },
  } as ObsEvent;
}

describe('narrativeMap（05 T-WEB-19 叙事化文案映射层）', () => {
  it('06 全类型叙事输出零工程词汇（09 §5 E4 grep 口径）', () => {
    for (const t of EVENT_TYPES) {
      const n = narrateEvent(ev(t, {
        participants: ['A01', 'A02'], teller: 'A01', listener: 'A02', from: 'A01', to: 'A02',
        actors: ['A01'], activity: '吃饭', amount_cents: 150000, r: -0.03, symbol: '星澜科技',
        title: '公告', body: '今晚停水', result: 'accepted', day: 3, level: 'L1',
      }), (id) => ({ A01: '林晚', A02: '周叙' })[id as 'A01'] ?? id);
      const text = `${n.icon}${n.tag}${n.text}`;
      expect(FORBIDDEN.test(text), `${t} → ${text}`).toBe(false);
      expect(n.text.length).toBeGreaterThan(0);
    }
  });

  it('需求六维 → 心情语句（数值降级为语言，03 §1.1）', () => {
    expect(moodSentence({ hunger: 80 }, 75)).toBe('心情不错。');
    expect(moodSentence({ hunger: 80, wealth: 20 }, 60)).toBe('心情还算平稳，但手头有点紧。');
    expect(moodSentence({ hunger: 20, energy: 10 }, 25)).toContain('明显不太好');
    expect(moodSentence(null, null)).toBe('心情成谜。');
    for (const s of [moodSentence({ hunger: 20 }, 40)]) {
      expect(FORBIDDEN.test(s)).toBe(false);
    }
  });

  it('关系人话：label 优先，数值降级', () => {
    expect(relationPhrase(['暗恋'], 35, 10).tag).toBe('暗恋');
    expect(relationPhrase([], 50, 10).phrase).toContain('亲近');
    expect(relationPhrase([], -30, 70).tag).toBe('对头');
    expect(relationPhrase([], 0, 0).phrase).toBe('点头之交');
  });

  it('失真人话：不显示数字（档值持有方 05 §3.7）', () => {
    expect(distortionPhrase(0.1)).toBe('和原话差不多');
    expect(distortionPhrase(0.3)).toBe('有点走样');
    expect(distortionPhrase(0.6)).toBe('面目全非');
  });
});
