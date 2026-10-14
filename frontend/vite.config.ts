import path from "node:path";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

export default defineConfig({
  plugins: [react()],
  // Matches "paths" in tsconfig.json — tsc understands "@/*" natively, but
  // Vite/Rollup needs its own alias declared, it doesn't read tsconfig.
  resolve: {
    alias: { "@": path.resolve(__dirname, "./src") },
  },
  server: {
    port: 5173,
    // Native fs-event watching is unreliable across Docker Desktop's bind
    // mount (verified: editing a file on the host doesn't trigger an HMR log
    // inside the container) — only enable polling when running inside
    // docker-compose (the CHOKIDAR_USEPOLLING env var is set there), not when
    // `npm run dev` runs directly on the host machine (polling wastes CPU
    // unnecessarily in that case).
    watch: { usePolling: process.env.CHOKIDAR_USEPOLLING === "true" },
  },
});
