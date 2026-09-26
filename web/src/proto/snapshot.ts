/** 快照协议（03 §5.1 /api/snapshot data 形态逐字；05 §3.6 白名单供数）。 */
import { z } from 'zod';
import { agentIdSchema, dialogueLineSchema } from './event';

export const snapshotAgentSchema = z
  .object({
    id: agentIdSchema,
    name: z.string(),
    lod: z.enum(['star', 'secondary', 'background']).nullable(),
    location_id: z.string().nullable(),
    activity: z.string().nullable(),
    mood: z.number().nullable(),
    needs: z.record(z.number()).nullable(),
  })
  .strip();

export const snapshotSchema = z
  .object({
    tick: z.number().int(),
    sim_time: z.string(),
    sim_day: z.number().int(),
    compression_ratio: z.number(),
    snapshot_time: z.string().nullable().optional(), // R1 #1：快照自身时点（"截至 HH:MM"标注）
    snapshot_kind: z.enum(['rolling', 'day_end']).optional(), // R1 #1：rolling=日内滚动 latest
    llm_status: z // R1 #2：LLM 降级运行态（观众人话提示信号）
      .object({
        degraded: z.boolean(), failover_count: z.number().int(), last_reason: z.string().nullable(),
        stalled: z.boolean().optional(), stalled_reason: z.string().nullable().optional(), // T-ITER2-01④
      })
      .optional(),
    world_stalled: z.boolean().optional(),        // T-ITER2-01④：世界停滞信号（假 healthy 修复）
    world_stalled_reason: z.string().nullable().optional(),
    agents: z.array(snapshotAgentSchema),
    economy: z.object({ stocks: z.array(z.object({ symbol: z.string(), price: z.number() })) }),
    active_dialogues: z.array(
      z
        .object({
          event_seq: z.number().int(),
          participants: z.array(agentIdSchema),
          location_id: z.string().nullable(),
          lines: z.array(dialogueLineSchema),
        })
        .strip(),
    ),
  })
  .strip();

export type SnapshotData = z.infer<typeof snapshotSchema>;
export type SnapshotAgent = z.infer<typeof snapshotAgentSchema>;
