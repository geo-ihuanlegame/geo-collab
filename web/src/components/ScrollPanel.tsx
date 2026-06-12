import { useLayoutEffect, useRef, type ReactNode } from "react";

// 跨 tab 浏览记忆：记录每个面板的滚动位置，切回时恢复（瀑布流浏览体验）。
// 模块级缓存，整个会话期间保留。
const scrollStore: Record<string, number> = {};

export function ScrollPanel({
  id,
  active,
  children,
}: {
  id: string;
  active: boolean;
  children: ReactNode;
}) {
  const ref = useRef<HTMLDivElement>(null);
  useLayoutEffect(() => {
    const el = ref.current;
    if (el && active) el.scrollTop = scrollStore[id] ?? 0;
  }, [active, id]);
  return (
    <div
      ref={ref}
      style={{ display: active ? undefined : "none" }}
      onScroll={(e) => {
        scrollStore[id] = e.currentTarget.scrollTop;
      }}
    >
      {children}
    </div>
  );
}
