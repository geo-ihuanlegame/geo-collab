// 后端对无时区 datetime 在序列化时已统一补 'Z'（见 server main.py 的 datetime 补丁），
// 历史/个别串可能本就带时区（Z 或 ±HH:MM 偏移）。仅在串【不带】时区标记时才补 'Z' 当 UTC，
// 否则会出现双 'Z'（"...ZZ"）→ new Date 解析失败 → "Invalid Date"。非法值返回 null。
function toLocalDate(isoString: string): Date | null {
  const hasTimezone = /[zZ]$|[+-]\d{2}:?\d{2}$/.test(isoString);
  const d = new Date(hasTimezone ? isoString : isoString + "Z");
  return Number.isNaN(d.getTime()) ? null : d;
}

export function formatDateTime(isoString: string | null | undefined): string {
  if (!isoString) return "—";
  const d = toLocalDate(isoString);
  return d ? d.toLocaleString("zh-CN") : "—";
}

export function formatDate(isoString: string | null | undefined): string {
  if (!isoString) return "—";
  const d = toLocalDate(isoString);
  return d ? d.toLocaleDateString("zh-CN") : "—";
}

export function formatTime(isoString: string | null | undefined): string {
  if (!isoString) return "—";
  const d = toLocalDate(isoString);
  return d ? d.toLocaleTimeString("zh-CN") : "—";
}
