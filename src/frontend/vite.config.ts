import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";
import { defineConfig } from "vite";

// Local-only app: the dev server proxies /api to the FastAPI backend; the
// production build is served BY the backend (pv-extractor gui). No CDN,
// no external requests of any kind.
export default defineConfig({
  // Stamp the bundle with its build time so Settings can show whether the
  // served frontend was rebuilt after a code change (the backend reports its
  // own git commit separately). Evaluated once, at `npm run build`.
  define: {
    __BUILD_TIME__: JSON.stringify(new Date().toISOString()),
  },
  plugins: [react(), tailwindcss()],
  server: {
    host: "127.0.0.1",
    proxy: {
      "/api": {
        target: "http://127.0.0.1:8765",
        ws: true,
      },
    },
  },
  build: {
    chunkSizeWarningLimit: 900,
  },
});
