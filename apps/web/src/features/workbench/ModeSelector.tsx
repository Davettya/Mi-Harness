import { ComposerMenu } from "./ComposerMenu";
export function ModeSelector({
  mode,
  currentMode,
  onChange,
  disabled,
}: {
  mode: string;
  currentMode?: string;
  onChange: (mode: "react" | "plan") => void;
  disabled?: boolean;
}) {
  return (
    <ComposerMenu
      label="执行模式"
      value={mode === "plan" ? "Plan" : "ReAct"}
      disabled={disabled}
      className="mode-menu"
    >
      {(close) => (
        <>
          <div className="popover-heading">执行模式</div>
          <div role="group" aria-label="可用模式">
            {(
              [
                ["react", "ReAct", "分析并执行任务"],
                ["plan", "Plan", "只读分析，制定计划"],
              ] as const
            ).map(([value, title, description]) => (
              <button
                type="button"
                className="composer-menu-option"
                role="radio"
                aria-checked={mode === value}
                key={value}
                onClick={() => {
                  onChange(value);
                  close();
                }}
              >
                <span>
                  <strong>{title}</strong>
                  <small>{description}</small>
                </span>
                <span aria-hidden="true">{mode === value ? "✓" : ""}</span>
              </button>
            ))}
          </div>
          {currentMode && currentMode !== mode && (
            <small className="model-control-status">
              当前运行：{currentMode}；下次提交：{mode}
            </small>
          )}
        </>
      )}
    </ComposerMenu>
  );
}
