/// <reference types="vitest" />
import { defineConfig, createLogger } from 'vite'
import react from '@vitejs/plugin-react'
import path from 'path'

// Vite prints WebSocket proxy failures itself via its logger (a proxy.on('error') handler cannot stop
// that). They are harmless in dev: the dashboard reconnects its WebSocket on every reload (twice
// under React StrictMode) and while the backend is still booting. Filter just those lines.
const logger = createLogger()
const baseError = logger.error
logger.error = (msg, options) => {
  if (typeof msg === 'string' && msg.includes('ws proxy')) return
  baseError(msg, options)
}

export default defineConfig({
  customLogger: logger,
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
      },
    },
  },
})
