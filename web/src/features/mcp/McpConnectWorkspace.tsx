import { useCallback, useEffect, useMemo, useState } from "react";
import {
  AlertTriangle,
  CheckCircle2,
  Copy,
  Eye,
  EyeOff,
  Loader2,
  Plug,
  RefreshCw,
  XCircle,
} from "lucide-react";
import { getMcpStatus, pingMcpHealth, type McpHealthResult, type McpStatus } from "../../api/mcp";
import { useToast } from "../../components/Toast";

const LOCALHOST_PATTERN = /^https?:\/\/(127\.0\.0\.1|localhost)(:\d+)?$/;

function buildHttpConfigJson(suggestedBaseUrl: string): string {
  const base = suggestedBaseUrl || "http://127.0.0.1:8000";
  const template = {
    mcpServers: {
      geo: {
        type: "http",
        url: `${base}/mcp/`,
        headers: { "X-MCP-Token": "<PASTE_YOUR_TOKEN_HERE>" },
      },
    },
  };
  return JSON.stringify(template, null, 2);
}

function buildStdioConfigJson(suggestedBaseUrl: string): string {
  const template = {
    mcpServers: {
      geo: {
        command: "python",
        args: ["-m", "server.mcp"],
        env: {
          GEO_MCP_TOKEN: "<PASTE_YOUR_TOKEN_HERE>",
          GEO_API_BASE_URL: suggestedBaseUrl || "http://127.0.0.1:8000",
          PYTHONPATH: "<PATH_TO_YOUR_LOCAL_geo-collab_CLONE>",
        },
      },
    },
  };
  return JSON.stringify(template, null, 2);
}

function isLocalhost(url: string): boolean {
  return LOCALHOST_PATTERN.test(url.trim());
}

// ─────────────────────────────────────────────────────────────────────────────
// Component
// ─────────────────────────────────────────────────────────────────────────────

