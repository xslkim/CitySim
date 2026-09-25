/** 占位类型（T-WEB-08 编译用）；zod 事件 schema 归 T-WEB-09。 */
export interface ObsEvent {
  seq: number;
  tick: number;
  sim_time: string;
  type: string;
  source: string;
  trigger: string;
  arc_id: string | null;
  ui: Record<string, unknown> | null;
  payload: Record<string, unknown>;
}
