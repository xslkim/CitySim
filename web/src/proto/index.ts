/** proto 总出口 + REST 响应包（03 §5.1 `{ok,data,meta.watermark_tick}` / 错误包）。 */
import { z } from 'zod';
import { eventSchema } from './event';

export * from './event';
export * from './snapshot';
export * from './agents';
export * from './ws';
export * from './health';
export * from './ripple';
export * from './relations';

export const envelopeMetaSchema = z
  .object({ watermark_tick: z.number().int() })
  .passthrough();

export function envelopeSchema<T extends z.ZodTypeAny>(data: T) {
  return z.object({
    ok: z.literal(true),
    data,
    meta: envelopeMetaSchema,
  });
}

export const errorEnvelopeSchema = z.object({
  ok: z.literal(false),
  error: z.object({ code: z.string(), message: z.string() }),
});

/** /api/events data（03 §5.1：items + next_cursor=末条 seq） */
export const envelopeEventsSchema = z.object({
  items: z.array(eventSchema),
  next_cursor: z.number().int().nullable(),
});

/** /api/events/histogram data（03 §5.1） */
export const eventsHistogramSchema = z.array(
  z.object({ bucket_start: z.string(), count: z.number().int(), a_count: z.number().int() }),
);

/** /api/usage（03 §5.1 门禁④度量） */
export const usageRowSchema = z.object({
  token: z.string(),
  day: z.string(),
  opens: z.number().int(),
  first_seen_at: z.string(),
  last_seen_at: z.string(),
});
export const usageDataSchema = z.array(usageRowSchema);