export function McpConnectWorkspace() {
  const { toast } = useToast();

  // Section ② — server status
  const [status, setStatus] = useState<McpStatus | null>(null);
  const [statusError, setStatusError] = useState("");
  const [statusLoading, setStatusLoading] = useState(true);

  // Section ③ — copy state
  const [copied, setCopied] = useState(false);

  // Section ③ — transport (HTTP 推荐 / stdio 可选)
  const [transport, setTransport] = useState<"http" | "stdio">("http");

  // Section ④ — test connection
  const [token, setToken] = useState("");
  const [tokenVisible, setTokenVisible] = useState(false);
  const [testing, setTesting] = useState(false);
  const [testResult, setTestResult] = useState<McpHealthResult | null>(null);

  const refreshStatus = useCallback(async () => {
    setStatusLoading(true);
    try {
      const data = await getMcpStatus();
      setStatus(data);
      setStatusError("");
    } catch (err) {
      setStatus(null);
      setStatusError(err instanceof Error ? err.message : "获取 MCP 状态失败");
    } finally {
      setStatusLoading(false);
    }
  }, []);

  useEffect(() => {
    void refreshStatus();
  }, [refreshStatus]);

  const suggestedBaseUrl = status?.suggested_base_url ?? "http://127.0.0.1:8000";
  const configJson = useMemo(
    () =>
      transport === "http"
        ? buildHttpConfigJson(suggestedBaseUrl)
        : buildStdioConfigJson(suggestedBaseUrl),
    [suggestedBaseUrl, transport],
  );

  const onCopy = useCallback(async () => {
    try {
      if (navigator.clipboard && window.isSecureContext) {
        await navigator.clipboard.writeText(configJson);
      } else {
        // Fallback for non-secure context (e.g. http://intranet).
        const ta = document.createElement("textarea");
        ta.value = configJson;
        ta.style.position = "fixed";
        ta.style.opacity = "0";
        document.body.appendChild(ta);
        ta.select();
        document.execCommand("copy");
        document.body.removeChild(ta);
      }
      setCopied(true);
      toast("已复制 MCP 配置 JSON", "success");
      setTimeout(() => setCopied(false), 1500);
    } catch (err) {
      const message = err instanceof Error ? err.message : "复制失败";
      // Last-resort: tell the user to copy manually.
      // eslint-disable-next-line no-alert
      window.alert(`复制失败：${message}\n请手动选择代码块内容复制。`);
    }
  }, [configJson, toast]);

  const onTest = useCallback(async () => {
    const trimmed = token.trim();
    if (!trimmed) return;
    setTesting(true);
    setTestResult(null);
    try {
      const result = await pingMcpHealth(trimmed);
      setTestResult(result);
    } finally {
      setTesting(false);
    }
  }, [token]);

  const localhostWarn = isLocalhost(suggestedBaseUrl);

  return (
    <>
      <header className="topbar">
        <div>
          <p className="eyebrow">MCP 连接</p>
          <h1>Claude Code 接入</h1>
        </div>
        <div className="topActions">
          <button
            className="secondaryButton"
            type="button"
            disabled={statusLoading}
            onClick={() => void refreshStatus()}
          >
            <RefreshCw size={15} className={statusLoading ? "hotSpin" : ""} />
            刷新状态
          </button>
        </div>
      </header>

      <div style={{ display: "grid", gap: 16, maxWidth: 860 }}>
        {/* Section ① 概览 ─────────────────────────────────────────────── */}
        <section className="panel">
          <h2 style={{ marginBottom: 12, display: "flex", alignItems: "center", gap: 8 }}>
            <Plug size={18} /> 概览
          </h2>
          <p style={{ color: "var(--fg-2)", lineHeight: 1.7, marginBottom: 12 }}>
            Claude Code 通过 MCP 协议调用 GEO 平台工具，自动跑生文 / 分发 / 评估周报三条 Loop。
            本页给你客户端配置模板和 token 自检入口，不存任何密钥到 GEO。
          </p>
          <div style={{ color: "var(--fg-2)", fontSize: 13, lineHeight: 1.9 }}>
            <div>
              GEO 当前注册了{" "}
              <strong style={{ color: "var(--fg)" }}>
                {status ? status.tools_count : "—"}
              </strong>{" "}
              个 atomic tools 供 Claude Code 调用。
            </div>
            <div style={{ marginTop: 8 }}>3 条 Loop 配方（仓库内）：</div>
            <ul style={{ marginTop: 4, paddingLeft: 20, listStyle: "disc" }}>
              <li>
                <code style={inlineCode}>claude-loops/generation-loop.md</code> — 生文 Loop
              </li>
              <li>
                <code style={inlineCode}>claude-loops/distribute-loop.md</code> — 发文 Loop
              </li>
              <li>
                <code style={inlineCode}>claude-loops/weekly-report-loop.md</code> — 评估周报 Loop
              </li>
            </ul>
          </div>
        </section>

        {/* Section ② 服务端状态 ─────────────────────────────────────────── */}
        <section className="panel">
          <h2 style={{ marginBottom: 12 }}>服务端状态</h2>

          {statusLoading ? (
            <div style={{ color: "var(--fg-3)", display: "flex", alignItems: "center", gap: 8 }}>
              <Loader2 size={16} className="hotSpin" />
              加载中…
            </div>
          ) : statusError ? (
            <div
              style={{
                color: "var(--red)",
                background: "var(--red-soft)",
                border: "1px solid rgba(248,113,113,0.3)",
                padding: "10px 14px",
                borderRadius: 10,
                fontSize: 13,
              }}
            >
              <XCircle size={14} style={{ verticalAlign: "-2px", marginRight: 6 }} />
              {statusError}
            </div>
          ) : status ? (
            <div style={{ display: "grid", gap: 10 }}>
              <div>
                {status.configured ? (
                  <span className="badge succeeded">
                    <CheckCircle2 size={12} style={{ marginRight: 2 }} />
                    服务端 token 已配置
                  </span>
                ) : (
                  <span className="badge failed">
                    <XCircle size={12} style={{ marginRight: 2 }} />
                    服务端 token 未配置 — 请联系 admin 在 .env 加 GEO_MCP_TOKEN
                  </span>
                )}
              </div>
              {!status.configured ? (
                <div
                  style={{
                    color: "var(--yellow)",
                    background: "var(--yellow-soft)",
                    border: "1px solid rgba(251,191,36,0.3)",
                    padding: "10px 14px",
                    borderRadius: 10,
                    fontSize: 13,
                    lineHeight: 1.75,
                  }}
                >
                  <div style={{ fontWeight: 600, marginBottom: 4 }}>Admin 配置指引</div>
                  <ol style={{ paddingLeft: 20, margin: 0 }}>
                    <li>
                      生成 token：<code style={inlineCode}>openssl rand -hex 32</code>
                      （PowerShell 等价命令见 docs/mcp-setup-notes.md）
                    </li>
                    <li>
                      SSH 到部署机，写到 <code style={inlineCode}>.env</code>：
                      <code style={inlineCode}>{"GEO_MCP_TOKEN=<生成值>"}</code>
                    </li>
                    <li>
                      重启后端：<code style={inlineCode}>docker compose restart app</code>
                      ，回本页刷新
                    </li>
                  </ol>
                </div>
              ) : null}
              <div style={{ fontSize: 13, color: "var(--fg-2)" }}>
                <span style={{ marginRight: 8 }}>建议 base_url：</span>
                <code style={inlineCode}>{status.suggested_base_url}</code>
              </div>
              <div style={{ fontSize: 13, color: "var(--fg-2)" }}>
                <span style={{ marginRight: 8 }}>MCP endpoint：</span>
                <code style={inlineCode}>{status.suggested_base_url}/mcp/</code>
              </div>
              {localhostWarn ? (
                <div
                  style={{
                    color: "var(--red)",
                    background: "var(--red-soft)",
                    border: "1px solid rgba(248,113,113,0.3)",
                    padding: "10px 14px",
                    borderRadius: 10,
                    fontSize: 13,
                    display: "flex",
                    gap: 8,
                    alignItems: "flex-start",
                  }}
                >
                  <AlertTriangle size={14} style={{ marginTop: 2, flexShrink: 0 }} />
                  <div>
                    你看到的是本机地址，外部机器复制配置时请把{" "}
                    <code style={inlineCodeDanger}>GEO_API_BASE_URL</code> 改成公网域名（如{" "}
                    <code style={inlineCodeDanger}>https://geo.example.com</code>）。
                  </div>
                </div>
              ) : null}
            </div>
          ) : null}
        </section>

        {/* Section ③ 客户端配置 ─────────────────────────────────────────── */}
        <section className="panel">
          <div
            style={{
              display: "flex",
              alignItems: "center",
              justifyContent: "space-between",
              marginBottom: 12,
              gap: 12,
            }}
          >
            <h2 style={{ margin: 0 }}>客户端配置</h2>
            <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
              <div style={{ display: "inline-flex", borderRadius: 8, background: "var(--bg-2)", padding: 2 }}>
                <button
                  type="button"
                  onClick={() => setTransport("http")}
                  style={{
                    padding: "4px 10px",
                    fontSize: 12,
                    borderRadius: 6,
                    border: "none",
                    background: transport === "http" ? "var(--accent)" : "transparent",
                    color: transport === "http" ? "var(--bg)" : "var(--fg-2)",
                    cursor: "pointer",
                  }}
                >
                  HTTP（推荐）
                </button>
                <button
                  type="button"
                  onClick={() => setTransport("stdio")}
                  style={{
                    padding: "4px 10px",
                    fontSize: 12,
                    borderRadius: 6,
                    border: "none",
                    background: transport === "stdio" ? "var(--accent)" : "transparent",
                    color: transport === "stdio" ? "var(--bg)" : "var(--fg-2)",
                    cursor: "pointer",
                  }}
                >
                  stdio（本地 dev）
                </button>
              </div>
              <button
                type="button"
                className="secondaryButton"
                onClick={() => void onCopy()}
                disabled={!status}
              >
                {copied ? (
                  <>
                    <CheckCircle2 size={14} /> 已复制
                  </>
                ) : (
                  <>
                    <Copy size={14} /> 复制 JSON
                  </>
                )}
              </button>
            </div>
          </div>

          <p style={{ color: "var(--fg-2)", fontSize: 13, marginBottom: 10 }}>
            {transport === "http"
              ? "在你的机器上编辑 ~/.claude.json，粘贴以下片段（无需本机装 Python）："
              : "在你的机器上编辑 ~/.claude.json，粘贴以下片段（需要本机装 Python + clone 仓库）："}
          </p>

          <pre style={codeBlock}>
            <code style={{ fontFamily: "var(--mono, monospace)", fontSize: 12.5 }}>
              {configJson}
            </code>
          </pre>

          {transport === "http" ? (
            <ul style={{ marginTop: 14, paddingLeft: 20, listStyle: "disc", lineHeight: 1.9, fontSize: 13, color: "var(--fg-2)" }}>
              <li>
                <code style={inlineCode}>type</code>:Claude Code 现行字段名为{" "}
                <code style={inlineCode}>type</code>(旧版叫 <code style={inlineCode}>transport</code>,
                仍可识别但已不推荐)。
              </li>
              <li>
                <code style={inlineCode}>url</code>:自动填了你浏览器看到的域名;Claude Code
                跑在容器里时把域名换成 <code style={inlineCode}>http://host.docker.internal:8000</code>。
                <strong>末尾的 <code style={inlineCodeDanger}>/</code> 不能省</strong>——
                FastMCP 的 HTTP transport 路径是 <code style={inlineCode}>/mcp/</code>,
                少了尾斜杠会被反代或框架返回 <code style={inlineCode}>405</code>。
              </li>
              <li>
                <code style={inlineCode}>X-MCP-Token</code>:找 admin 获取。
              </li>
              <li>
                需要 Nginx 反代时,<code style={inlineCode}>location /mcp/</code> 块必须加{" "}
                <code style={inlineCode}>proxy_buffering off; proxy_request_buffering off;</code>
                (streamable HTTP 依赖 chunked,默认 buffering 会卡住 stream);同时
                <code style={inlineCode}>proxy_pass</code> 末尾保留 <code style={inlineCode}>/</code>
                以原样透传路径。
              </li>
            </ul>
          ) : (
            <>
              <div
                style={{
                  marginTop: 12,
                  color: "var(--red)",
                  background: "var(--red-soft)",
                  border: "1px solid rgba(248,113,113,0.3)",
                  padding: "10px 14px",
                  borderRadius: 10,
                  fontSize: 13,
                  lineHeight: 1.7,
                }}
              >
                <AlertTriangle size={14} style={{ verticalAlign: "-2px", marginRight: 6 }} />
                stdio 模式需要你的机器有 Python + clone 仓库,仅推荐本机开发 / air-gap 场景。
                日常使用请切回 HTTP。
              </div>
              <ul style={{ marginTop: 14, paddingLeft: 20, listStyle: "disc", lineHeight: 1.9, fontSize: 13, color: "var(--fg-2)" }}>
                <li>
                  <code style={inlineCode}>GEO_MCP_TOKEN</code>:找 admin 获取。
                </li>
                <li>
                  <code style={inlineCode}>GEO_API_BASE_URL</code>:默认填你浏览器看到的域名;Claude Code
                  跑在容器里访问宿主时改成{" "}
                  <code style={inlineCode}>http://host.docker.internal:8000</code>。
                </li>
                <li>
                  <code style={inlineCode}>PYTHONPATH</code>:在自己机器上{" "}
                  <code style={inlineCode}>
                    git clone https://github.com/geo-ihuanlegame/geo-collab.git
                  </code>{" "}
                  后填克隆出来的绝对路径;Windows 注意双反斜杠。
                </li>
              </ul>
            </>
          )}
        </section>

        {/* Section ④ 测试连接 ─────────────────────────────────────────── */}
        <section className="panel">
          <h2 style={{ marginBottom: 12 }}>测试连接</h2>
          <p style={{ color: "var(--fg-2)", fontSize: 13, marginBottom: 12, lineHeight: 1.7 }}>
            粘贴你拿到的 token，点测试 — 仅验证 token + 网络可达性；不验证 Claude Code 能否起
            MCP 子进程（后者请到 Claude Code <code style={inlineCode}>/mcp</code> 自查）。
          </p>

          <div style={{ display: "flex", gap: 10, alignItems: "stretch" }}>
            <div style={{ flex: 1, position: "relative" }}>
              <input
                type={tokenVisible ? "text" : "password"}
                value={token}
                onChange={(e) => setToken(e.target.value)}
                placeholder="粘贴 GEO_MCP_TOKEN…"
                autoComplete="off"
                spellCheck={false}
                style={{
                  ...field,
                  fontFamily: "var(--mono, monospace)",
                  paddingRight: 40,
                }}
              />
              <button
                type="button"
                onClick={() => setTokenVisible((v) => !v)}
                aria-label={tokenVisible ? "隐藏 token" : "显示 token"}
                style={eyeBtn}
              >
                {tokenVisible ? <EyeOff size={16} /> : <Eye size={16} />}
              </button>
            </div>
            <button
              type="button"
              className="primaryButton"
              disabled={!token.trim() || testing}
              onClick={() => void onTest()}
            >
              {testing ? (
                <>
                  <Loader2 size={14} className="hotSpin" /> 测试中…
                </>
              ) : (
                "测试"
              )}
            </button>
          </div>

          {testResult ? (
            <div style={{ marginTop: 12 }}>
              {testResult.ok ? (
                <div
                  style={{
                    color: "var(--green)",
                    background: "var(--green-soft)",
                    border: "1px solid rgba(52,211,153,0.3)",
                    padding: "10px 14px",
                    borderRadius: 10,
                    fontSize: 13,
                    display: "flex",
                    alignItems: "center",
                    gap: 8,
                  }}
                >
                  <CheckCircle2 size={14} />
                  token 正确，网络可达
                </div>
              ) : (
                <div
                  style={{
                    color: "var(--red)",
                    background: "var(--red-soft)",
                    border: "1px solid rgba(248,113,113,0.3)",
                    padding: "10px 14px",
                    borderRadius: 10,
                    fontSize: 13,
                    display: "flex",
                    alignItems: "flex-start",
                    gap: 8,
                  }}
                >
                  <XCircle size={14} style={{ marginTop: 2, flexShrink: 0 }} />
                  <div>
                    {testResult.status === 0
                      ? `[网络错误] ${testResult.message}`
                      : `[${testResult.status}] ${testResult.message}`}
                  </div>
                </div>
              )}
            </div>
          ) : null}
        </section>

        {/* Section ⑤ 故障排查 ─────────────────────────────────────────── */}
        <section className="panel">
          <details>
            <summary
              style={{
                cursor: "pointer",
                fontFamily: "var(--display)",
                fontSize: 19,
                fontWeight: 640,
                letterSpacing: "-0.3px",
                color: "var(--fg)",
              }}
            >
              故障排查（点击展开）
            </summary>
            <div style={{ marginTop: 16, display: "grid", gap: 14 }}>
              <TroubleshootRow
                code="401 MCP token not configured"
                hint="GEO 后端 .env 没读到 GEO_MCP_TOKEN。让 admin 检查 .env 内容并重启 uvicorn（docker 部署：docker compose restart app）。"
              />
              <TroubleshootRow
                code="401 invalid MCP token"
                hint="两边 token 不一致。对照本页段 ④ 的「测试连接」验证你的 token 是否被服务端接受；不接受就让 admin 给你最新 token。"
              />
              <TroubleshootRow
                code="405 Method Not Allowed"
                hint="URL 末尾少了 / —— FastMCP 的 streamable HTTP 路径是 /mcp/，少斜杠会触发反代或框架的 method 不匹配。按段 ③ 模板把 url 改成以 /mcp/ 结尾即可；如果用 Nginx 反代，确认 location 块也是 /mcp/ 且 proxy_pass 末尾带 /。"
              />
              <TroubleshootRow
                code="421 Misdirected Request"
                hint="反代 SNI / Host header 失配。常见原因：客户端走 https 但反代后端是 http、Nginx 配置里有 server_name 路由到错的 vhost、或 CDN/WAF 强制了 HTTP/2 而后端不支持。让 admin 检查反代 server_name 是否覆盖你访问的域名，并保留 proxy_set_header Host $host;。"
              />
              <TroubleshootRow
                code="502 Bad Gateway / 504 Gateway Timeout"
                hint="后端进程没起、Nginx upstream 不通，或 streamable HTTP 被 buffering 卡住。先 docker compose ps 看 app 容器是否 running；如果 ps 正常但仍 502/504，去 nginx 的 location /mcp/ 块加 proxy_buffering off; proxy_request_buffering off; 并 reload。"
              />
              <TroubleshootRow
                code="测试连接成功但 Claude Code /mcp 仍是 failed"
                hint="本页测试只校验 token + HTTP 可达，不验证 MCP 协议握手。先确认 ~/.claude.json 用的是段 ③ 的 type 为 http 的模板（不是旧版 transport 字段），url 末尾带 /；再重启 Claude Code 让它重新发起 initialize 请求。"
              />
              <TroubleshootRow
                code="Claude Code 完全看不到 geo server"
                hint="JSON 格式坏（用 jq 或在线 lint 验证），或 Claude Code 没重启。HTTP 模式还要确认 url / headers / type 三字段拼写正确——大小写和下划线都不能错。"
              />
              <TroubleshootRow
                code="stdio 模式：geo · connected · no tools"
                hint="99% 是 ~/.claude.json 的 command 配成了 python -m server.mcp.server。正确写法是 python -m server.mcp（即段 ③ stdio 模板里的 args）。改完重启 Claude Code。"
              />
              <TroubleshootRow
                code="stdio 模式：-32000 / Failed to reconnect / ModuleNotFoundError"
                hint="spawn 的 Python 没装 mcp[cli]，或 PYTHONPATH 没指向 geo-collab 仓库根。把 command 钉死成 python 的绝对路径，并用同一个 python 跑 pip install mcp[cli] httpx pydantic；PYTHONPATH 改成能 cd 进去看到 server/ 子目录的路径。"
              />
            </div>
          </details>
        </section>
      </div>
    </>
  );
}

