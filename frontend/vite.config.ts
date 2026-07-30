import { fileURLToPath, URL } from 'node:url'

import vue from '@vitejs/plugin-vue'
// vitest's re-export, so the `test` block below type-checks as part of one config file.
import { defineConfig } from 'vitest/config'

export default defineConfig({
  plugins: [vue()],
  resolve: {
    alias: { '@': fileURLToPath(new URL('./src', import.meta.url)) },
  },
  // Built into the image by a Docker stage; the API process serves these as static assets.
  build: { outDir: 'dist', emptyOutDir: true },
  server: {
    // Dev only. In production the SPA and the API share an origin, so there is no proxy and no
    // CORS configuration to get wrong.
    proxy: {
      '/api': {
        // Docker's frontend container reaches the API by Compose service name; host development
        // keeps the familiar localhost default without needing an environment file.
        target: process.env.VITE_API_TARGET ?? 'http://localhost:8000',
        changeOrigin: true,
      },
    },
  },
  test: {
    environment: 'jsdom',
    include: ['src/**/__tests__/**/*.spec.ts'],
  },
})
