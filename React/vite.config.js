import { fileURLToPath } from 'node:url';
import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

const reactDir = fileURLToPath(new URL('.', import.meta.url));
const sourceDir = fileURLToPath(new URL('../src', import.meta.url));

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: { '/src': sourceDir },
    dedupe: ['react', 'react-dom'],
  },
  server: {
    fs: { allow: [reactDir, sourceDir] },
    proxy: {
      '/api': 'http://127.0.0.1:8000',
    },
  },
});
