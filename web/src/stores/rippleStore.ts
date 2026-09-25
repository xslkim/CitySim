/** rippleStore（03 §6.1）：当前涟漪查询结果（一次只持一个源事件）。 */
import { create } from 'zustand';
import type { RippleData, RippleToday } from '../proto/ripple';

interface RippleState {
  current: RippleData | null;
  today: RippleToday | null;
  setCurrent: (r: RippleData | null) => void;
  setToday: (t: RippleToday) => void;
}

export const useRippleStore = create<RippleState>((set) => ({
  current: null,
  today: null,
  setCurrent: (current) => set({ current }),
  setToday: (today) => set({ today }),
}));
