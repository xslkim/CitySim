/** agentsStore（03 §6.1）：40 人静态档案 + 按需详情缓存（LRU 20 人）。 */
import { create } from 'zustand';
import type { AgentDetail, AgentState, AgentSummary, Reflection } from '../proto/agents';

export const AGENT_DETAIL_LRU = 20; // 03 §6.1

export interface AgentDetailBundle {
  detail?: AgentDetail;
  state?: AgentState;
  reflections?: Reflection[];
}

interface AgentsState {
  profiles: AgentSummary[];
  details: Map<string, AgentDetailBundle>; // LRU：插入序即最近使用序
  setProfiles: (p: AgentSummary[]) => void;
  cacheDetail: (id: string, bundle: AgentDetailBundle) => void;
  getDetail: (id: string) => AgentDetailBundle | undefined;
}

export const useAgentsStore = create<AgentsState>((set, get) => ({
  profiles: [],
  details: new Map(),
  setProfiles: (profiles) => set({ profiles }),
  cacheDetail: (id, bundle) =>
    set((s) => {
      const details = new Map(s.details);
      const prev = details.get(id);
      details.delete(id);
      details.set(id, { ...prev, ...bundle });
      while (details.size > AGENT_DETAIL_LRU) {
        const oldest = details.keys().next().value!;
        details.delete(oldest);
      }
      return { details };
    }),
  getDetail: (id) => {
    const d = get().details.get(id);
    if (d) get().cacheDetail(id, d); // touch（LRU 最近使用提升）
    return d;
  },
}));
