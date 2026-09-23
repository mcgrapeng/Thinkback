import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";
import path from "node:path";

export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "./src"),
    },
  },
  server: {
    // 默认端口 7001（FRONTEND_PORT 可覆盖）；被占用时 vite 自动顺延
    port: Number(process.env.FRONTEND_PORT ?? 7001),
    // 本地开发代理到后端服务，避免 CORS 与 token 跨域配置
    proxy: {
      "/admin/api": {
        target: process.env.VITE_API_TARGET ?? "http://localhost:8000",
        changeOrigin: true,
      },
    },
  },
});
