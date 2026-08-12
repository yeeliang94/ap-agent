import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The dev server forwards /api calls to the Python backend, so the browser
// talks to one origin and we avoid CORS trouble in development.
export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      "/api": "http://127.0.0.1:8002",
    },
  },
});
