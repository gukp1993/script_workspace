import { fileURLToPath, URL } from 'node:url'

import vue from '@vitejs/plugin-vue'
import { defineConfig } from 'vitest/config'

// 控制面后端地址（desktop_shell 启动的进程内 uvicorn，仅回环）
const BACKEND = process.env.VAW_BACKEND ?? 'http://127.0.0.1:17653'

export default defineConfig({
  plugins: [vue()],
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },
  server: {
    port: 5173,
    // dev 模式经 Vite 代理访问后端（生产模式由桌面壳同源加载 dist/）
    proxy: {
      '/api': {
        target: BACKEND,
        changeOrigin: true,
        ws: true,
      },
    },
  },
  test: {
    environment: 'jsdom',
    include: ['tests/**/*.test.ts', 'src/**/*.test.ts'],
  },
})
