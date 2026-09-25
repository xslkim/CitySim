/**
 * 事件协议 zod schema（03 §5.1 单条事件形态；06 §1.1/§1.2/§2 值形态仲裁）。
 * 前端零信任（03 §7.2）：未知键剥除（.strip()）+ 命中 text_raw/未登记内部键时告警计数。
 * proto 层只定义 *_display 字段；text_raw 永不定义（00 §4 红线 7）。
 */
import { z } from 'zod';
import { EVENT_TYPES } from './event_types';

export const AGENT_ID_RE = /^A(0[1-9]|[1-3][0-9]|40)$/; // 04 §5.2 CHECK 同形
export const BARE_SEQ_RE = /^\d+$/;                      // 裸 seq 数字字符串（06 §2）

export const agentIdSchema = z.string().regex(AGENT_ID_RE);
export const bareSeqSchema = z.string().regex(BARE_SEQ_RE);

export const triggerSchema = z.enum([
  'autonomous', 'world', 'director', 'gift', 'vote', 'system', // 06 §1.1 六枚举（含 system，评审 P2-17）
]);
export const sourceSchema = z.union([
  z.literal('world'),
  z.literal('director'),
  z.literal('system'),
  z.string().regex(/^agent:A(0[1-9]|[1-3][0-9]|40)$/), // 'agent:<id>'（04 §5.2）
]);
export const eventTypeSchema = z.enum(EVENT_TYPES);
export const gradeSchema = z.enum(['A', 'B', 'C']);

/** 对话逐句行（06 §2：at_offset_s = float ≥0，客户端倍率唯一节拍权威） */
export const dialogueLineSchema = z
  .object({
    speaker: agentIdSchema,
    text_display: z.string(),
    at_offset_s: z.number().min(0),
  })
  .strip();

/**
 * payload JSONB：结构键宽松透传 + 已登记引用键值形态强校验（06 §2）：
 * caused_by/for_event_ref/target_seq/request_ref = 裸 seq 数字字符串；cites[] = 裸 seq 串列表；
 * lines[].at_offset_s = float ≥0；amount_cents = 整数分（06 §1.3）。
 */
export const payloadSchema = z
  .record(z.unknown())
  .superRefine((p, ctx) => {
    for (const k of ['caused_by', 'for_event_ref', 'target_seq', 'request_ref'] as const) {
      const v = p[k];
      if (v !== undefined && (typeof v !== 'string' || !BARE_SEQ_RE.test(v))) {
        ctx.addIssue({ code: z.ZodIssueCode.custom, message: `${k} 须为裸 seq 数字字符串（06 §2）` });
      }
    }
    const cites = p.cites;
    if (cites !== undefined) {
      if (!Array.isArray(cites) || cites.some((c) => typeof c !== 'string' || !BARE_SEQ_RE.test(c))) {
        ctx.addIssue({ code: z.ZodIssueCode.custom, message: 'cites[] 元素须为裸 seq 数字字符串（06 §2）' });
      }
    }
    if (p.amount_cents !== undefined && !Number.isInteger(p.amount_cents)) {
      ctx.addIssue({ code: z.ZodIssueCode.custom, message: 'amount_cents 须为整数（分，06 §1.3）' });
    }
    if (p.lines !== undefined) {
      const r = z.array(dialogueLineSchema).safeParse(p.lines);
      if (!r.success) {
        ctx.addIssue({ code: z.ZodIssueCode.custom, message: `lines[] 形态非法：${r.error.issues[0]?.message}` });
      }
    }
  });

export const eventSchema = z
  .object({
    seq: z.number().int(),                    // 唯一标识（DB 主键）；UI 引用形态 e<seq> 仅渲染层拼前缀
    tick: z.number().int(),
    sim_time: z.string(),                     // ISO-8601 模拟时间
    type: eventTypeSchema,
    source: sourceSchema,
    trigger: triggerSchema,
    arc_id: z.string().nullable(),
    ui: z.record(z.unknown()).nullable(),     // ui.grade = 初值；最新生效 grade 读 event_grade_view（05 §3.8）
    payload: payloadSchema,
  })
  .strip();

export type ObsEvent = z.infer<typeof eventSchema>;
export type Trigger = z.infer<typeof triggerSchema>;

/** 内部通道键（永不定义/永不出站，00 §4 红线 7；命中即告警计数，03 §7.2 前端零信任） */
export const FORBIDDEN_KEYS = ['text_raw', 'embedding', 'prompt', 'content'] as const;

export interface ParseResult<T> {
  data: T;
  warnings: string[];
}

function collectWarnings(raw: unknown, path = ''): string[] {
  const warnings: string[] = [];
  if (raw && typeof raw === 'object') {
    for (const [k, v] of Object.entries(raw as Record<string, unknown>)) {
      const p = path ? `${path}.${k}` : k;
      if ((FORBIDDEN_KEYS as readonly string[]).includes(k)) {
        warnings.push(`forbidden key stripped: ${p}`);
      } else {
        warnings.push(...collectWarnings(v, p));
      }
    }
  }
  return warnings;
}

/** 递归剥除禁用键（zod strip 只管顶层；payload 内的 text_raw 在 schema 外剥除，03 §7.2） */
function stripForbidden(raw: unknown): unknown {
  if (Array.isArray(raw)) return raw.map(stripForbidden);
  if (raw && typeof raw === 'object') {
    return Object.fromEntries(
      Object.entries(raw as Record<string, unknown>)
        .filter(([k]) => !(FORBIDDEN_KEYS as readonly string[]).includes(k))
        .map(([k, v]) => [k, stripForbidden(v)]),
    );
  }
  return raw;
}

/** 前端零信任解析：剥除禁用键 + 告警计数 → zod 校验（03 §7.2）。 */
export function parseEvent(raw: unknown): ParseResult<ObsEvent> {
  const warnings = collectWarnings(raw);
  const cleaned = stripForbidden(raw);
  const r = eventSchema.safeParse(cleaned);
  if (!r.success) {
    throw new Error(`event schema 校验失败：${r.error.issues[0]?.path.join('.')} ${r.error.issues[0]?.message}`);
  }
  return { data: r.data, warnings };
}
