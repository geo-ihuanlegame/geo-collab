import { useEffect } from "react";
import { useToast } from "./Toast";

/**
 * 捕获所有漏掉 catch 的 Promise 失败与未捕获异常，弹 error toast。
 * 已被局部 try/catch 处理的错误不会触发这两个事件。
 */
export function GlobalErrorListener() {
  const { toast } = useToast();

  useEffect(() => {
    function onRejection(event: PromiseRejectionEvent) {
      const reason: unknown = event.reason;
      const message =
        reason instanceof Error
          ? reason.message
          : typeof reason === "string" && reason
            ? reason
            : "未知错误";
      toast(`操作失败：${message}`, "error");
    }
    function onError(event: ErrorEvent) {
      if (!event.message) return;
      if (event.message.includes("ResizeObserver loop")) return;
      toast(`页面错误：${event.message}`, "error");
    }
    window.addEventListener("unhandledrejection", onRejection);
    window.addEventListener("error", onError);
    return () => {
      window.removeEventListener("unhandledrejection", onRejection);
      window.removeEventListener("error", onError);
    };
  }, [toast]);

  return null;
}
