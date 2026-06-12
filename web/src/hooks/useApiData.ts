import { useCallback, useEffect, useRef, useState } from "react";

/**
 * 统一管理「加载一份远端数据」的 loading / error / data 三件套。
 * - 挂载时自动加载一次；refresh() 手动重载。
 * - 用自增 generation 丢弃过期响应（连续 refresh 的竞态安全）。
 * - 错误存入 state（不向上抛），不会冒泡成未处理 rejection。消费方应自行渲染 error。
 * - 注意：fetcher 经 ref 透传，**其闭包参数变化不会触发重新加载**。
 *   参数化场景（如带筛选条件的列表）必须自己在 effect 里调 refresh()。
 */
export function useApiData<T>(fetcher: () => Promise<T>) {
  const [data, setData] = useState<T | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<Error | null>(null);
  const fetcherRef = useRef(fetcher);
  fetcherRef.current = fetcher;
  const generationRef = useRef(0);

  const refresh = useCallback(async () => {
    const generation = ++generationRef.current;
    setLoading(true);
    try {
      const result = await fetcherRef.current();
      if (generationRef.current !== generation) return;
      setData(result);
      setError(null);
    } catch (err) {
      if (generationRef.current !== generation) return;
      console.warn("useApiData fetch failed", err);
      setError(err instanceof Error ? err : new Error(String(err)));
    } finally {
      if (generationRef.current === generation) setLoading(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  return { data, loading, error, refresh };
}

/**
 * 统一轮询：enabled 为 true 时每 intervalMs 调一次 fn，false 时自动清理。
 * fn 的异常在内部吞掉并 console.warn（轮询失败不应弹全局 toast 刷屏）。
 * fn 经 ref 透传，闭包里读到的 state 永远是最新值，无需加入依赖。
 */
export function usePolling(
  fn: () => void | Promise<void>,
  intervalMs: number,
  enabled: boolean,
  options?: { immediate?: boolean },
) {
  const fnRef = useRef(fn);
  fnRef.current = fn;
  const immediate = options?.immediate ?? false;

  useEffect(() => {
    if (!enabled) return;
    const tick = () => {
      void (async () => await fnRef.current())().catch((err) => {
        console.warn("usePolling tick failed", err);
      });
    };
    if (immediate) tick();
    const timer = window.setInterval(tick, intervalMs);
    return () => window.clearInterval(timer);
  }, [enabled, intervalMs, immediate]);
}
