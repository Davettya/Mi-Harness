import { useEffect, useState } from "react";
import { api, ApiError, Command } from "../../api/client";
import {
  array,
  id,
  items,
  number,
  object,
  pretty,
  string,
  type ObjectValue,
} from "../../api/values";
import {
  Empty,
  ErrorNotice,
  JsonDetail,
  Notice,
  SafeText,
  Status,
} from "../../components/common";
import type { RunProjection } from "../../state/run-reducer";

export type Panel = "context" | "artifacts" | "tools" | "tasks" | "memory";

function ArtifactPreview({ artifact }: { artifact: ObjectValue }) {
  const [url, setUrl] = useState("");
  const [text, setText] = useState<string | null>(null);
  const [error, setError] = useState<unknown>();
  const artifactId = string(artifact.artifact_id);
  const mime = string(artifact.mime_type).toLowerCase();
  const activeContent = mime.includes("html") || mime.includes("svg");
  const textual =
    activeContent ||
    mime.startsWith("text/") ||
    mime.includes("json") ||
    mime.includes("xml");
  useEffect(() => {
    const controller = new AbortController();
    let blobUrl = "";
    if (number(artifact.size_bytes) > 10_000_000) return;
    void fetch(api.artifactUrl(artifactId, true), {
      credentials: "same-origin",
      signal: controller.signal,
    })
      .then(async (response) => {
        if (!response.ok) throw new Error(`预览读取失败 (${response.status})`);
        const blob = await response.blob();
        if (controller.signal.aborted) return;
        if (textual) {
          const content = await blob.text();
          if (!controller.signal.aborted) setText(content);
          return;
        }
        blobUrl = URL.createObjectURL(blob);
        setUrl(blobUrl);
      })
      .catch((error) => {
        if (!controller.signal.aborted) setError(error);
      });
    return () => {
      controller.abort();
      if (blobUrl) URL.revokeObjectURL(blobUrl);
    };
  }, [artifactId, artifact.size_bytes, textual]);
  return (
    <div className="artifact-preview">
      <ErrorNotice error={error} />
      {text !== null && !activeContent ? (
        <>
          <pre className="text-artifact">{text.slice(0, 100000)}</pre>
          {text.length > 100000 && (
            <p className="muted">预览已截取前 100,000 字符，完整内容可下载。</p>
          )}
        </>
      ) : text !== null && activeContent ? (
        <iframe
          title={`产物预览 ${string(artifact.display_name, artifactId)}`}
          sandbox=""
          referrerPolicy="no-referrer"
          srcDoc={`<meta http-equiv="Content-Security-Policy" content="default-src 'none'; img-src data: blob:; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'">${text}`}
        />
      ) : url && mime.startsWith("image/") ? (
        <img
          className="artifact-image"
          src={url}
          alt={string(artifact.display_name, "产物图片")}
        />
      ) : url && mime !== "application/pdf" ? (
        <iframe
          title={`产物预览 ${string(artifact.display_name, artifactId)}`}
          src={url}
          sandbox=""
          referrerPolicy="no-referrer"
        />
      ) : (
        <p className="muted">
          {error
            ? "预览未完成，可以下载原文件查看。"
            : mime === "application/pdf" && url
              ? "PDF 可通过下方按钮下载到本机查看。"
              : number(artifact.size_bytes) > 10_000_000
                ? "文件超过 10 MB，请下载查看。"
                : "正在读取安全预览…"}
        </p>
      )}
      <a className="button" href={api.artifactUrl(artifactId)} download>
        下载原文件
      </a>
      <p className="muted">
        文本按纯文本显示；HTML 等主动内容隔离预览，禁止脚本与 Host 访问。
      </p>
    </div>
  );
}

