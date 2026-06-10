import { useState } from "react";
import {
  Bot, FileText, Images, LogOut, MessagesSquare, MonitorCog,
  MoreHorizontal, RadioTower, ScrollText, Send, Sparkles, Users, X,
} from "lucide-react";
import type { NavKey } from "../types";

type Item = { key: NavKey; label: string; icon: typeof Bot };

const BOTTOM: Item[] = [
  { key: "agents", label: "智能体", icon: Bot },
  { key: "content", label: "内容", icon: FileText },
  { key: "ai", label: "生文", icon: Sparkles },
  { key: "tasks", label: "分发", icon: Send },
];

const MORE_BASE: Item[] = [
  { key: "prompts", label: "提示词管理", icon: MessagesSquare },
  { key: "image-library", label: "图片库", icon: Images },
  { key: "media", label: "媒体矩阵", icon: RadioTower },
  { key: "system", label: "系统状态", icon: MonitorCog },
];

const MORE_ADMIN: Item[] = [
  { key: "admin", label: "用户管理", icon: Users },
  { key: "audit-logs", label: "审计日志", icon: ScrollText },
];

export function MobileNav({
  activeNav,
  onNavigate,
  isAdmin,
  username,
  onLogout,
}: {
  activeNav: NavKey;
  onNavigate: (key: NavKey) => void;
  isAdmin: boolean;
  username: string;
  onLogout: () => void;
}) {
  const [moreOpen, setMoreOpen] = useState(false);
  const moreItems = isAdmin ? [...MORE_BASE, ...MORE_ADMIN] : MORE_BASE;
  const bottomActive = BOTTOM.some((b) => b.key === activeNav);

  function go(key: NavKey) {
    setMoreOpen(false);
    onNavigate(key);
  }

  return (
    <>
      {moreOpen && (
        <div className="mobileMoreOverlay" onClick={() => setMoreOpen(false)}>
          <div className="mobileMoreSheet" onClick={(e) => e.stopPropagation()}>
            <div className="mobileMoreHead">
              <span>更多</span>
              <button type="button" className="iconButton" onClick={() => setMoreOpen(false)}>
                <X size={16} />
              </button>
            </div>
            <div className="mobileMoreGrid">
              {moreItems.map((it) => {
                const Icon = it.icon;
                return (
                  <button
                    key={it.key}
                    type="button"
                    className={`mobileMoreItem${activeNav === it.key ? " active" : ""}`}
                    onClick={() => go(it.key)}
                  >
                    <Icon size={20} />
                    <span>{it.label}</span>
                  </button>
                );
              })}
            </div>
            <div className="mobileMoreUser">
              <span className="mobileMoreName">{username}</span>
              <button type="button" className="secondaryButton" onClick={onLogout}>
                <LogOut size={15} /> 退出
              </button>
            </div>
          </div>
        </div>
      )}

      <nav className="mobileBottomBar">
        {BOTTOM.map((it) => {
          const Icon = it.icon;
          return (
            <button
              key={it.key}
              type="button"
              className={`mobileTab${activeNav === it.key ? " active" : ""}`}
              onClick={() => go(it.key)}
            >
              <Icon size={20} />
              <span>{it.label}</span>
            </button>
          );
        })}
        <button
          type="button"
          className={`mobileTab${!bottomActive || moreOpen ? " active" : ""}`}
          onClick={() => setMoreOpen((v) => !v)}
        >
          <MoreHorizontal size={20} />
          <span>更多</span>
        </button>
      </nav>
    </>
  );
}
