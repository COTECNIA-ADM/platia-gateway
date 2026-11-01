/// <reference types="vitest/config" />
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import { resolve } from 'node:path'

// Panel de administración (G13b, ADR-017) y portal de usuario (G14, ADR-018) en un solo build.
// Nginx sirve los dos desde /panel/ (el portal, en /portal/ con portal.html) y hablan con el
// gateway por el mismo origen: /auth, /me, /admin y el WebSocket /events/stream.
// En desarrollo (npm run dev) esas rutas van al gateway levantado con Docker (puerto 8090).
const gateway = 'http://localhost:8090'

export default defineConfig({
  base: '/panel/',
  plugins: [
    react(),
    {
      // En desarrollo, /portal/… sirve la página del portal, como hace Nginx
      name: 'portal-en-desarrollo',
      configureServer(server) {
        server.middlewares.use((req, _res, next) => {
          if (req.url === '/portal' || req.url?.startsWith('/portal/')) req.url = '/panel/portal.html'
          next()
        })
      },
    },
  ],
  build: {
    sourcemap: false,
    rollupOptions: { input: { panel: resolve(__dirname, 'index.html'), portal: resolve(__dirname, 'portal.html') } },
  },
  server: {
    proxy: {
      '/auth': gateway,
      '/admin': gateway,
      '/me': gateway,
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
