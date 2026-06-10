import { Bot, FileText, MoreHorizontal, Send, Sparkles } from "lucide-react";
import type { NavKey } from "../types";

type Item = { key: NavKey; label: string; icon: typeof Bot };

const BOTTOM: Item[] = [
  { key: "agents", label: "智能体", icon: Bot },
  { key: "ai", label: "生文", icon: Sparkles },
  { key: "content", label: "内容", icon: FileText },
  { key: "tasks", label: "分发", icon: Send },
];

export function MobileNav({
  activeNav,
  onNavigate,
  moreActive,
  onMoreClick,
}: {
  activeNav: NavKey;
  onNavigate: (key: NavKey) => void;
  moreActive: boolean;
  onMoreClick: () => void;
}) {
  return (
    <nav className="mobileBottomBar">
      {BOTTOM.map((it) => {
        const Icon = it.icon;
        return (
          <button
            key={it.key}
            type="button"
            className={`mobileTab${!moreActive && activeNav === it.key ? " active" : ""}`}
            onClick={() => onNavigate(it.key)}
          >
            <Icon size={20} />
            <span>{it.label}</span>
          </button>
        );
      })}
      <button
        type="button"
        className={`mobileTab${moreActive ? " active" : ""}`}
        onClick={onMoreClick}
      >
        <MoreHorizontal size={20} />
        <span>更多</span>
      </button>
    </nav>
  );
}
