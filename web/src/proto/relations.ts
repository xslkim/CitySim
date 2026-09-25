/** 关系协议（03 §5.1 /api/relations/*；稀疏编码 05 §3.4）。 */
import { z } from 'zod';
import { agentIdSchema, eventSchema } from './event';

export const relationEdgeSchema = z
  .object({
    a: agentIdSchema,
    b: agentIdSchema,
    aff: z.number().int(),
    ten: z.number().int(),
    label: z.array(z.string()),
  })
  .strip();

export const relationSnapshotsSchema = z.object({
  items: z.array(z.object({ sim_day: z.string(), edges: z.array(relationEdgeSchema) })),
});

export const relationPairSchema = z.object({
  a: agentIdSchema,
  b: agentIdSchema,
  series: z.object({
    forward: z.array(z.object({ sim_day: z.string(), affinity: z.number(), tension: z.number() })),
    backward: z.array(z.object({ sim_day: z.string(), affinity: z.number(), tension: z.number() })),
  }),
  changes: z.array(
    z.object({
      event_seq: z.number().int(),
      a: agentIdSchema,
      b: agentIdSchema,
      delta_affinity: z.number().int(),
      delta_tension: z.number().int(),
      labels_added: z.array(z.string()).nullable(),
      labels_removed: z.array(z.string()).nullable(),
      sim_time: z.string(),
    }),
  ),
  key_events: z.array(eventSchema),
});

export type RelationEdge = z.infer<typeof relationEdgeSchema>;
export type RelationSnapshots = z.infer<typeof relationSnapshotsSchema>;
export type RelationPair = z.infer<typeof relationPairSchema>;
