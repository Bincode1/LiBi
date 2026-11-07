import { fileURLToPath, URL } from 'node:url'
import { defineConfig, loadEnv } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

const workspacePort = 24157
const defaultRuntimeOrigin = 'http://127.0.0.1:24158'

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), 'PAICLI_WEB_')
  const key = process.env.PAICLI_WEB_RUNTIME_KEY || env.PAICLI_WEB_RUNTIME_KEY
  const target =
    process.env.PAICLI_WEB_RUNTIME_URL || env.PAICLI_WEB_RUNTIME_URL || defaultRuntimeOrigin
  const runtime = new URL(target)
  if (
    !['127.0.0.1', 'localhost', '[::1]'].includes(runtime.hostname) ||
    runtime.protocol !== 'http:' ||
    runtime.username ||
    runtime.password
  ) {
    throw new Error('Runtime URL must be a local HTTP origin without credentials.')
  }
  return {
    plugins: [
      react(),
      tailwindcss(),
      {
        name: 'paicli-local-bridge',
        configureServer(server) {
          server.middlewares.use((req, res, next) => {
            if (
              req.url?.startsWith('/v1/') &&
              (req.headers['sec-fetch-site'] === 'cross-site' ||
                (req.headers.origin && req.headers.origin !== `http://${req.headers.host}`))
            ) {
              res.statusCode = 403
              res.setHeader('Content-Type', 'application/json')
              res.end(JSON.stringify({ error: 'Cross-origin Runtime access is not allowed.' }))
              return
            }
            if (req.url === '/__paicli/status') {
              res.setHeader('Content-Type', 'application/json')
              res.end(JSON.stringify({ configured: Boolean(key) }))
              return
            }
            if (req.url?.startsWith('/v1/') && !key) {
              res.statusCode = 503
              res.setHeader('Content-Type', 'application/json')
              res.end(
                JSON.stringify({
                  error: '请在 frontend/.env.local 配置 PAICLI_WEB_RUNTIME_KEY 并重启前端。',
                }),
              )
              return
            }
            next()
          })
        },
      },
    ],
    resolve: { alias: { '@': fileURLToPath(new URL('./src', import.meta.url)) } },
    build: {
      rollupOptions: {
        output: {
          manualChunks: {
            markdown: ['streamdown'],
            primitives: ['@radix-ui/react-dialog', '@radix-ui/react-dropdown-menu'],
            framework: ['react', 'react-dom', 'react-router-dom', '@tanstack/react-query'],
          },
        },
      },
    },
    server: {
      host: '127.0.0.1',
      port: workspacePort,
      strictPort: true,
      proxy: {
        '/v1': {
          target: runtime.origin,
          changeOrigin: true,
          headers: key ? { 'x-api-key': key } : {},
          configure(proxy) {
            proxy.on('error', (_error, _req, res) => {
              if ('writeHead' in res && !res.headersSent) {
                res.writeHead(502, { 'Content-Type': 'application/json' })
                res.end(JSON.stringify({ error: '无法连接本地 Runtime，请确认服务已启动。' }))
              }
            })
          },
        },
      },
    },
    preview: { host: '127.0.0.1', port: workspacePort, strictPort: true, proxy: {} },
  }
})
