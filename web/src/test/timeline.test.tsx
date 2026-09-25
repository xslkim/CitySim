/** T-WEB-14 时间轴测试：focus 解析 / grade_revise 徽标 / 过滤参数下推。 */
import { render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { describe, expect, it } from 'vitest';
import TimelineList, { parseFocus } from '../components/timeline/TimelineList';
import { buildEventParams } from '../components/timeline/FilterBar';
import { EMPTY_FILTER } from '../stores/timelineStore';
import type { ObsEvent } from '../proto/event';

function ev(over: Partial<ObsEvent>): ObsEvent {
  return {
    seq: 1, tick: 1, sim_time: '2026-10-12T19:38:11+08:00', type: 'dialogue.chat',
    source: 'agent:A01', trigger: 'autonomous', arc_id: null, ui: null,
    payload: { participants: ['A01', 'A02'], lines: [], witnesses: [], text_display: 'x' },
    ...over,
  } as ObsEvent;
}

describe('T-WEB-14', () => {
  it('test_focus_param_parsing：e1102→1102，非法 → null', () => {
    expect(parseFocus('e1102')).toBe(1102);
    expect(parseFocus('1102')).toBeNull(); // e<seq> 形态必填（06 §2 引用形态）
    expect(parseFocus('eabc')).toBeNull();
    expect(parseFocus(null)).toBeNull();
  });

  it('test_grade_revise_badge_render：改判行渲染标记 + target_seq 链接', () => {
    const revise = ev({
      seq: 200, type: 'director.grade_revise', trigger: 'director', source: 'director',
      payload: { target_seq: '150', new_grade: 'A', reason: '终审上调' },
    });
    render(
      <MemoryRouter>
        <TimelineList events={[revise]} names={new Map()} />
      </MemoryRouter>,
    );
    expect(screen.getByText('改判')).toBeInTheDocument();
  });

  it('六维过滤全部下推 REST 参数（03 §3.2 前端不另滤）', () => {
    const qs = buildEventParams({
      ...EMPTY_FILTER,
      types: ['dialogue.chat', 'dialogue.gossip'],
      actors: ['A01'],
      locations: ['corp.pantry'],
      triggers: ['system'],
      gradeAOnly: true,
      q: '八卦',
    });
    const p = new URLSearchParams(qs);
    expect(p.get('type')).toBe('dialogue.chat,dialogue.gossip');
    expect(p.get('actor')).toBe('A01');
    expect(p.get('location')).toBe('corp.pantry');
    expect(p.get('trigger')).toBe('system');
    expect(p.get('grade')).toBe('A');
    expect(p.get('q')).toBe('八卦');
  });
});
