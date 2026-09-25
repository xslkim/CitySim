/** WS 帧协议（03 §5.2 全帧形态逐字：hello/welcome/subscribe/event/state_diff/health/
 * ping/pong/resync_required）。 */
import { z } from 'zod';
import { agentIdSchema, eventSchema, eventTypeSchema, gradeSchema, triggerSchema } from './event';
import { healthDataSchema } from './health';

// —— 客户端 → 服务端 ——
export const helloFrameSchema = z.object({
  op: z.literal('hello'),
  token: z.string(),
  client: z.string(),
  last_seq: z.number().int().optional(), // 断线 resume（03 §5.2）
});

export const subscribeFrameSchema = z.object({
  op: z.literal('subscribe'),
  channels: z.array(
    z.union([
      z.object({
        name: z.literal('events'),
        filter: z.object({
          types: z.array(eventTypeSchema),     // 空=全量
          actors: z.array(agentIdSchema),
          locations: z.array(z.string()),
          grades: z.array(gradeSchema),        // 最新生效 grade（event_grade_view 等价物）
        }),
      }),
      z.object({ name: z.literal('world_state') }),
      z.object({ name: z.literal('health') }),
    ]),
  ),
});

export const pingFrameSchema = z.object({ op: z.literal('ping'), t: z.number().optional() });

// —— 服务端 → 客户端 ——
export const welcomeFrameSchema = z.object({
  op: z.literal('welcome'),
  watermark_tick: z.number().int(),
  server_time: z.string(),
  session_id: z.string(),
});

export const eventFrameSchema = z.object({
  op: z.literal('event'),
  seq: z.number().int(),
  data: eventSchema,
});

export const stateDiffSchema = z.object({
  tick: z.number().int(),
  agents: z.array(
    z
      .object({
        id: agentIdSchema,
        location_id: z.string().optional(),
        mood: z.number().optional(),
        needs: z.record(z.number()).optional(),
      })
      .strip(),
  ),
});

export const stateDiffFrameSchema = z.object({
  op: z.literal('state_diff'),
  seq: z.number().int(),
  data: stateDiffSchema,
});

export const healthFrameSchema = z.object({
  op: z.literal('health'),
  seq: z.number().int(),
  data: healthDataSchema,
});

export const pongFrameSchema = z.object({ op: z.literal('pong'), t: z.number().optional() });
export const resyncFrameSchema = z.object({ op: z.literal('resync_required') });

export const serverFrameSchema = z.discriminatedUnion('op', [
  welcomeFrameSchema,
  eventFrameSchema,
  stateDiffFrameSchema,
  healthFrameSchema,
  pongFrameSchema,
  resyncFrameSchema,
]);

export type ServerFrame = z.infer<typeof serverFrameSchema>;
export type StateDiff = z.infer<typeof stateDiffSchema>;

// triggerSchema re-export 供过滤 UI（03 §3.2 枚举含 system）
export { triggerSchema };
