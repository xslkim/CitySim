/**
 * 快照时点指示条（03 §0.1/§9.2 + R1 #1）：展示快照自身时点"世界状态截至 HH:MM"；
 * 颜色仍按事件滞后（watermark_tick − 最新事件 tick 换算）分级——>60s 黄 / >10min 红。
 * R1 #1：快照滚动刷新间隔 > 数秒，不再自称"live / 延迟<1s"（验收 ④）。
 * tick = 模拟 5 分钟（00 §4 红线 10）：滞后 tick 数 × 5min / 压缩比 折算真实秒。
 * T-ITER2-05：时刻格式化走 lib/simTime（+08 展示唯一入口）。
 */
import { useTimelineStore } from '../../stores/timelineStore';
import { useWorldStore } from '../../stores/worldStore';
import { formatSimHHMM } from '../../lib/simTime';

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

/** 快照 ISO 时点 → HH:MM（+08 展示，与日界口径一致；唯一定义在 lib/simTime） */
export function snapshotHHMM(simTime: string | null | undefined): string {
  return formatSimHHMM(simTime);
}

export default function LatencyBar() {
  const watermarkTick = useWorldStore((s) => s.watermarkTick);
  const latestTick = useTimelineStore((s) => s.latestTick);
  const ratio = useWorldStore((s) => s.snapshot?.compression_ratio ?? 1);
  const snapshot = useWorldStore((s) => s.snapshot);
  const lag = latestTick > 0 ? lagSeconds(watermarkTick, latestTick, ratio || 1) : 0;
  const level = latencyLevel(lag);
  const snapTime = snapshot?.snapshot_time ?? snapshot?.sim_time ?? null;
  return (
    <span data-testid="latency-bar" className={COLOR[level]}>
      世界状态截至 {snapshotHHMM(snapTime)}
      {snapshot?.snapshot_kind === 'rolling' ? '（滚动刷新）' : ''}
    </span>
  );
}
