/** T-WEB-13 地图页逻辑测试（布局/归属/offsite 角标/scrub 语义）。 */
import { readFileSync } from 'node:fs';
import { describe, expect, it } from 'vitest';
import {
  injectLayout,
  locationName,
  nodeRect,
  offsiteCount,
  siteOf,
  spreadOffsets,
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

describe('T-ITER2-06 首屏可视（公共区顶带 + 散开算法）', () => {
  it('公共区带在顶部（y=0，首屏常驻），房间整体下移 48 不重叠', () => {
    const apt = layout.sites[0];
    const kitchen = nodeRect(apt, 'apt.kitchen')!;
    expect(kitchen.y).toBe(0);
    expect(kitchen.x).toBe(88);
    const topFloor = nodeRect(apt, 'apt.L6.601')!;  // 最高层（floor=6）
    expect(topFloor.y).toBe(48);                     // 48 + (6-6)*72
    const ground = nodeRect(apt, 'apt.L1.101')!;
    expect(ground.y).toBe(48 + 5 * 72);
    // 公共区带与最高层房间不重叠
    expect(kitchen.y + kitchen.h).toBeLessThanOrEqual(topFloor.y);
  });

  it('spreadOffsets：同房散开相邻间距 ≥ 头像直径 18px（n=2~6）', () => {
    for (const n of [2, 3, 4, 5, 6]) {
      const offs = spreadOffsets(n);
      expect(offs).toHaveLength(n);
      let min = Infinity;
      for (let i = 0; i < n; i++) {
        for (let j = i + 1; j < n; j++) {
          const d = Math.hypot(offs[i].dx - offs[j].dx, (offs[i].dy - offs[j].dy) / 0.8);
          min = Math.min(min, d);
        }
      }
      expect(min).toBeGreaterThanOrEqual(18);
    }
    expect(spreadOffsets(1)).toEqual([{ dx: 0, dy: 0 }]);
  });

  it('天台（apt.roof）在 448 宽 viewBox 内不被右缘裁切', () => {
    const apt = layout.sites[0];
    const roof = nodeRect(apt, 'apt.roof')!;
    expect(roof.x + roof.w).toBeLessThanOrEqual(440);  // LiteMap/SiteSvg viewBox 宽 448（-4 起）
  });
});
