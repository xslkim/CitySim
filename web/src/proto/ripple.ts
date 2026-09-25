/** 涟漪协议（03 §3.4 五段 + §5.1 /api/ripple/*；失真三档阈值持有方 = 05 §3.7）。 */
import { z } from 'zod';
import { agentIdSchema, eventSchema } from './event';

export const rippleProjectionSchema = z
  .object({
    agent_id: agentIdSchema,
    memory_id: z.number().int(),
    importance: z.number().int(),
    is_witness: z.boolean(),
    content_display: z.string(),
    sim_time: z.string(),
  })
  .strip();

export const rippleChainEdgeSchema = z
  .object({
    dst_event_seq: z.number().int(),
    src_event_seq: z.number().int(),
    teller_id: agentIdSchema,
    listener_id: agentIdSchema,
    hop: z.number().int().min(1),
    distortion: z.number().min(0).max(1).nullable(),
    sim_time: z.string(),
  })
  .strip();

export const relationChangeSchema = z
  .object({
    a_id: agentIdSchema,
    b_id: agentIdSchema,
    delta_affinity: z.number().int(),
    delta_tension: z.number().int(),
    labels_added: z.array(z.string()).nullable(),
    labels_removed: z.array(z.string()).nullable(),
    event_seq: z.number().int(),
    sim_time: z.string(),
  })
  .strip();

export const rippleStatsSchema = z.object({
  covered_agents: z.number().int(),
  hops: z.number().int(),
  max_distortion: z.number(),
  followup_count: z.number().int(),
});

export const rippleDataSchema = z
  .object({
    source_seq: z.number().int(),
    source: eventSchema.optional(), // 源事件本体（03 §3.4 源事件卡）
    projections: z.array(rippleProjectionSchema),
    chain: z.array(rippleChainEdgeSchema),
    followups: z.array(eventSchema),
    relation_changes: z.array(relationChangeSchema),
    stats: rippleStatsSchema,
  })
  .strip();

export const rippleTodaySchema = z
  .object({
    sim_day: z.string().nullable(),
    items: z.array(z.object({ score: z.number(), stats: rippleStatsSchema, event: eventSchema })),
  })
  .strip();

export type RippleData = z.infer<typeof rippleDataSchema>;
export type RippleToday = z.infer<typeof rippleTodaySchema>;
