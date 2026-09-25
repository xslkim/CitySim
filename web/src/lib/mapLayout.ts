/** 地图布局（03 §3.1：`map_layout.json` 静态随前端发版 public/；offsite 聚合区块 N-P1-9）。
 *  运行时 fetch('/map_layout.json') 加载（public 资源不参与打包 import）；vitest 经 injectLayout 注入。 */

export interface RoomNode {
  id: string;
  name: string;
  floor?: number | string;
  col?: number;
  w?: number;
  h?: number;
}
export interface Site {
  id: string;
  name: string;
  type?: string;
  floors?: number;
  roomsPerFloor?: number;
  rooms?: RoomNode[];
  commons?: RoomNode[];
  zones?: (RoomNode & { occupancy_badge?: boolean; matches_prefix?: string })[];
}

let LAYOUT: { sites: Site[] } | null = null;

export async function loadMapLayout(): Promise<{ sites: Site[] }> {
  if (LAYOUT) return LAYOUT;
  const res = await fetch('/map_layout.json');
  LAYOUT = (await res.json()) as { sites: Site[] };
  return LAYOUT;
}

/** 容错变体（jsdom/离线不抛未捕获拒绝；布局仍不可用返回 null） */
export async function tryLoadMapLayout(): Promise<{ sites: Site[] } | null> {
  try {
    return await loadMapLayout();
  } catch {
    return null;
  }
}

/** 测试/直播页注入（同名同文件同源，T-ART-03 第三挂载口径） */
export function injectLayout(layout: { sites: Site[] }): void {
  LAYOUT = layout;
}

export function getLayout(): { sites: Site[] } | null {
  return LAYOUT;
}

/** location_id → 中文名（取不到映射降级显示 location_id 原文，06 §7 B8 同口径） */
export function locationName(locationId: string | null | undefined): string {
  if (!locationId) return '—';
  for (const site of LAYOUT?.sites ?? []) {
    for (const n of [...(site.rooms ?? []), ...(site.commons ?? []), ...(site.zones ?? [])]) {
      if (n.id === locationId) return n.name;
    }
    if (site.type === 'aggregate') {
      const z = site.zones?.[0];
      if (z?.matches_prefix && locationId.startsWith(z.matches_prefix)) return z.name;
    }
  }
  return locationId;
}

/** location_id → site id（pawn 归属 tab；offsite = home.* 前缀聚合） */
export function siteOf(locationId: string | null | undefined): string | null {
  if (!locationId) return null;
  if (locationId.startsWith('apt.')) return 'apt';
  if (locationId.startsWith('corp.')) return 'corp';
  if (locationId.startsWith('ext.')) return 'ext';
  if (locationId.startsWith('home.')) return 'offsite';
  return null;
}

/** 节点几何（逻辑坐标，03 §3.1；×4 换算为门禁③预留，本期纯 SVG） */
export function nodeRect(site: Site, nodeId: string): { x: number; y: number; w: number; h: number } | null {
  if (site.id === 'apt') {
    const room = site.rooms?.find((r) => r.id === nodeId);
    if (room) {
      return {
        x: (room.col! - 1) * (room.w! + 8),
        y: (site.floors! - Number(room.floor)) * (room.h! + 8),
        w: room.w!,
        h: room.h!,
      };
    }
    const commonsOrder = ['apt.lobby', 'apt.kitchen', 'apt.gym', 'apt.laundry', 'apt.roof'];
    const idx = commonsOrder.indexOf(nodeId);
    if (idx >= 0) {
      return { x: idx * 88, y: 6 * (64 + 8), w: 80, h: 40 }; // 公共区底带
    }
    return null;
  }
  const zones = site.zones ?? [];
  const idx = zones.findIndex((z) => z.id === nodeId);
  if (idx < 0) return null;
  const cols = 4;
  return { x: (idx % cols) * 96, y: Math.floor(idx / cols) * 72, w: 88, h: 64 };
}

/** offsite 角标人数 = 位于 home.* 节点的 NPC 数（03 §3.1 N-P1-9；驻留时段由内核 residence 驱动） */
export function offsiteCount(positions: Record<string, string | null>): number {
  return Object.values(positions).filter((p) => p?.startsWith('home.')).length;
}
