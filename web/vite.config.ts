import react from '@vitejs/plugin-react';
import { defineConfig } from 'vitest/config';

// 端口钉死 5173（05 T-WEB-08，与 09 §8 step⑤ 及 T-WEB-20 就绪检查同口径）
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    strictPort: true,
    proxy: {
      // 开发期直连本机 obs-api（03 §8.1 拓扑的开发形态；token 经 ?token= 注入）
      '/api': { target: 'http://127.0.0.1:8080', changeOrigin: true },
      '/ws': { target: 'ws://127.0.0.1:8080', ws: true },
      '/assets': { target: 'http://127.0.0.1:8080', changeOrigin: true },
    },
  },
  test: {
    environment: 'jsdom',
    globals: true,
    setupFiles: ['src/test/setup.ts'],
  },
});
