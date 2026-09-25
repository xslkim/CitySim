/** 健康度协议（03 §5.1 /api/health；阈值/阈值色随响应下发，前端零硬编码，03 §3.6）。 */
import { z } from 'zod';

export const thresholdBandSchema = z
  .object({
    min: z.number().optional(),
    max: z.number().optional(),
  })
  .passthrough(); // healthy/warning/alarm 段形态随 01 §9（含 low/high 双段、计数型 min_total 等）

export const healthMetricSchema = z
  .object({
    key: z.string(),
    column: z.string().nullable(),
    value: z.number().nullable(),
    unit: z.string().nullable(),
    window: z.string().nullable(),
    thresholds: z.object({
      healthy: thresholdBandSchema.nullable(),
      warning: thresholdBandSchema.nullable(),
      alarm: thresholdBandSchema.nullable(),
    }),
    color: z.enum(['green', 'yellow', 'red']).nullable(), // 前端映射 green→positive/yellow→warn/red→negative（02 §7.1）
    advice: z.string().nullable(),
  })
  .strip();

export const healthDataSchema = z
  .object({
    sim_day: z.string().nullable(),
    metrics: z.array(healthMetricSchema),
    today_partial: z.object({
      events_today: z.number().int(),
      a_grade_today: z.number().int(),
      intervention_rate_today: z.number(),
    }),
    runtime: z.object({
      intervention_rate: z.number().nullable(),
      intervention_rate_cap: z.number().nullable(), // 数值持有方 = 源方案 §4.8（API 下发）
      cost_micro_cny: z.number().nullable(),
      cost_limit_micro_cny: z.number().nullable(),
      cost_breaker: z.object({
        alarm_ratio: z.number().nullable(),
        throttle_ratio: z.number().nullable(),
      }),
      replica_lag_s: z.number(),
      replica_lag_ticks: z.number(),
    }),
  })
  .strip();

export type HealthData = z.infer<typeof healthDataSchema>;
export type HealthMetric = z.infer<typeof healthMetricSchema>;
