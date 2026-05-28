import type { CSSProperties, ReactNode } from "react";

export function Modal({
  title,
  children,
  footer,
  onClose,
  width,
  maxHeight,
}: {
  title: string;
  children: ReactNode;
  footer: ReactNode;
  onClose: () => void;
  width?: number | string;
  maxHeight?: number | string;
}) {
  const style: CSSProperties = {};
  if (width !== undefined) {
    style.width = typeof width === "number" ? `min(${width}px, 100%)` : width;
  }
  if (maxHeight !== undefined) {
    style.maxHeight = typeof maxHeight === "number" ? `min(${maxHeight}px, calc(100vh - 48px))` : maxHeight;
  }
  return (
    <div
      className="modalBackdrop"
      role="dialog"
      aria-modal="true"
      onClick={onClose}
    >
      <div
        className="modal"
        style={Object.keys(style).length ? style : undefined}
        onClick={(e) => e.stopPropagation()}
      >
        <header className="modalHeader">
          <h3>{title}</h3>
          <button className="iconButton" type="button" onClick={onClose}>
            ×
          </button>
        </header>
        <div className="modalContent">{children}</div>
        <footer className="modalActions">{footer}</footer>
      </div>
    </div>
  );
}
