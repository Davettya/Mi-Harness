import { useState } from "react";
import { object, string, type ObjectValue } from "../../api/values";
import { ComposerMenu } from "./ComposerMenu";

function profileIdentity(ref: string) {
  const separator = ref.lastIndexOf("@");
  return separator > 0 ? ref.slice(0, separator) : ref;
}

export function productModels(models: ObjectValue[]) {
  return models.filter(
    (model) =>
      profileIdentity(string(model.profile_ref)) !== "demo" &&
      string(model.provider_id) !== "local-demo" &&
      string(model.model_id) !== "deterministic-demo-v1",
  );
}

export function displayModel(models: ObjectValue[], ref: string) {
  const visible = productModels(models);
  return (
    visible.find((model) => string(model.profile_ref) === ref) ??
    visible.find(
      (model) =>
        profileIdentity(string(model.profile_ref)) === profileIdentity(ref),
    )
  );
}

export function visibleModelSelection(
  models: ObjectValue[],
  ...candidates: string[]
) {
  const visible = productModels(models);
  for (const candidate of candidates) {
    if (candidate && displayModel(visible, candidate)) return candidate;
  }
  return (
    string(visible.find((model) => !model.disabled_reason)?.profile_ref) ||
    string(visible[0]?.profile_ref)
  );
}

export function modelOptionDetail(model: ObjectValue) {
  const capability =
    object(object(model.capabilities).vision).status === "verified"
      ? "图文"
      : "文字 · 图片待验证";
  return [string(model.provider_id), capability, string(model.disabled_reason)]
    .filter(Boolean)
    .join(" · ");
}

export function ModelSelector({
  models,
  selected,
  control,
  onChange,
  disabled,
}: {
  models: ObjectValue[];
  selected: string;
  control: ObjectValue;
  onChange: (ref: string) => void;
  disabled?: boolean;
}) {
  const [query, setQuery] = useState("");
  const visible = productModels(models);
  const model = displayModel(visible, selected);
  const name = (ref: string) =>
    string(displayModel(visible, ref)?.model_id, "未配置模型");
  const filtered = visible.filter((m) =>
    `${m.provider_id} ${m.model_id}`
      .toLowerCase()
      .includes(query.trim().toLowerCase()),
  );
  return (
    <ComposerMenu
      label="会话模型"
      value={string(model?.model_id, selected || "选择模型")}
      disabled={disabled}
      className="model-menu"
    >
      {(close) => (
        <>
          <div className="popover-heading">选择模型</div>
          <input
            className="model-search"
            aria-label="搜索已配置模型"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="搜索模型或提供方"
          />
          <div className="model-options" role="group" aria-label="可用模型">
            {filtered.map((m) => (
              <button
                type="button"
                className="composer-menu-option"
                role="radio"
                aria-checked={
                  profileIdentity(string(m.profile_ref)) ===
                  profileIdentity(selected)
                }
                key={string(m.profile_ref)}
                disabled={Boolean(m.disabled_reason)}
                title={string(m.disabled_reason, string(m.model_id))}
                onClick={() => {
                  onChange(string(m.profile_ref));
                  setQuery("");
                  close();
                }}
              >
                <span>
                  <strong>{string(m.model_id)}</strong>
                  <small>{modelOptionDetail(m)}</small>
                </span>
                <span aria-hidden="true">
                  {profileIdentity(string(m.profile_ref)) ===
                  profileIdentity(selected)
                    ? "✓"
                    : ""}
                </span>
              </button>
            ))}
            {!filtered.length && (
              <p className="model-empty">
                {visible.length
                  ? "没有匹配的模型"
                  : "暂无可用模型，请先在设置中添加。"}
              </p>
            )}
          </div>
          {Boolean(control.effective_profile_ref) && (
            <small className="model-control-status" aria-live="polite">
              当前调用：{name(string(control.effective_profile_ref))}
              {control.status === "requested"
                ? `；已选择 ${name(string(control.desired_profile_ref))}，等待下一次调用`
                : control.status === "rejected"
                  ? `；切换被拒绝：${control.reason}`
                  : control.status === "not_applied"
                    ? "；运行结束，待选模型留给下次提交"
                    : ""}
            </small>
          )}
        </>
      )}
    </ComposerMenu>
  );
}
