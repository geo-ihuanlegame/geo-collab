// 后端返回的 noVNC URL 里 host 可能是 0.0.0.0 / 127.0.0.1 / localhost
// （服务端监听地址），浏览器无法直连。这里统一重写成当前访问主机后再打开。
export function normalizeRemoteBrowserUrl(rawUrl: string): string {
  const url = new URL(rawUrl, window.location.href);
  const localHosts = new Set(["0.0.0.0", "127.0.0.1", "localhost"]);
  if (localHosts.has(url.hostname)) {
    url.hostname = window.location.hostname;
    url.protocol = window.location.protocol;
    url.port = window.location.port;
    if (url.searchParams.has("host")) {
      url.searchParams.set("host", window.location.hostname);
    }
    if (url.searchParams.has("port")) {
      url.searchParams.set(
        "port",
        window.location.port || (window.location.protocol === "https:" ? "443" : "80"),
      );
    }
  }
  return url.toString();
}

export function openRemoteBrowser(url: string): void {
  window.open(normalizeRemoteBrowserUrl(url), "_blank", "noopener,noreferrer");
}
