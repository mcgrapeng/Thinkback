import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";
import path from "node:path";
import { mockApi } from "./vite-mock-plugin";

export default defineConfig({
  plugins: [react(), tailwindcss(), ...(process.env.VITE_USE_MOCK === "1" ? [mockApi()] : [])],
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "./src"),
    },
  },
  server: {
    // 默认端口 7001（FRONTEND_PORT 可覆盖）；被占用时 vite 自动顺延
    port: Number(process.env.FRONTEND_PORT ?? 7001),
    // 本地开发代理到后端服务，避免 CORS 与 token 跨域配置
    // VITE_USE_MOCK=1 时 mock 插件直接接管 /admin/api/*，proxy 不需要
    proxy: process.env.VITE_USE_MOCK === "1"
      ? undefined
      : {
          "/admin/api": {
            target: process.env.VITE_API_TARGET ?? "http://localhost:8000",
            changeOrigin: true,
          },
        },
  },
});