function ToolDetail({
  record,
  workspace,
}: {
  record: ObjectValue;
  workspace: string;
}) {
  const [detail, setDetail] = useState(record);
  const [evidence, setEvidence] = useState("");
  const [decision, setDecision] = useState("");
  const [error, setError] = useState<unknown>();
  const [pending, setPending] = useState(false);
  const [receipt, setReceipt] = useState<ObjectValue | null>(null);
  useEffect(() => {
    let active = true;
    void api
      .operation(string(record.operation_id))
      .then((value) => {
        if (active) setDetail(value);
      })
      .catch((error) => {
        if (active) setError(error);
      });
    return () => {
      active = false;
    };
  }, [record]);
  const actions = array(detail.allowed_actions ?? detail.allowed_decisions);
  const [evidenceRefs, setEvidenceRefs] = useState<ObjectValue[]>([]);
  const [resultRef, setResultRef] = useState("");
  return (
    <article className="record">
      <h3>{string(detail.tool_id, "工具操作")}</h3>
      <p className="muted">
        {string(detail.status ?? detail.result_status ?? detail.event_type)} ·
        尝试 {number(detail.attempt, 1)}
      </p>
      <SafeText
        text={
          typeof detail.result_summary === "string"
            ? detail.result_summary
            : pretty(detail.result_summary ?? detail.input_summary)
        }
      />
      <JsonDetail value={detail} label="参数、来源与执行证据" />
      <ErrorNotice error={error} />
      {actions.length > 0 && (
        <form
          className="reconcile"
          onSubmit={async (event) => {
            event.preventDefault();
            setPending(true);
            setError(null);
            try {
              const result = await api.reconcile(
                string(detail.operation_id),
                new Command({
                  expected_revision: detail.revision,
                  decision,
                  note: evidence,
                  evidence_refs: evidenceRefs,
                  ...(resultRef
                    ? {
                        result_ref: evidenceRefs.find(
                          (value) => value.artifact_id === resultRef,
                        ),
                      }
                    : {}),
                }),
              );
              setReceipt(result);
              setDetail(await api.operation(string(detail.operation_id)));
            } catch (error) {
              setError(error);
            } finally {
              setPending(false);
            }
          }}
        >
          <h4>结果未知，需要核对</h4>
          <p>
            先检查外部结果，再按服务端允许的方式处理。接受未决结果不会表示工具成功。
          </p>
          <label>
            处理方式
            <select
              required
              value={decision}
              onChange={(event) => setDecision(event.target.value)}
            >
              <option value="">请选择</option>
              {actions.map((action) => {
                const value =
                  typeof action === "string"
                    ? action
                    : string(object(action).decision ?? object(action).id);
                return (
                  <option key={value} value={value}>
                    {typeof action === "string"
                      ? action
                      : string(object(action).label, value)}
                  </option>
                );
              })}
            </select>
          </label>
          <label>
            上传核对材料
            <input
              type="file"
              disabled={pending}
              onChange={async (event) => {
                const file = event.target.files?.[0];
                if (!file) return;
                setPending(true);
                try {
                  const reference = await api.upload(
                    workspace,
                    file,
                    crypto.randomUUID(),
                  );
                  setEvidenceRefs((previous) => [...previous, reference]);
                } catch (error) {
                  setError(error);
                } finally {
                  setPending(false);
                }
              }}
            />
          </label>
          {evidenceRefs.length > 0 && (
            <label>
              已验证的结果文件（确认完成时选择）
              <select
                value={resultRef}
                onChange={(event) => setResultRef(event.target.value)}
                required={decision === "confirm_completed"}
              >
                <option value="">请选择</option>
                {evidenceRefs.map((value) => (
                  <option
                    key={string(value.artifact_id)}
                    value={string(value.artifact_id)}
                  >
                    {string(value.display_name, string(value.artifact_id))}
                  </option>
                ))}
              </select>
            </label>
          )}
          <label>
            核对证据
            <textarea
              required
              value={evidence}
              onChange={(event) => setEvidence(event.target.value)}
              placeholder="记录已核验的目标、结果与依据"
            />
          </label>
          <button className="primary" disabled={pending || Boolean(receipt)}>
            {pending ? "正在核验…" : "提交核对材料"}
          </button>
          {receipt && (
            <Notice tone="success">
              核对决定已受理，实际结果以服务端为准。
              <JsonDetail value={receipt} />
            </Notice>
          )}
        </form>
      )}
    </article>
  );
}

