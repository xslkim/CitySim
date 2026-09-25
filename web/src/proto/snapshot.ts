/** 占位类型（T-WEB-08 编译用）；zod 协议层归 T-WEB-09（proto/ 与 03 §5 一一对应）。 */
export interface SnapshotData {
  tick: number;
  sim_time: string;
  sim_day: number;
  compression_ratio: number;
  agents: unknown[];
  economy: { stocks: { symbol: string; price: number }[] };
  active_dialogues: unknown[];
}
