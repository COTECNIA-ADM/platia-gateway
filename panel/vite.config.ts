/// <reference types="vitest/config" />
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// Panel de administración (G13b, ADR-017). Se sirve desde Nginx en /panel/ y habla con el
// gateway por el mismo origen: /auth, /admin y el WebSocket /events/stream.
// En desarrollo (npm run dev) esas rutas van al gateway levantado con Docker (puerto 8090).
const gateway = 'http://localhost:8090'

export default defineConfig({
  base: '/panel/',
  plugins: [react()],
  build: { sourcemap: false },
  server: {
    proxy: {
      '/auth': gateway,
      '/admin': gateway,
      '/events': { target: gateway.replace('http', 'ws'), ws: true },
    },
  },
  test: {
    environment: 'jsdom',
    include: ['src/**/*.test.{ts,tsx}'],
    setupFiles: ['src/test/setup.ts'],
    restoreMocks: true,
  },
})
