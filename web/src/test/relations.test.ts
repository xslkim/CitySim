/** T-WEB-17 关系页测试（02 §7.5 边映射 / ?pair= 解析）。 */
import { describe, expect, it } from 'vitest';
import { edgeColor, edgeInnerCore, edgeWidth, parsePair } from '../lib/relationsLayout';

describe('T-WEB-17', () => {
  it('test_edge_width_mapping：|aff| → 1~5px 线性（02 §7.5）', () => {
    expect(edgeWidth(0)).toBe(1);
    expect(edgeWidth(100)).toBe(5);
    expect(edgeWidth(-50)).toBe(3);
    expect(edgeWidth(250)).toBe(5); // clamp
  });

  it('边色：aff>0 positive / <0 negative；tension>50 叠加 warn 内芯', () => {
    expect(edgeColor({ a: 'A01', b: 'A02', aff: 10, ten: 0, label: [] })).toBe('#2EFA8D');
    expect(edgeColor({ a: 'A01', b: 'A02', aff: -10, ten: 0, label: [] })).toBe('#F75752');
    expect(edgeInnerCore({ a: 'A01', b: 'A02', aff: 0, ten: 60, label: [] })).toBe('#FEAE3E');
    expect(edgeInnerCore({ a: 'A01', b: 'A02', aff: 0, ten: 50, label: [] })).toBeNull();
  });

  it('test_pair_query_parsing：?pair=A01,A02 解析；非法/同人对拒', () => {
    expect(parsePair('A01,A02')).toEqual(['A01', 'A02']);
    expect(parsePair('A01,A41')).toBeNull();
    expect(parsePair('ag07,A02')).toBeNull();
    expect(parsePair(null)).toBeNull();
  });
});
