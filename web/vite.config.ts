import { fileURLToPath } from 'node:url'
import { defineConfig, loadEnv } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig(({ mode }) => {
  const projectRoot = fileURLToPath(new URL('..', import.meta.url))
  const env = loadEnv(mode, projectRoot, 'API_')
  const backend = `http://127.0.0.1:${env.API_PORT || '8765'}`
  return {
    plugins: [react()],
    server: {
      port: 5173,
      strictPort: true,
      proxy: {
        '/api': { target: backend, ws: true },
        '/healthz': { target: backend },
        '/readyz': { target: backend },
      },
    },
    build: { sourcemap: true },
  }
})
