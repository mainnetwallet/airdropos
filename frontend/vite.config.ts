/// <reference types="vitest" />
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import path from 'path'

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      '@': path.resolve(__dirname, './src'),
    },
  },
  test: {
    environment: 'jsdom',
    include: ['src/**/*.test.{ts,tsx}'],
  },
  server: {
    port: 5173,
    host: true,
    proxy: {
      '/api': {
        target: 'http://localhost:8000',
        changeOrigin: true,
        ws: true,
        configure: (proxy) => {
          // The dashboard reconnects its WebSocket on every reload (and twice in dev because of
          // React StrictMode). Writing to the already-closed socket throws ECONNABORTED/ECONNRESET,
          // which is harmless, so keep it out of the terminal.
          proxy.on('error', (err) => {
            const code = (err as NodeJS.ErrnoException).code
            if (code === 'ECONNABORTED' || code === 'ECONNRESET' || code === 'EPIPE') return
            console.error('[proxy]', err.message)
          })
        },
      },
    },
  },
})
