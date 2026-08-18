import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The dev server forwards /api calls to the Python backend, so the browser
// talks to one origin and we avoid CORS trouble in development. The
// backend port defaults to 8002 (start.sh); AP_API_PORT overrides it when a
// second backend is running for verification.
export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      "/api": `http://127.0.0.1:${process.env.AP_API_PORT ?? "8002"}`,
    },
  },
});
