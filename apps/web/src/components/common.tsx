import { useEffect, useRef, type ReactNode } from "react";
import { ApiError } from "../api/client";
import { pretty } from "../api/values";
import { statusLabels } from "../state/run-reducer";

export function Notice({
  children,
  tone = "info",
}: {
  children: ReactNode;
  tone?: "info" | "error" | "success";
}) {
  return (
    <div
      className={`notice ${tone}`}
      role={tone === "error" ? "alert" : "status"}
    >
      {children}
    </div>
  );
}
export function ErrorNotice({ error }: { error: unknown }) {
  if (!error) return null;
  return (
    <Notice tone="error">
      {error instanceof Error ? error.message : String(error)}
      {error instanceof ApiError && (
        <small>
          {error.code}
          {error.correlationId ? ` · 诊断编号 ${error.correlationId}` : ""}
        </small>
      )}
    </Notice>
  );
}
export function JsonDetail({
  value,
  label = "查看详细信息",
}: {
  value: unknown;
  label?: string;
}) {
  return (
    <details className="json-detail">
      <summary>{label}</summary>
      <pre>{pretty(value)}</pre>
    </details>
  );
}
export function Status({ status }: { status: string }) {
  return (
    <span className={`status status-${status}`}>
      <span />
      {statusLabels[status] ?? status}
    </span>
  );
}
export function Empty({
  title,
  children,
}: {
  title: string;
  children?: ReactNode;
}) {
  return (
    <div className="empty">
      <div className="empty-symbol">◈</div>
      <h3>{title}</h3>
      {children && <p>{children}</p>}
    </div>
  );
}
export function Modal({
  title,
  onClose,
  children,
}: {
  title: string;
  onClose: () => void;
  children: ReactNode;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    dialog.current?.showModal();
    const el = dialog.current;
    return () => el?.close();
  }, []);
  return (
    <dialog
      ref={dialog}
      className="modal"
      onCancel={(event) => {
        event.preventDefault();
        onClose();
      }}
    >
      <header>
        <h2>{title}</h2>
        <button aria-label="关闭" className="icon-button" onClick={onClose}>
          ×
        </button>
      </header>
      {children}
    </dialog>
  );
}
export function safeExternalUrl(value: unknown): string | undefined {
  if (typeof value !== "string") return undefined;
  try {
    const url = new URL(value);
    return ["https:", "http:"].includes(url.protocol) ? url.href : undefined;
  } catch {
    return undefined;
  }
}
/** Plain-text rendering deliberately keeps untrusted Markdown/HTML inert. */
export function SafeText({ text }: { text: string }) {
  return <div className="safe-text">{text}</div>;
}
