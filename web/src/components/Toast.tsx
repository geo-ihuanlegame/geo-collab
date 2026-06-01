import { createContext, useContext, useState, useCallback, type ReactNode } from "react";

type ToastType = "info" | "success" | "error";

type ToastItem = {
  id: number;
  message: string;
  type: ToastType;
};

type ToastContextValue = {
  toast: (message: string, type?: ToastType) => void;
};

const ToastContext = createContext<ToastContextValue>({ toast: () => {} });

let _nextId = 0;

export function ToastProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<ToastItem[]>([]);

  const toast = useCallback((message: string, type: ToastType = "info") => {
    const id = ++_nextId;
    setToasts((prev) => [...prev, { id, message, type }]);
    setTimeout(() => {
      setToasts((prev) => prev.filter((t) => t.id !== id));
    }, 4000);
  }, []);

  const dismiss = useCallback((id: number) => {
    setToasts((prev) => prev.filter((t) => t.id !== id));
  }, []);

  function getStyle(type: ToastType): { bg: string; color: string } {
    switch (type) {
      case "success": return { bg: "#e8f5e9", color: "#2e7d32" };
      case "error": return { bg: "var(--red-soft)", color: "var(--red)" };
      default: return { bg: "#e3f2fd", color: "#1565c0" };
    }
  }

  return (
    <ToastContext.Provider value={{ toast }}>
      {children}
      <div style={{ position: "fixed", top: 16, right: 16, zIndex: 9999, display: "flex", flexDirection: "column", gap: 8, maxWidth: 400 }}>
        {toasts.map((t) => {
          const style = getStyle(t.type);
          return (
            <div
              key={t.id}
              style={{
                background: style.bg,
                color: style.color,
                padding: "10px 16px",
                borderRadius: "var(--r)",
                fontSize: 13,
                display: "flex",
                alignItems: "center",
                gap: 10,
                boxShadow: "0 2px 8px rgba(0,0,0,0.12)",
                animation: "slideInRight .3s ease",
              }}
            >
              <span style={{ flex: 1 }}>{t.message}</span>
              <button
                type="button"
                onClick={() => dismiss(t.id)}
                style={{ background: "none", border: "none", cursor: "pointer", fontSize: 16, color: "inherit", opacity: 0.6, lineHeight: 1 }}
              >
                ×
              </button>
            </div>
          );
        })}
      </div>
    </ToastContext.Provider>
  );
}

export function useToast() {
  return useContext(ToastContext);
}
