import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

// Dashboard runs on 5173 per CLAUDE.md. strictPort so a busy port fails loudly
// instead of silently moving the app somewhere the docs do not mention.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    strictPort: true,
  },
  preview: {
    port: 4173,
    strictPort: true,
  },
});
