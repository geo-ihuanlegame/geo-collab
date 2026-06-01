export function formatDateTime(isoString: string | null | undefined): string {
  if (!isoString) return "—";
  return new Date(isoString + 'Z').toLocaleString("zh-CN");
}

export function formatDate(isoString: string | null | undefined): string {
  if (!isoString) return "—";
  return new Date(isoString + 'Z').toLocaleDateString("zh-CN");
}

export function formatTime(isoString: string | null | undefined): string {
  if (!isoString) return "—";
  return new Date(isoString + 'Z').toLocaleTimeString("zh-CN");
}
