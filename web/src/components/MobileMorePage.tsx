import {
  ChevronRight, Flame, Images, LogOut, MessagesSquare, MonitorCog,
  Plug, RadioTower, ScrollText, User, Users,
} from "lucide-react";
import type { NavKey } from "../types";

type Row = { key: NavKey; label: string; icon: typeof Users };
type Group = { title: string; rows: Row[] };

export function MobileMorePage({
  username,
  role,
  isAdmin,
  onNavigate,
  onLogout,
}: {
  username: string;
  role: string;
  isAdmin: boolean;
  onNavigate: (key: NavKey) => void;
  onLogout: () => void;
}) {
  const groups: Group[] = [
    { title: "资讯", rows: [{ key: "hot-lists", label: "热榜", icon: Flame }] },
    { title: "内容工具", rows: [{ key: "prompts", label: "提示词管理", icon: MessagesSquare }] },
    {
      title: "素材",
      rows: [
        { key: "image-library", label: "图片库", icon: Images },
        { key: "media", label: "媒体矩阵", icon: RadioTower },
      ],
    },
    {
      title: "系统",
      rows: [
        { key: "system", label: "系统状态", icon: MonitorCog },
        { key: "mcp", label: "MCP 接入", icon: Plug },
      ],
    },
    ...(isAdmin
      ? [
          {
            title: "管理",
            rows: [
              { key: "admin" as NavKey, label: "用户管理", icon: Users },
              { key: "audit-logs" as NavKey, label: "审计日志", icon: ScrollText },
            ],
          },
        ]
      : []),
  ];

  return (
    <div className="mobileMorePage">
      <div className="mobileProfile">
        <div className="mobileAvatar">
          <User size={30} />
        </div>
        <div className="mobileProfileName">{username}</div>
        <div className="mobileProfileRole">{role === "admin" ? "管理员" : "操作员"}</div>
      </div>

      {groups.map((g) => (
        <div className="mobileMoreGroup" key={g.title}>
          <div className="mobileMoreGroupTitle">{g.title}</div>
          <div className="mobileMoreList">
            {g.rows.map((r) => {
              const Icon = r.icon;
              return (
                <button
                  key={r.key}
                  type="button"
                  className="mobileMoreRow"
                  onClick={() => onNavigate(r.key)}
                >
                  <Icon size={18} />
                  <span>{r.label}</span>
                  <ChevronRight size={18} className="mobileMoreChevron" />
                </button>
              );
            })}
          </div>
        </div>
      ))}

      <button type="button" className="mobileLogoutRow" onClick={onLogout}>
        <LogOut size={18} /> 退出登录
      </button>
    </div>
  );
}
