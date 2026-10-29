import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// Dev: Vite on :5173 proxies /api to FastAPI on :8000. Prod: FastAPI serves dist/.
export default defineConfig({
  plugins: [react()],
  server: { proxy: { '/api': 'http://127.0.0.1:8000' } },
  // never inline assets as data: URIs, so the backend's strict CSP (font-src 'self') holds
  build: { assetsInlineLimit: 0 },
})
