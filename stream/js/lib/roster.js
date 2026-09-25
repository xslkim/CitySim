/** T-ART-02 直播页 agent 目录：签名色解析、特写 URL 解析、缺资产兜底描述（纯逻辑，不触 DOM）。
 *
 * 权威链（00 §1 A17）：姓名/签名色 = obs-api `/api/agents`（上游 config/agents.yaml）；
 * 特写文件映射 = `/assets/portraits/manifest.json`（gen_portrait_manifest.py 构建产物，禁止手改）。
 * 签名色两种入站形态：`"N-色名"`（01 §2.2 原型口径）与 `"#RRGGBB"`（agents.yaml {id,name,hex}
 * 经 obs-api 下发形态）；非法值兜底 `neutral` token 并 console 告警（06 T-ART-02）。
 */

import { SEMANTIC_COLORS, SIGNATURE_COLORS } from './palette-data.js';

const NEUTRAL = SEMANTIC_COLORS.neutral; // 兜底 token（02 §7.1）

const SIG_ID_NAME_RE = /^(\d{1,2})-(\S+)$/;
const HEX_RE = /^#[0-9A-Fa-f]{6}$/;

/** 解析签名色 → HEX。支持 'N-色名' 与 '#RRGGBB'；非法格式抛错。 */
export function parseSignatureColor(input) {
  if (typeof input !== 'string' || !input) throw new Error(`非法 signature_color：${input}`);
  const m = SIG_ID_NAME_RE.exec(input);
  if (m) {
    const entry = SIGNATURE_COLORS[String(Number(m[1]))];
    if (!entry) throw new Error(`签名色编号越界：${input}`);
    if (entry.name !== m[2]) throw new Error(`签名色编号与色名不符：${input}（#${m[1]}=${entry.name}）`);
    return entry.hex;
  }
  if (HEX_RE.test(input)) return input.toUpperCase();
  throw new Error(`非法 signature_color：${input}`);
}

/** 解析失败兜底 neutral token 并告警（页面侧不抛错）。 */
export function resolveColor(input, warn = console.warn) {
  try {
    return parseSignatureColor(input);
  } catch (e) {
    warn(`[roster] ${e.message}，兜底 neutral`);
    return NEUTRAL;
  }
}

/** 深色底上签名色提亮 10%（02 §7.1 口径）。 */
export function lighten(hex, ratio = 0.10) {
  const n = parseInt(hex.slice(1), 16);
  const ch = (v) => Math.round(v + (255 - v) * ratio);
  const r = ch((n >> 16) & 255), g = ch((n >> 8) & 255), b = ch(n & 255);
  return `#${((r << 16) | (g << 8) | b).toString(16).padStart(6, '0').toUpperCase()}`;
}

export class Roster {
  /** @param agents /api/agents 的 data.items（[{id,name,signature_color,...}]）
   *  @param manifest manifest.json（{agent_id:{file}}，构建产物） */
  constructor({ agents = [], manifest = {}, warn = console.warn } = {}) {
    this._warn = warn;
    this._agents = new Map();
    for (const a of agents) {
      if (!a || typeof a.id !== 'string') continue;
      this._agents.set(a.id, {
        id: a.id,
        name: typeof a.name === 'string' && a.name ? a.name : a.id,
        color: resolveColor(a.signature_color, warn),
      });
    }
    this._files = new Map();
    for (const [id, entry] of Object.entries(manifest || {})) {
      if (entry && typeof entry.file === 'string') this._files.set(id, entry.file);
    }
  }

  agent(id) {
    return this._agents.get(id) || null;
  }

  nameOf(id) {
    return this.agent(id)?.name ?? id;
  }

  /** 签名色（提亮 10%，深色底口径）；未知 agent 兜底 neutral。 */
  colorOf(id) {
    const a = this.agent(id);
    return lighten(a ? a.color : NEUTRAL);
  }

  /** 特写 URL（T-ART-03 挂载形态 /assets/portraits/<file>）；无 manifest 条目 → null。 */
  portraitUrl(id) {
    const file = this._files.get(id);
    return file ? `/assets/portraits/${file}` : null;
  }

  /** 渲染描述：有特写 → img；无特写 → 签名色圆底 + 姓名首字（ui/web-lite .av 口径）。 */
  describe(id) {
    const a = this.agent(id) || { id, name: id, color: NEUTRAL };
    const url = this.portraitUrl(id);
    const color = this.colorOf(id);
    if (url) return { kind: 'portrait', url, name: a.name, color };
    return { kind: 'initial', color, char: a.name.slice(0, 1), name: a.name };
  }
}

/** 启动加载：一次 /api/agents（摘要含签名色，05 T-WEB-03）+ manifest.json。 */
export async function loadRoster({ base = '', token = '', fetchImpl = fetch, warn = console.warn } = {}) {
  const q = token ? `?token=${encodeURIComponent(token)}` : '';
  const [agentsResp, manifestResp] = await Promise.all([
    fetchImpl(`${base}/api/agents${q}`),
    fetchImpl(`${base}/assets/portraits/manifest.json`),
  ]);
  if (!agentsResp.ok) throw new Error(`GET /api/agents → ${agentsResp.status}`);
  const body = await agentsResp.json();
  const manifest = manifestResp.ok ? await manifestResp.json() : {};
  if (!manifestResp.ok) warn(`[roster] manifest.json → ${manifestResp.status}，全部走兜底头像`);
  return new Roster({ agents: (body?.data?.items) || [], manifest, warn });
}
