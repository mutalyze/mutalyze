import { defineConfig } from 'vite';
import { viteSingleFile } from 'vite-plugin-singlefile';

// Default build → normal multi-file (what Vercel deploys).
// SINGLEFILE=1 build → everything inlined into one dist/index.html
// (used to produce a portable, zero-setup preview).
export default defineConfig({
  plugins: process.env.SINGLEFILE ? [viteSingleFile()] : [],
});
