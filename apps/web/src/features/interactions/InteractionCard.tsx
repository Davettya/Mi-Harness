import { useEffect, useState } from "react";
import { api, Command, ApiError } from "../../api/client";
import type { InteractionResponse } from "../../api/types";
import {
  array,
  number,
  object,
  string,
  type ObjectValue,
} from "../../api/values";
import {
  ErrorNotice,
  JsonDetail,
  Notice,
  SafeText,
} from "../../components/common";

export function SchemaFields({
  schema,
  value,
  onChange,
  disabled,
}: {
  schema: ObjectValue;
  value: ObjectValue;
  onChange: (value: ObjectValue) => void;
  disabled: boolean;
}) {
  const required = array(schema.required);
  const fields = Object.entries(object(schema.properties));
  const labels: Record<string, string> = {
    decision: "处理决定",
    text: "补充内容",
    approve_once: "仅允许本次操作",
    approve_scoped: "允许服务端列明的有限范围",
    deny: "拒绝此次操作",
    confirm_completed: "确认已经完成",
    confirm_not_executed: "确认尚未执行",
    accept_unresolved: "接受结果仍未确定",
    continue: "确认继续",
  };
  if (!fields.length)
    return (
      <label>
        补充信息
        <textarea
          disabled={disabled}
          required
          value={string(value.text)}
          onChange={(event) => onChange({ text: event.target.value })}
          rows={4}
        />
      </label>
    );
  return (
    <>
      {fields.map(([name, definition]) => {
        const field = object(definition);
        const choices = array(field.enum);
        const constant = field.const;
        const booleanField =
          field.type === "boolean" || typeof constant === "boolean";
        const update = (next: unknown) => onChange({ ...value, [name]: next });
        return (
          <label key={name}>
            {string(field.title, labels[name] ?? name)}
            {required.includes(name) && <span aria-label="必填"> *</span>}
            {choices.length ? (
              <select
                disabled={disabled}
                required={required.includes(name)}
                value={string(value[name])}
                onChange={(event) => update(event.target.value)}
              >
                <option value="">请选择</option>
                {choices.map((choice) => (
                  <option key={String(choice)} value={String(choice)}>
                    {labels[String(choice)] ?? String(choice)}
                  </option>
                ))}
              </select>
            ) : booleanField ? (
              <select
                disabled={disabled}
                value={value[name] === undefined ? "" : String(value[name])}
                required={required.includes(name)}
                onChange={(event) => update(event.target.value === "true")}
              >
                <option value="">请选择</option>
                {(constant === undefined || constant === true) && (
                  <option value="true">是</option>
                )}
                {(constant === undefined || constant === false) && (
                  <option value="false">否</option>
                )}
              </select>
            ) : (
              <input
                disabled={disabled}
                required={required.includes(name)}
                type={
                  field.type === "number" || field.type === "integer"
                    ? "number"
                    : "text"
                }
                value={String(value[name] ?? "")}
                onChange={(event) =>
                  update(
                    field.type === "number" || field.type === "integer"
                      ? Number(event.target.value)
                      : event.target.value,
                  )
                }
              />
            )}
            {field.description != null && (
              <small>{string(field.description)}</small>
            )}
          </label>
        );
      })}
    </>
  );
}

export function InteractionCard({
  record,
  onChanged,
  runEnded = false,
}: {
  record: ObjectValue;
  onChanged: () => void;
  runEnded?: boolean;
}) {
  const interactionId = string(record.interaction_id);
  const [detail, setDetail] = useState(record);
  const [response, setResponse] = useState<ObjectValue>({});
  const [pending, setPending] = useState(false);
  const [receipt, setReceipt] = useState<ObjectValue | null>(null);
  const [error, setError] = useState<unknown>();
  const [retry, setRetry] = useState<Command<InteractionResponse> | null>(null);
  useEffect(() => {
    setDetail(record);
    let active = true;
    void api
      .interaction(interactionId)
      .then((value) => {
        if (active) setDetail(value);
      })
      .catch((value) => {
        if (active) setError(value);
      });
    return () => {
      active = false;
    };
  }, [interactionId, record]);
  const disabled =
    runEnded || detail.status !== "open" || pending || Boolean(receipt);
  const kind = string(detail.kind);
  async function send(command?: Command<InteractionResponse>) {
    if (runEnded) return;
    setPending(true);
    setError(null);
    const next =
      command ??
      new Command<InteractionResponse>({
        expected_revision: number(detail.revision),
        response,
        ...(detail.binding_ref
          ? {
              binding_ref:
                detail.binding_ref as InteractionResponse["binding_ref"],
            }
          : {}),
      });
    setRetry(next);
    try {
      setReceipt(await api.respond(interactionId, next));
      setRetry(null);
      onChanged();
    } catch (error) {
      setError(error);
      if (error instanceof ApiError && [409, 410].includes(error.status)) {
        setRetry(null);
        setDetail(await api.interaction(interactionId));
      }
    } finally {
      setPending(false);
    }
  }
  return (
    <section className="interaction-card">
      <div className="eyebrow">
        {kind === "approval"
          ? "操作需要你的确认"
          : kind.includes("mcp") || kind === "elicitation"
            ? "MCP 补充信息"
            : "等待你的答复"}
      </div>
      <h3>
        {string(
          detail.title,
          kind === "approval" ? "审阅本次操作" : "补充任务信息",
        )}
      </h3>
      <SafeText text={string(detail.prompt)} />
      {detail.operation_preview != null && (
        <div className="operation-preview">
          <h4>本次操作与授权范围</h4>
          <p className="muted">
            {string(object(detail.operation_preview).tool)} ·{" "}
            {string(object(detail.operation_preview).scope)}
          </p>
          <pre>
            {JSON.stringify(object(detail.operation_preview).args, null, 2)}
          </pre>
          {object(detail.operation_preview).diff != null && (
            <>
              <h4>文件更改预览</h4>
              <pre>{string(object(detail.operation_preview).diff)}</pre>
            </>
          )}
        </div>
      )}
      {detail.expires_at != null && (
        <p className="muted">
          有效期至 {new Date(string(detail.expires_at)).toLocaleString()}
        </p>
      )}
      <JsonDetail
        value={
          detail.review_material ??
          detail.materials ?? {
            ...object(detail.operation_preview),
            binding_ref: detail.binding_ref,
          }
        }
        label="完整审阅材料与授权绑定"
      />
      <ErrorNotice error={error} />
      {receipt ? (
        <Notice tone="success">
          服务端已受理答复。运行状态将随事件更新。
          <JsonDetail value={receipt} label="提交回执" />
        </Notice>
      ) : (
        <form
          onSubmit={(event) => {
            event.preventDefault();
            void send();
          }}
        >
          <SchemaFields
            schema={object(detail.response_schema)}
            value={response}
            onChange={(value) => {
              setResponse(value);
              setRetry(null);
            }}
            disabled={disabled}
          />
          {detail.status !== "open" && (
            <Notice>
              此交互当前为 {string(detail.status)}，请以服务端最新状态为准。
            </Notice>
          )}
          {runEnded && <Notice>任务已经结束，此答复入口已停用。</Notice>}
          <div className="actions">
            <button className="primary" disabled={disabled}>
              {pending ? "正在提交…" : "提交本次答复"}
            </button>
            {retry && !pending && !runEnded && (
              <button type="button" onClick={() => void send(retry)}>
                重试原答复
              </button>
            )}
          </div>
        </form>
      )}
    </section>
  );
}
