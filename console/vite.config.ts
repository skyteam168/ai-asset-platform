import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// 构建产物由控制面在 /console 下同源提供；开发时把 /api 代理到本地控制面（python tools/dev.py serve）
export default defineConfig({
  base: "/console/",
  plugins: [react()],
  server: { port: 5173, proxy: { "/api": "http://127.0.0.1:18081" } },
  build: { outDir: "dist", sourcemap: false, chunkSizeWarningLimit: 800 },
  test: { environment: "jsdom", setupFiles: ["./src/test/setup.ts"], css: false, testTimeout: 20000, exclude: ["e2e/**", "node_modules/**"] },
});
