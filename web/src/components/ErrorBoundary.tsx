import { Component } from "react";
import type { ErrorInfo, ReactNode } from "react";

interface Props {
  children: ReactNode;
  /** 出错时显示的上下文名称，如「内容管理」→ 标题渲染为「内容管理出错」 */
  title?: string;
}

interface State {
  hasError: boolean;
  error: Error | null;
}

export class ErrorBoundary extends Component<Props, State> {
  state: State = { hasError: false, error: null };

  static getDerivedStateFromError(error: Error): State {
    return { hasError: true, error };
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    console.error("ErrorBoundary caught:", error, info.componentStack);
  }

  render() {
    if (this.state.hasError) {
      return (
        <div role="alert" className="panel" style={{ borderColor: "var(--red-soft)", color: "var(--red)", padding: 24, margin: 24 }}>
          <h2>{this.props.title ? `${this.props.title}出错` : "出现错误"}</h2>
          <p>{this.state.error?.message || "未知错误"}</p>
          <button
            className="secondaryButton"
            type="button"
            onClick={() => {
              const message = this.state.error?.message ?? "";
              // React.lazy 会缓存失败的动态 import，原地重试无效，只能整页刷新
              if (/dynamically imported module|Importing a module script failed/i.test(message)) {
                window.location.reload();
                return;
              }
              this.setState({ hasError: false, error: null });
            }}
            style={{ marginTop: 12 }}
          >
            重试
          </button>
        </div>
      );
    }
    return this.props.children;
  }
}
