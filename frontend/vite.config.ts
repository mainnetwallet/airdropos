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

// `npm run dev:mobile` (vite --mode mobile) turns the HMR WebSocket off. On a phone, Chrome drops that
// socket when the tab is backgrounded and Vite then full-reloads the page on return.
export default defineConfig(({ mode }) => ({
  customLogger: logger,
  plugins: [react()],
  resolve: {
    alias: {
      '@': path.resolve(__dirname, './src'),
    },
  },
  build: {
    rollupOptions: {
      output: {
        manualChunks: {
          react: ['react', 'react-dom', 'react-router-dom'],
          query: ['@tanstack/react-query', 'zustand'],
          markdown: ['react-markdown', 'remark-gfm'],
        },
      },
    },
  },
  test: {
    environment: 'jsdom',
    include: ['src/**/*.test.{ts,tsx}'],
  },
  server: {
    port: 5173,
    host: true,
    // hmr:false alone keeps the WebSocket (and its reload-on-reconnect); ws:false removes it.
    hmr: mode === 'mobile' ? false : undefined,
    ws: mode === 'mobile' ? false : undefined,
    proxy: {
      '/api': {
        target: 'http://localhost:8000',
        changeOrigin: true,
        ws: true,
      },
    },
  },
}))
