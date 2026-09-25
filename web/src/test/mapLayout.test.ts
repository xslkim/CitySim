/** T-WEB-13 地图页逻辑测试（布局/归属/offsite 角标/scrub 语义）。 */
import { readFileSync } from 'node:fs';
import { describe, expect, it } from 'vitest';
import {
  injectLayout,
  locationName,
  nodeRect,
  offsiteCount,
  siteOf,
} from '../lib/mapLayout';

const layout = JSON.parse(readFileSync('public/map_layout.json', 'utf-8'));
injectLayout(layout);

describe('map_layout.json（03 §3.1 结构）', () => {
  it('四 site + 公寓 6 层×4 房 + 公共区 + 校外聚合区块', () => {
    expect(layout.sites.map((s: { id: string }) => s.id)).toEqual(['apt', 'corp', 'ext', 'offsite']);
    const apt = layout.sites[0];
    expect(apt.rooms).toHaveLength(24);
    expect(apt.commons.length).toBeGreaterThanOrEqual(5);
    const offsite = layout.sites[3];
    expect(offsite.type).toBe('aggregate');
    expect(offsite.zones[0].occupancy_badge).toBe(true);
  });

  it('公寓房间逻辑坐标 80×64（×4 = 320×256 像素，03 §3.1 铁律）', () => {
    const r = nodeRect(layout.sites[0], 'apt.L3.302');
    expect(r).toBeTruthy();
    expect(r!.w * 4).toBe(320);
    expect(r!.h * 4).toBe(256);
  });

  it('siteOf/locationName（home.* → offsite；未映射降级原文，06 §7 B8）', () => {
    expect(siteOf('apt.L3.302')).toBe('apt');
    expect(siteOf('corp.pantry')).toBe('corp');
    expect(siteOf('home.A17')).toBe('offsite');
    expect(locationName('corp.pantry')).toBe('茶水间');
    expect(locationName('home.A17')).toBe('校外住处');
    expect(locationName('nowhere')).toBe('nowhere');
  });

  it('offsiteCount 角标 = home.* 节点人数（N-P1-9）', () => {
    expect(offsiteCount({ A01: 'home.A01', A02: 'apt.L2.201', A03: 'home.A03' })).toBe(2);
  });
});
