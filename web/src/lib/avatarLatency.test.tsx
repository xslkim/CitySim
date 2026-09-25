/** T-WEB-11 Avatar/LatencyBar 测试。 */
import { render } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import Avatar, { resolveAvatarColor } from '../components/common/Avatar';
import { lagSeconds, latencyLevel } from '../components/common/LatencyBar';
import { PORTRAITS, portraitUrl } from './portraits';

describe('Avatar（05 T-WEB-11 / 06 T-ART-02）', () => {
  it('有资产 → img；无资产 → 签名色 initials 兜底', () => {
    const { getByTestId } = render(<Avatar id="A01" name="林晚" signatureColor="#C3CDDA" />);
    expect(getByTestId('avatar-img-A01')).toHaveAttribute('src', '/assets/portraits/linwan.png');
    const { getByTestId: g2 } = render(<Avatar id="A09" name="陈默" signatureColor="#209894" />);
    const fallback = g2('avatar-fallback-A09');
    expect(fallback.textContent).toBe('陈');
    expect(fallback.style.background).toBeTruthy();
  });

  it('test_fallback_initials_stable_color：签名色缺失按 id 稳定散列取池内一色', () => {
    const c1 = resolveAvatarColor('A21', null);
    const c2 = resolveAvatarColor('A21', null);
    expect(c1).toBe(c2);
    expect(c1).toMatch(/^#[0-9A-Fa-f]{6}$/);
    expect(resolveAvatarColor('A22', null)).not.toBe(resolveAvatarColor('A21', '#zzz' as never));
  });

  it('PORTRAITS 覆盖 6 人（D11：其余走兜底）', () => {
    expect(Object.keys(PORTRAITS)).toHaveLength(6);
    expect(portraitUrl('A40')).toBeNull();
    expect(portraitUrl('A06')).toBe('/assets/portraits/hanche.png');
  });
});

describe('LatencyBar（03 §0.1/§9.2）', () => {
  it('注入 61s → warn；601s → negative', () => {
    expect(latencyLevel(59)).toBe('ok');
    expect(latencyLevel(61)).toBe('warn');
    expect(latencyLevel(601)).toBe('negative');
  });

  it('滞后换算 = (watermark−latest) × 300s ÷ 压缩比', () => {
    expect(lagSeconds(100, 99, 3)).toBe(100); // 1 tick = 模拟 5min = 300s，压缩比 3 → 100s
    expect(lagSeconds(100, 100, 3)).toBe(0);
  });
});
