import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

const backend = process.env.VIBEEVAL_BACKEND_URL ?? 'http://127.0.0.1:8000';
const proxy = {
  '/api': { target: backend, ws: true },
  '/r2-preview/model': { target: backend },
};

export default defineConfig({
  plugins: [react()],
  server: { proxy },
  preview: { proxy },
});
