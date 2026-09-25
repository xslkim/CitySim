import react from '@vitejs/plugin-react';
import { createReadStream, existsSync } from 'node:fs';
import { extname, join, normalize } from 'node:path';
import type { Plugin } from 'vite';
import { defineConfig } from 'vitest/config';

const MIME: Record<string, string> = {
  '.png': 'image/png', '.svg': 'image/svg+xml', '.json': 'application/json',
};

/** 开发期 /assets/* 直托 ../ui/assets/*（06 T-ART-03 的 obs-api 挂载属 M5；生产 nginx 直托同口径）。
 *  路径白名单 = ui/assets 根目录，防目录穿越。 */
function devAssets(): Plugin {
  const root = normalize(join(__dirname, '..', 'ui', 'assets'));
  return {
    name: 'worldsim-dev-assets',
    configureServer(server) {
      server.middlewares.use('/assets', (req, res) => {
        const rel = normalize(req.url ?? '').replace(/^([/\\])+/, '');
        const path = join(root, rel);
        if (!path.startsWith(root) || !existsSync(path)) {
          res.statusCode = 404;
          res.end('not found');
          return;
        }
        res.setHeader('content-type', MIME[extname(path)] ?? 'application/octet-stream');
        createReadStream(path).pipe(res);
      });
    },
  };
}

// 端口钉死 5173（05 T-WEB-08，与 09 §8 step⑤ 及 T-WEB-20 就绪检查同口径）
export default defineConfig({
  plugins: [react(), devAssets()],
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
