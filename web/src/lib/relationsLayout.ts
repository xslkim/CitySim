/** 关系图工具（02 §7.5：边宽 ∝ |affinity| 1~5px 线性；边色 affinity>0 positive/<0 negative；
 *  tension>50 叠加 warn 1px 内芯；演化动画节点锁死只变边）。 */
import type { RelationEdge } from '../proto/relations';

/** 边宽映射：|aff| ∈ [0,100] → 1~5px 线性（02 §7.5） */
export function edgeWidth(affinity: number): number {
  return 1 + Math.min(100, Math.abs(affinity)) / 100 * 4;
}

export function edgeColor(e: RelationEdge): string {
  return e.aff > 0 ? '#2EFA8D' : e.aff < 0 ? '#F75752' : '#8A93A0'; // positive/negative/neutral（02 §7.1）
}

export function edgeInnerCore(e: RelationEdge): string | null {
  return e.ten > 50 ? '#FEAE3E' : null; // tension>50 叠加 warn 1px 内芯（02 §7.5）
}

/** `?pair=A,B` 入口解析（03 §3.3） */
export function parsePair(pair: string | null): [string, string] | null {
  if (!pair) return null;
  const m = /^(A(?:0[1-9]|[1-3][0-9]|40)),(A(?:0[1-9]|[1-3][0-9]|40))$/.exec(pair);
  return m ? [m[1], m[2]] : null;
}
