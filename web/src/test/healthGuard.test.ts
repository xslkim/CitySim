/**
 * T-WEB-18 阈值守卫（验收 1）：从 server/config/health_thresholds.yaml（01 T-CFG-07/01 §9 镜像）
 * 动态读全部阈值数值生成 grep 模式，断言 web/src 零命中（守卫自身不写死任何阈值字面量）。
 * 口径：非整数阈值字面量（如 2.4/0.45 形态）才具判别力；整数阈值（1/2/8/12/40）在自然代码中
 * 不可避免同形，按"非整数小数"收窄（01 §9 阈值表中比率/熵值均为小数）。
 * 豁免：① 注释文本（扫描前剥除）；② `lib/rippleLayout.ts` 的失真三档 0.15/0.40——持有方是
 * 05 §3.7（ripple_edge.distortion），与 01 §9 健康阈值同形不同源，非本守卫对象（05 文档偏差表登记）。
 */
import { readFileSync } from 'node:fs';
import { describe, expect, it } from 'vitest';
import { readdirSync } from 'node:fs';
import { join } from 'node:path';

const EXEMPT_FILES = new Set([
  join('src', 'lib', 'rippleLayout.ts'), // 失真三档档值持有方 = 05 §3.7（见文件头豁免②）
  join('src', 'test', 'ripple.test.ts'), // 同上：失真分档测试样本值（05 §3.7 域）
  join('src', 'lite', 'narrativeMap.ts'), // 同上：lite 失真人话分档（05 §3.7 域，不显示数字）
  join('src', 'test', 'lite.test.ts'), // 同上：失真人话测试样本值
]);

function stripComments(text: string): string {
  return text.replace(/\/\*[\s\S]*?\*\//g, '').replace(/\/\/[^\n]*/g, '');
}

function collectSources(dir: string, out: string[] = []): string[] {
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const p = join(dir, entry.name);
    if (entry.isDirectory()) collectSources(p, out);
    else if (/\.(ts|tsx)$/.test(entry.name) && !entry.name.endsWith('healthGuard.test.ts')
      && !EXEMPT_FILES.has(p)) out.push(p);
  }
  return out;
}

function thresholdLiterals(): string[] {
  const yaml = readFileSync('../server/config/health_thresholds.yaml', 'utf-8');
  // 只取 healthy/warning/alarm 段的数值字面量中的非整数小数（判别力口径见文件头）
  const nums = new Set<string>();
  for (const m of yaml.matchAll(/\d+\.\d+/g)) {
    nums.add(m[0]);
  }
  return [...nums];
}

describe('T-WEB-18 阈值守卫（03 §3.6 前端零阈值硬编码）', () => {
  it('health_thresholds.yaml 全部小数阈值在 web/src 零命中', () => {
    const lits = thresholdLiterals();
    expect(lits.length).toBeGreaterThan(5); // 守卫自身有效性：确实读到了阈值
    const hits: string[] = [];
    for (const file of collectSources('src')) {
      const text = stripComments(readFileSync(file, 'utf-8'));
      for (const lit of lits) {
        if (text.includes(lit)) hits.push(`${file}: ${lit}`);
      }
    }
    expect(hits).toEqual([]);
  });
});
