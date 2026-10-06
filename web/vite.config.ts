import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// Built output is served by FastAPI at /app (see app/main.py). Assets are
// hashed and emitted under app/static/app/assets so the CSP's `'self'` rule
// covers everything; nothing is loaded from a third-party origin.
export default defineConfig({
  plugins: [react()],
  base: '/app/',
  build: {
    outDir: '../app/static/app',
    emptyOutDir: true,
    sourcemap: false,
  },
  server: {
    port: 5173,
    proxy: {
      // Dev-only. The API's boundary rejects cross-origin mutations, so the
      // proxy rewrites Origin/Referer to look same-origin to the backend.
      '/api': {
        target: 'http://127.0.0.1:8008',
        changeOrigin: true,
        headers: { origin: 'http://127.0.0.1:8008', referer: 'http://127.0.0.1:8008/app' },
      },
    },
  },
})
