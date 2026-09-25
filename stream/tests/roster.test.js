/** T-ART-02 roster.js 单测（node --test；02 §4.3 签名色形态、兜底头像口径）。 */

import test from 'node:test';
import assert from 'node:assert/strict';

import {
  Roster, loadRoster, lighten, parseSignatureColor, resolveColor,
} from '../js/lib/roster.js';

const AGENTS = [
  { id: 'A01', name: '林晚', signature_color: '#C3CDDA' },
  { id: 'A05', name: '叶蓁', signature_color: '9-杏黄' },
  { id: 'A07', name: '赵启', signature_color: '#A8B82E' },
];
const MANIFEST = { A01: { file: 'linwan.png' }, A05: { file: 'yezhen.png' } };

test("'14-雾蓝'（N-色名形态）→ #C3CDDA", () => {
  assert.equal(parseSignatureColor('14-雾蓝'), '#C3CDDA');
});

test("'9-杏黄' → #FADB40；编号与色名不符抛错", () => {
  assert.equal(parseSignatureColor('9-杏黄'), '#FADB40');
  assert.throws(() => parseSignatureColor('9-雾蓝'));
});

test("'#RRGGBB' 形态规范化大写；非法格式抛错", () => {
  assert.equal(parseSignatureColor('#c3cdda'), '#C3CDDA');
  assert.throws(() => parseSignatureColor('雾蓝'));
  assert.throws(() => parseSignatureColor('#12345'));
  assert.throws(() => parseSignatureColor(''));
  assert.throws(() => parseSignatureColor(null));
});

test('resolveColor 非法值兜底 neutral 并告警', () => {
  const warnings = [];
  const hex = resolveColor('bogus', (m) => warnings.push(m));
  assert.equal(hex, '#8A93A0');
  assert.equal(warnings.length, 1);
});

test('lighten 提亮 10%（02 §7.1 深色底口径）', () => {
  assert.equal(lighten('#000000'), '#1A1A1A');
  assert.equal(lighten('#C3CDDA'), '#C9D2DE');
});

test('describe：有 manifest 条目 → portrait；无条目 → 签名色 + 姓名首字兜底', () => {
  const r = new Roster({ agents: AGENTS, manifest: MANIFEST, warn: () => {} });
  const p = r.describe('A01');
  assert.equal(p.kind, 'portrait');
  assert.equal(p.url, '/assets/portraits/linwan.png');
  assert.equal(p.name, '林晚');
  const f = r.describe('A07');
  assert.equal(f.kind, 'initial');
  assert.equal(f.char, '赵');
  assert.equal(f.color, lighten('#A8B82E'));
  assert.equal(f.name, '赵启');
});

test('未知 agent：姓名兜底 id 原文，色兜底 neutral', () => {
  const r = new Roster({ agents: AGENTS, manifest: MANIFEST, warn: () => {} });
  const f = r.describe('A40');
  assert.equal(f.kind, 'initial');
  assert.equal(f.name, 'A40');
  assert.equal(f.char, 'A');
  assert.equal(f.color, lighten('#8A93A0'));
});

test('loadRoster：/api/agents 摘要 + manifest 两路拉取', async () => {
  const calls = [];
  const fetchImpl = async (url) => {
    calls.push(url);
    if (url.startsWith('/api/agents')) {
      return { ok: true, json: async () => ({ ok: true, data: { items: AGENTS } }) };
    }
    return { ok: true, json: async () => MANIFEST };
  };
  const r = await loadRoster({ token: 'dev_t', fetchImpl, warn: () => {} });
  assert.ok(calls[0].includes('token=dev_t'));
  assert.equal(r.portraitUrl('A05'), '/assets/portraits/yezhen.png');
  assert.equal(r.colorOf('A01'), lighten('#C3CDDA'));
});

test('loadRoster：manifest 缺失（404）→ 全员兜底头像，不抛错', async () => {
  const fetchImpl = async (url) => (url.startsWith('/api/agents')
    ? { ok: true, json: async () => ({ ok: true, data: { items: AGENTS } }) }
    : { ok: false, status: 404 });
  const r = await loadRoster({ fetchImpl, warn: () => {} });
  assert.equal(r.describe('A01').kind, 'initial');
});