// ─────────────────────────────────────────────────────────────────────────────
// Subcomponents + local styles
// ─────────────────────────────────────────────────────────────────────────────

function TroubleshootRow({ code, hint }: { code: string; hint: string }) {
  return (
    <div>
      <div
        style={{
          fontFamily: "var(--mono, monospace)",
          fontSize: 12.5,
          color: "var(--accent-deep)",
          marginBottom: 4,
        }}
      >
        {code}
      </div>
      <div style={{ color: "var(--fg-2)", fontSize: 13, lineHeight: 1.7 }}>{hint}</div>
    </div>
  );
}

const inlineCode: React.CSSProperties = {
  fontFamily: "var(--mono, monospace)",
  fontSize: 12,
  background: "var(--cream-2)",
  border: "1px solid var(--hair)",
  padding: "1px 6px",
  borderRadius: 6,
  color: "var(--fg)",
};

const inlineCodeDanger: React.CSSProperties = {
  ...inlineCode,
  color: "var(--red)",
  background: "rgba(248,113,113,0.10)",
  borderColor: "rgba(248,113,113,0.25)",
};

const codeBlock: React.CSSProperties = {
  background: "var(--surface-2)",
  border: "1px solid var(--hair)",
  borderRadius: 10,
  padding: "14px 16px",
  margin: 0,
  overflowX: "auto",
  fontFamily: "var(--mono, monospace)",
  fontSize: 12.5,
  lineHeight: 1.55,
  color: "var(--fg)",
};

const field: React.CSSProperties = {
  width: "100%",
  height: 38,
  padding: "0 12px",
  border: "1px solid var(--hair-2, var(--hair))",
  borderRadius: 10,
  background: "var(--paper, var(--glass))",
  color: "var(--fg)",
  fontSize: 13,
  colorScheme: "dark",
  boxSizing: "border-box",
};

const eyeBtn: React.CSSProperties = {
  position: "absolute",
  right: 6,
  top: 4,
  height: 30,
  width: 30,
  display: "inline-flex",
  alignItems: "center",
  justifyContent: "center",
  background: "transparent",
  border: "none",
  cursor: "pointer",
  color: "var(--fg-3)",
  padding: 0,
};
