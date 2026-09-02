import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Dev server proxies /api to the FastAPI backend on :8000. The production build
// (`vite build` -> dist/) is served by FastAPI itself, so /api is same-origin.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: { "/api": "http://localhost:8000" },
  },
  build: { outDir: "dist", emptyOutDir: true },
});
