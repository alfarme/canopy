import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

export default defineConfig({
  plugins: [react()],
  // Absolute base: the SPA has deep routes (/plants/:id); relative asset
  // URLs would resolve against them and fall into the index.html fallback.
  base: '/',
  server: {
    host: true,
    proxy: {
      '/api': 'http://localhost:8080',
      '/photos': 'http://localhost:8080',
    },
  },
  build: {
    outDir: 'dist',
  },
});
