/// <reference types="vitest/config" />
import path from 'node:path'
import tailwindcss from '@tailwindcss/vite'
import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: {
      '@': path.resolve(import.meta.dirname, './src'),
    },
  },
  test: {
    environment: 'jsdom',
    globals: true,
    setupFiles: './src/test/setup.ts',
    css: true,
    env: {
      // A fixed, obviously-fake test-only value -- never a real endpoint --
      // so src/services/api.ts's tests can exercise the "configured"
      // code path deterministically regardless of whether a real
      // .env.local exists on the developer's machine.
      VITE_API_BASE_URL: 'https://example.test.invalid',
    },
  },
})
