import { defineConfig, type ProxyOptions } from "vite";
import react from "@vitejs/plugin-react";

const localApi: ProxyOptions = {
  target: "http://127.0.0.1:8767",
  changeOrigin: true,
  configure(proxy) {
    proxy.on("proxyReq", (outbound, incoming) => {
      // The loopback-only development proxy translates its own exact origin.
      if (
        ["http://127.0.0.1:5173", "http://localhost:5173"].includes(
          incoming.headers.origin ?? "",
        )
      )
        outbound.setHeader("Origin", "http://127.0.0.1:8767");
    });
  },
};

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    strictPort: true,
    proxy: {
      "/api": localApi,
      "/health": localApi,
    },
  },
  build: { sourcemap: true },
});
