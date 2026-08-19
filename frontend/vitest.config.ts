import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

// The frontend's own test run (`npm test`). It is a separate config from
// vite.config.ts on purpose: the dev server's /api proxy has no meaning
// here — a test that reaches the network is a test that is wrong — and the
// suite should not silently inherit whatever the dev config grows.
//
// jsdom gives the component tests a DOM; the pure helpers (units, flags,
// mapProblems) need nothing from it.
export default defineConfig({
  plugins: [react()],
  test: {
    environment: "jsdom",
    include: ["src/**/*.test.{ts,tsx}"],
    setupFiles: ["src/test/setup.ts"],
    globals: false,
    restoreMocks: true,
  },
});
