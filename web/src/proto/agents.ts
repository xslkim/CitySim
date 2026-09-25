/** 角色接口协议（03 §5.1 /api/agents 系列；persona_display 六键边界 05 §3.6 P2-6）。 */
import { z } from 'zod';
import { agentIdSchema, eventSchema } from './event';

export const personaDisplaySchema = z
  .object({
    big_five: z.record(z.number()).nullable().optional(),
    backstory: z.array(z.unknown()).nullable().optional(),
    appearance: z.record(z.unknown()).nullable().optional(),
    signature_quirk: z.string().nullable().optional(),
    contrast_public: z.string().nullable().optional(),
    speech_style_public: z.string().nullable().optional(),
    // secret / trigger_point 永不出站（05 §3.6）：不定义，strip 剥除
  })
  .strip();

export const agentSummarySchema = z
  .object({
    id: agentIdSchema,
    name: z.string(),
    gender: z.string().nullable(),
    age: z.number().nullable(),
    room_no: z.string().nullable(),
    department: z.string().nullable(),
    job_title: z.string().nullable(),
    lod: z.enum(['star', 'secondary', 'background']).nullable(),
    signature_color: z.string().nullable(),
    appearance: z.record(z.unknown()).nullable(),
  })
  .strip();

export const agentListSchema = z.object({ items: z.array(agentSummarySchema) });

export const goalSchema = z.object({
  goal: z.string(),
  blocked_count: z.number().int(),
  frustration: z.number(),
});

export const agentDetailSchema = z
  .object({
    id: agentIdSchema,
    name: z.string(),
    gender: z.string().nullable(),
    age: z.number().nullable(),
    room_no: z.string().nullable(),
    department: z.string().nullable(),
    job_title: z.string().nullable(),
    lod: z.enum(['star', 'secondary', 'background']).nullable(),
    persona_display: personaDisplaySchema,
    routine: z.record(z.unknown()),
    goals: z.array(goalSchema),
  })
  .strip();

export const agentStateSchema = z.object({
  id: agentIdSchema,
  lod: z.enum(['star', 'secondary', 'background']).nullable(),
  needs: z.record(z.number()).nullable(),
  mood: z.number().nullable(),
  goals: z.array(goalSchema),
  frustration: z.number(),
});

export const agentScheduleSchema = z.object({
  id: agentIdSchema,
  day: z.string(),
  routine: z.record(z.unknown()),
  events: z.array(eventSchema),
});

export const reflectionSchema = z.object({
  memory_id: z.number().int(),
  sim_time: z.string(),
  content_display: z.string(), // 仅展示通道（05 §3.2）；content 原文永不定义
  importance: z.number().int(),
});
export const reflectionsSchema = z.object({ items: z.array(reflectionSchema) });

export type AgentSummary = z.infer<typeof agentSummarySchema>;
export type AgentDetail = z.infer<typeof agentDetailSchema>;
export type AgentState = z.infer<typeof agentStateSchema>;
export type Reflection = z.infer<typeof reflectionSchema>;
