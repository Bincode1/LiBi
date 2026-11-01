import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const projectRoot = path.dirname(fileURLToPath(import.meta.url))

// https://vite.dev/config/
export default defineConfig({
  plugins: [react()],
  resolve: { alias: { '@': path.resolve(projectRoot, './src') } },
  build: { cssMinify: false },
  server: {
    host: '127.0.0.1',
    proxy: {
      '/v1': {
        target: process.env.PAICLI_RUNTIME_URL ?? 'http://127.0.0.1:8080',
        changeOrigin: true,
        configure(proxy) {
          proxy.on('proxyReq', (request) => {
            const key = process.env.PAICLI_RUNTIME_API_KEY
            if (key) request.setHeader('x-api-key', key)
          })
        },
      },
    },
  },
})
