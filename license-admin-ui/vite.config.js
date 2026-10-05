import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Standalone admin UI for the License Server.
// In dev we proxy /api and /health to the license server (default :8001) so the
// browser talks to a same-origin path and avoids CORS quirks. In production set
// VITE_LICENSE_API_URL to the license server origin only if it's hosted
// separately from this UI — and add that origin to the server's
// CORS_ALLOWED_ORIGINS (license_server/app/core/config.py), which no longer
// sends a wildcard.
const LICENSE_SERVER = process.env.LICENSE_SERVER_URL || "http://localhost:8001";

export default defineConfig({
  plugins: [react()],
  server: {
    host: "0.0.0.0",
    port: 5173,
    strictPort: true,
    proxy: {
      "/api": { target: LICENSE_SERVER, changeOrigin: true },
      "/health": { target: LICENSE_SERVER, changeOrigin: true },
    },
  },
});