function MemoryManager({ workspace }: { workspace: string }) {
  const [records, setRecords] = useState<ObjectValue[]>([]);
  const [selected, setSelected] = useState<ObjectValue | null>(null);
  const [draft, setDraft] = useState("");
  const [error, setError] = useState<unknown>();
  const [pending, setPending] = useState(false);
  const [receipt, setReceipt] = useState("");
  const [forget, setForget] = useState(false);
  const refresh = async () => setRecords(items(await api.memories(workspace)));
  useEffect(() => {
    let active = true;
    void api
      .memories(workspace)
      .then((value) => {
        if (active) setRecords(items(value));
      })
      .catch((error) => {
        if (active) setError(error);
      });
    return () => {
      active = false;
    };
  }, [workspace]);
  async function act(action: "save" | "accept" | "reject" | "forget") {
    if (!selected) return;
    setPending(true);
    setError(null);
    setReceipt("");
    try {
      if (action === "forget") {
        await api.forgetMemory(
          id(selected),
          number(selected.revision),
          new Command({}),
        );
        setSelected(null);
        setForget(false);
      } else {
        const result =
          action === "save"
            ? await api.updateMemory(
                id(selected),
                new Command({
                  content: draft,
                  expected_revision: selected.revision,
                }),
              )
            : await api.reviewMemory(
                id(selected),
                new Command({
                  decision: action,
                  expected_revision: selected.revision,
                }),
              );
        setSelected(result);
      }
      setReceipt(
        action === "forget" ? "服务端已确认遗忘此记忆。" : "记忆更改已保存。",
      );
      await refresh();
    } catch (error) {
      setError(error);
      if (error instanceof ApiError && error.status === 409) {
        setSelected(await api.memory(id(selected)));
        setReceipt("已读取最新版本，你的编辑草稿仍被保留，请核对后再提交。");
      }
    } finally {
      setPending(false);
    }
  }
  return (
    <>
      <ErrorNotice error={error} />
      {receipt && <Notice>{receipt}</Notice>}
      {records.length === 0 && (
        <Empty title="还没有保存的记忆">
          任务中提出的记忆会在这里等待审阅。
        </Empty>
      )}
      {records.map((record) => (
        <button
          className={`list-card ${selected && id(selected) === id(record) ? "selected" : ""}`}
          key={id(record)}
          onClick={async () => {
            try {
              const detail = await api.memory(id(record));
              setSelected(detail);
              setDraft(string(detail.content));
              setForget(false);
            } catch (error) {
              setError(error);
            }
          }}
        >
          <strong>
            {string(
              record.title,
              string(record.content, "记忆记录").slice(0, 64),
            )}
          </strong>
          <small>
            {string(record.scope)} · {string(record.review_state)} · v
            {number(record.revision)}
          </small>
        </button>
      ))}
      {selected && (
        <section className="record">
          <h3>编辑记忆</h3>
          <JsonDetail value={selected} label="来源、范围、审阅状态与有效期" />
          <label>
            内容
            <textarea
              rows={8}
              value={draft}
              onChange={(event) => setDraft(event.target.value)}
            />
          </label>
          <div className="actions">
            <button disabled={pending} onClick={() => void act("save")}>
              保存版本
            </button>
            {["pending", "proposed", "pending_review"].includes(
              string(selected.review_state),
            ) && (
              <>
                <button disabled={pending} onClick={() => void act("accept")}>
                  接受提议
                </button>
                <button disabled={pending} onClick={() => void act("reject")}>
                  拒绝提议
                </button>
              </>
            )}
            <button
              className="danger"
              disabled={pending}
              onClick={() => setForget(true)}
            >
              遗忘…
            </button>
          </div>
          {forget && (
            <Notice>
              将遗忘范围为 {string(selected.scope)} 的这条记忆。
              <SafeText text={string(selected.content)} />
              <button
                className="danger"
                disabled={pending}
                onClick={() => void act("forget")}
              >
                确认遗忘此记忆
              </button>
            </Notice>
          )}
        </section>
      )}
    </>
  );
}

