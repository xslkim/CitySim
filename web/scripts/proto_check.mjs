#!/usr/bin/env node
/**
 * 协议对拍脚本（05 T-WEB-09；09 §8 smoke step④ 消费：curl 响应管道 → 本脚本）。
 *
 * 用法：curl -s http://127.0.0.1:8080/api/snapshot?token=... | pnpm proto:check
 *   - stdin 读 JSON（REST 响应包 `{ok,data,meta}` 或 WS 帧 `{op:...}`）；
 *   - schema 选择：优先 `meta.kind`（obs-api 随包下发），否则按 data/op 形态启发式；
 *   - 前端零信任口径（03 §7.2）：禁用键（text_raw/embedding/prompt/content）剥除并告警计数（stderr），
 *     zod 校验失败 → 非零退出；
 *   - proto/ 为 TS 源码：本脚本先用 tsc（tsconfig.proto.json）编译到 node_modules/.cache/proto-check
 *     再以 CJS 加载（单源，无第二份 JS 镜像）。
 */
import { execFileSync } from 'node:child_process';
import { createRequire } from 'node:module';
import { readFileSync } from 'node:fs';

// 1) 编译 proto/（幂等增量；失败即非零退出）
execFileSync('node_modules/.bin/tsc', ['-p', 'tsconfig.proto.json'], { stdio: 'inherit' });

const require = createRequire(import.meta.url);
const proto = require('../node_modules/.cache/proto-check/index.js');

const raw = readFileSync(0, 'utf-8');
let body;
try {
  body = JSON.parse(raw);
} catch (e) {
  console.error(`FAIL: stdin 非合法 JSON：${e.message}`);
  process.exit(1);
}

const KIND_SCHEMAS = {
  snapshot: proto.snapshotSchema,
  events: proto.envelopeEventsSchema,
  health: proto.healthDataSchema,
  ripple: proto.rippleDataSchema,
  ripple_today: proto.rippleTodaySchema,
  relations_snapshots: proto.relationSnapshotsSchema,
  relations_pair: proto.relationPairSchema,
  agents: proto.agentListSchema,
  usage: proto.usageDataSchema,
};

function guessKind(data) {
  if (data == null) return null;
  if (Array.isArray(data)) return Array.isArray(data[0]?.opens !== undefined ? data : null) && data[0] && 'opens' in data[0] ? 'usage' : null;
  if (data.agents && data.active_dialogues) return 'snapshot';
  if (data.metrics && data.runtime) return 'health';
  if (data.projections && data.chain) return 'ripple';
  if (data.items && data.items[0]?.edges) return 'relations_snapshots';
  if (data.series && data.key_events) return 'relations_pair';
  if (data.items && data.items[0]?.seq !== undefined) return 'events';
  if (data.items && data.items[0]?.lod !== undefined) return 'agents';
  if (data.items && data.items[0]?.score !== undefined) return 'ripple_today';
  return null;
}

let warnings = 0;
function countForbidden(v) {
  if (Array.isArray(v)) v.forEach(countForbidden);
  else if (v && typeof v === 'object') {
    for (const [k, x] of Object.entries(v)) {
      if (proto.FORBIDDEN_KEYS.includes(k)) {
        warnings += 1;
        console.error(`WARN: 禁用键命中（剥除，03 §7.2）: ${k}`);
      } else countForbidden(x);
    }
  }
}

try {
  if (body && typeof body === 'object' && 'op' in body) {
    // WS 帧（03 §5.2）
    const frame = proto.serverFrameSchema.parse(body);
    if (frame.op === 'event') {
      const r = proto.parseEvent(body.data);
      warnings += r.warnings.length;
    }
    console.log(`OK: ws frame op=${frame.op}`);
  } else if (body && body.ok === true) {
    const kind = body.meta?.kind ?? guessKind(body.data);
    const schema = KIND_SCHEMAS[kind];
    if (!schema) {
      console.error(`FAIL: 无法判定 schema（meta.kind=${body.meta?.kind ?? '缺'}，heuristic miss）`);
      process.exit(1);
    }
    countForbidden(body.data);
    if (kind === 'events') {
      // events 列表逐条走 parseEvent（剥除+告警同口径）
      for (const item of body.data.items ?? []) {
        const r = proto.parseEvent(item);
        warnings += r.warnings.length;
      }
    }
    schema.parse(body.data);
    console.log(`OK: rest kind=${kind} warnings=${warnings}`);
  } else if (body && body.ok === false) {
    proto.errorEnvelopeSchema.parse(body);
    console.log(`OK: error envelope code=${body.error.code}`);
  } else {
    console.error('FAIL: 既非 WS 帧也非响应包');
    process.exit(1);
  }
  if (warnings > 0) console.error(`WARN: 共 ${warnings} 个禁用键被剥除`);
  process.exit(0);
} catch (e) {
  console.error(`FAIL: schema 校验失败：${e.message}`);
  process.exit(1);
}
