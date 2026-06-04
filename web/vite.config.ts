import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// 代理目标：默认本地后端；设置 GEO_API_TARGET 可指向外网后端做纯前端联调。
// 例：$env:GEO_API_TARGET = "https://your-backend.example.com"; pnpm --filter @geo/web dev
const apiTarget = process.env.GEO_API_TARGET ?? "http://127.0.0.1:8000";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": {
        target: apiTarget,
        changeOrigin: true, // 改写 Host 头，远程虚拟主机 / HTTPS SNI 需要
        secure: false, // 容忍自签证书；正式证书无影响
        // 让远程后端下发的 httpOnly 鉴权 cookie 能落在 localhost 上：
        // 1) 把 cookie 域改写到 localhost；2) 去掉 Secure（本地是 http，否则浏览器丢弃）；
        //    SameSite=None 降级为 Lax，避免无 Secure 时被拒。
        cookieDomainRewrite: "localhost",
        configure: (proxy) => {
          proxy.on("proxyRes", (proxyRes) => {
            const sc = proxyRes.headers["set-cookie"];
            if (Array.isArray(sc)) {
              proxyRes.headers["set-cookie"] = sc.map((c) =>
                c.replace(/;\s*Secure/gi, "").replace(/;\s*SameSite=None/gi, "; SameSite=Lax"),
              );
            }
          });
        },
      },
    },
  },
});
