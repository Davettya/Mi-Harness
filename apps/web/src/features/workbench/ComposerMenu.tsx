import { useEffect, useId, useRef, useState, type ReactNode } from "react";

export function ComposerMenu({
  label,
  value,
  disabled,
  children,
  className = "",
}: {
  label: string;
  value: string;
  disabled?: boolean;
  children: (close: () => void) => ReactNode;
  className?: string;
}) {
  const [open, setOpen] = useState(false);
  const root = useRef<HTMLDivElement>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  const id = useId();
  const close = () => {
    setOpen(false);
    trigger.current?.focus();
  };
  useEffect(() => {
    if (!open) return;
    const outside = (event: PointerEvent) => {
      if (!root.current?.contains(event.target as Node)) setOpen(false);
    };
    document.addEventListener("pointerdown", outside);
    const first = root.current?.querySelector<HTMLElement>(
      ".composer-popover input, .composer-popover button[aria-checked='true'], .composer-popover button:not(:disabled)",
    );
    first?.focus();
    return () => document.removeEventListener("pointerdown", outside);
  }, [open]);
  useEffect(() => {
    if (disabled) setOpen(false);
  }, [disabled]);
  return (
    <div
      ref={root}
      className={`composer-menu ${className}`}
      onBlur={(event) => {
        if (
          event.relatedTarget &&
          !event.currentTarget.contains(event.relatedTarget as Node)
        )
          setOpen(false);
      }}
      onKeyDown={(event) => {
        if (event.key === "Escape" && open) {
          event.preventDefault();
          event.stopPropagation();
          close();
        }
        if (
          open &&
          ["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key) &&
          !(
            event.target instanceof HTMLInputElement &&
            ["Home", "End"].includes(event.key)
          )
        ) {
          const items = Array.from(
            root.current?.querySelectorAll<HTMLButtonElement>(
              ".composer-popover button:not(:disabled)",
            ) ?? [],
          );
          if (!items.length) return;
          event.preventDefault();
          const index = items.indexOf(
            document.activeElement as HTMLButtonElement,
          );
          const next =
            event.key === "Home"
              ? 0
              : event.key === "End"
                ? items.length - 1
                : (index + (event.key === "ArrowUp" ? -1 : 1) + items.length) %
                  items.length;
          items[next].focus();
        }
      }}
    >
      <button
        ref={trigger}
        type="button"
        className="composer-menu-trigger"
        aria-label={`${label}：${value}`}
        aria-haspopup="dialog"
        aria-expanded={open}
        aria-controls={open ? id : undefined}
        disabled={disabled}
        title={value}
        onClick={() => setOpen(!open)}
        onKeyDown={(event) => {
          if (event.key === "ArrowUp" || event.key === "ArrowDown") {
            event.preventDefault();
            setOpen(true);
          }
        }}
      >
        <span>{value}</span>
        <span className="menu-chevron" aria-hidden="true">
          ⌃
        </span>
      </button>
      {open && (
        <div
          id={id}
          className="composer-popover"
          role="dialog"
          aria-label={label}
        >
          {children(close)}
        </div>
      )}
    </div>
  );
}
