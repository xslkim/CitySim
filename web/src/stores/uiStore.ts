/**
 * uiStore（03 §6.1）：路由上下文/选中/跟拍/调试态/播放倍率。
 * T-WEB-08 先立骨架（调试态开关 + 倍率），T-WEB-10 补齐五 store 合并语义。
 */
import { create } from 'zustand';

export type PlaybackRate = 0.5 | 1 | 2 | 4 | 8; // 03 §3.2 速度档（观察端播放倍率，与内核压缩比分显示）

interface UiState {
  debug: boolean;                    // 调试态开关（快捷键 d，03 §3.6）
  playbackRate: PlaybackRate;        // 客户端倍率 = 逐句播放唯一节拍权威（06 §2）
  selectedAgent: string | null;
  selectedLocation: string | null;
  followAgent: string | null;        // 跟拍角色（03 §3.1）
  toggleDebug: () => void;
  setPlaybackRate: (r: PlaybackRate) => void;
  selectAgent: (id: string | null) => void;
  selectLocation: (id: string | null) => void;
  setFollowAgent: (id: string | null) => void;
}

export const useUiStore = create<UiState>((set) => ({
  debug: false,
  playbackRate: 1,
  selectedAgent: null,
  selectedLocation: null,
  followAgent: null,
  toggleDebug: () => set((s) => ({ debug: !s.debug })),
  setPlaybackRate: (playbackRate) => set({ playbackRate }),
  selectAgent: (selectedAgent) => set({ selectedAgent }),
  selectLocation: (selectedLocation) => set({ selectedLocation }),
  setFollowAgent: (followAgent) => set({ followAgent }),
}));
