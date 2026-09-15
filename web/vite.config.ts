import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Port 3000 per PRD §4 (UI :3000). The API's CORS allowlist expects this exact origin.
export default defineConfig({
  plugins: [react()],
  server: { port: 3000, strictPort: true },
});