export function Inspector({
  panel,
  run,
  workspace,
  onSelectRun,
  onClose,
}: {
  panel: Panel;
  run: RunProjection;
  workspace: string;
  onSelectRun: (id: string) => void;
  onClose: () => void;
}) {
  const [context, setContext] = useState<ObjectValue>({});
  const [error, setError] = useState<unknown>();
  const [preview, setPreview] = useState<ObjectValue | null>(null);
  const [pinText, setPinText] = useState("");
  const [pinPending, setPinPending] = useState(false);
  const [pinReceipt, setPinReceipt] = useState("");
  useEffect(() => {
    let active = true;
    setError(null);
    if (panel === "context" && run.runId)
      void api
        .context(run.runId)
        .then((value) => {
          if (active) setContext(value);
        })
        .catch((error) => {
          if (active) setError(error);
        });
    return () => {
      active = false;
    };
  }, [panel, run.runId, run.lastAppliedDurableSeq]);
  const titles: Record<Panel, string> = {
    context: "上下文",
    artifacts: "附件与产物",
    tools: "工具执行",
    tasks: "任务树",
    memory: "记忆管理",
  };
  const peakBudget = array(context.views)
    .map(object)
    .map((view) => object(view.budget_breakdown))
    .filter((budget) => typeof budget.total === "number")
    .reduce<ObjectValue>(
      (highest, budget) =>
        number(budget.total) > number(highest.total, -1) ? budget : highest,
      {},
    );
  return (
    <aside className="inspector">
      <header>
        <div>
          <div className="eyebrow">INSPECTOR</div>
          <h2>{titles[panel]}</h2>
        </div>
        <button
          className="icon-button"
          onClick={onClose}
          aria-label="关闭检查面板"
        >
          ×
        </button>
      </header>
      <div className="inspector-body">
        <ErrorNotice error={error} />
        {panel === "context" && (
          <>
            <div className="metric-grid">
              <div>
                <span>上下文估算峰值</span>
                <strong>
                  {peakBudget.total == null
                    ? "未知"
                    : number(peakBudget.total).toLocaleString()}
                </strong>
              </div>
              <div>
                <span>对应输入预算</span>
                <strong>
                  {peakBudget.input_limit == null
                    ? "未知"
                    : number(peakBudget.input_limit).toLocaleString()}
                </strong>
              </div>
            </div>
            <p className="muted">
              {string(
                peakBudget.algorithm_version,
                "估算方法与供应商计量以服务端报告为准。",
              )}
            </p>
            <JsonDetail
              value={context}
              label="来源、激活 Skill、压缩与原始档案"
            />
            <JsonDetail value={run.usage} label="用量、费用与未知项" />
            <section className="record">
              <h3>固定上下文约束</h3>
              <p className="muted">
                固定内容在后续模型回合保留，不能扩大工具权限。
              </p>
              {array(object(context.pins).items)
                .map(object)
                .map((pin) => (
                  <div key={string(pin.id)} className="notice">
                    <SafeText text={string(pin.text)} />
                  </div>
                ))}
              <form
                onSubmit={async (event) => {
                  event.preventDefault();
                  setPinPending(true);
                  setError(null);
                  setPinReceipt("");
                  try {
                    await api.pinContext(
                      run.runId,
                      new Command({
                        text: pinText,
                        expected_context_revision: number(
                          object(context.pins).revision,
                        ),
                      }),
                    );
                    setContext(await api.context(run.runId));
                    setPinText("");
                    setPinReceipt(
                      "固定内容已由服务端保存，下一安全输入边界生效。",
                    );
                  } catch (error) {
                    setError(error);
                    if (error instanceof ApiError && error.status === 409)
                      setContext(await api.context(run.runId));
                  } finally {
                    setPinPending(false);
                  }
                }}
              >
                <label>
                  需要持续遵守的内容
                  <textarea
                    rows={4}
                    required
                    value={pinText}
                    disabled={pinPending}
                    onChange={(event) => setPinText(event.target.value)}
                  />
                </label>
                <button disabled={!run.runId || pinPending || !pinText.trim()}>
                  {pinPending ? "正在保存…" : "固定此内容"}
                </button>
              </form>
              {pinReceipt && <Notice tone="success">{pinReceipt}</Notice>}
            </section>
          </>
        )}
        {panel === "artifacts" && (
          <>
            {Object.values(run.artifacts).length === 0 && (
              <Empty title="还没有产物">
                任务生成的文件和引用会出现在这里。
              </Empty>
            )}
            {Object.values(run.artifacts).map((artifact) => (
              <button
                className="list-card"
                key={string(artifact.artifact_id)}
                onClick={() => setPreview(artifact)}
              >
                <strong>
                  {string(artifact.display_name, string(artifact.artifact_id))}
                </strong>
                <small>
                  {string(artifact.mime_type)} ·{" "}
                  {(number(artifact.size_bytes) / 1024).toFixed(1)} KB
                </small>
                <small>版本 {string(artifact.content_hash).slice(0, 12)}</small>
              </button>
            ))}
            {preview && (
              <>
                <JsonDetail value={preview} label="产物来源与版本" />
                <ArtifactPreview
                  key={string(preview.artifact_id)}
                  artifact={preview}
                />
              </>
            )}
          </>
        )}
        {panel === "tools" && (
          <>
            {Object.values(run.tools).length === 0 && (
              <Empty title="等待工具执行">
                工具来源、参数与结果将在此汇总。
              </Empty>
            )}
            {Object.values(run.tools).map((record) => (
              <ToolDetail
                key={string(record.operation_id)}
                record={record}
                workspace={workspace}
              />
            ))}
          </>
        )}
        {panel === "tasks" && (
          <>
            <button
              className="list-card selected"
              onClick={() => onSelectRun(run.runId)}
            >
              <strong>当前任务</strong>
              <Status status={run.serverStatus} />
              <small>{run.runId}</small>
            </button>
            {Object.values(run.children).map((child) => (
              <div
                className="list-card child"
                key={string(child.child_run_id ?? child.run_id)}
              >
                <button
                  className="text-button"
                  onClick={() =>
                    onSelectRun(string(child.child_run_id ?? child.run_id))
                  }
                >
                  <strong>{string(child.goal_summary, "子任务")}</strong>
                </button>
                <Status status={string(child.status)} />
                <JsonDetail value={child} label="预算与结果" />
              </div>
            ))}
            {!Object.keys(run.children).length && (
              <p className="muted">当前任务尚未派生子任务。</p>
            )}
          </>
        )}
        {panel === "memory" && <MemoryManager workspace={workspace} />}
      </div>
    </aside>
  );
}
