/**
 * 数据延迟指示条（03 §0.1/§9.2）：watermark_tick − 最新事件 tick 换算滞后时长；
 * >60s 黄（warn）/ >10min 红（negative）。M4 主库直连恒近零属预期（05 文档 D4）。
 * tick = 模拟 5 分钟（00 §4 红线 10）：滞后 tick 数 × 5min / 压缩比 折算真实秒。
 */
import { useTimelineStore } from '../../stores/timelineStore';
import { useWorldStore } from '../../stores/worldStore';

export const TICK_SIM_MINUTES = 5; // tick = 模拟 5 分钟（00 §4 红线 10）

export type LatencyLevel = 'ok' | 'warn' | 'negative';

export function latencyLevel(lagSeconds: number): LatencyLevel {
  if (lagSeconds > 600) return 'negative'; // >10min 红（03 §9.2）
  if (lagSeconds > 60) return 'warn';      // >60s 黄
  return 'ok';
}

/** 滞后秒数 = (watermark_tick − 最新事件 tick) × 300s ÷ 压缩比（模拟秒→真实秒） */
export function lagSeconds(watermarkTick: number, latestTick: number, compressionRatio: number): number {
  const simLag = Math.max(0, watermarkTick - latestTick) * TICK_SIM_MINUTES * 60;
  return simLag / Math.max(compressionRatio, 0.0001);
}

const COLOR: Record<LatencyLevel, string> = {
  ok: 'text-text-1',
  warn: 'text-warn',
  negative: 'text-negative',
};

export default function LatencyBar() {
  const watermarkTick = useWorldStore((s) => s.watermarkTick);
  const latestTick = useTimelineStore((s) => s.latestTick);
  const ratio = useWorldStore((s) => s.snapshot?.compression_ratio ?? 1);
  const lag = lagSeconds(watermarkTick, latestTick, ratio || 1);
  const level = latencyLevel(lag);
  return (
    <span data-testid="latency-bar" className={COLOR[level]}>
      延迟{lag < 1 ? '<1s' : lag < 60 ? `${Math.round(lag)}s` : `${Math.round(lag / 60)}min`}
    </span>
  );
}
