import { useEffect, useState } from "react";
import { ModelSettings } from "./ModelSettings";
import { api, ApiError, Command, type ConfigKind } from "../../api/client";
import {
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
  safeExternalUrl,
  SafeText,
} from "../../components/common";

type SettingsTab = ConfigKind | "skills" | "plugins" | "diagnostics";
const tabs: { id: SettingsTab; label: string; description: string }[] = [
  { id: "models", label: "模型", description: "连接并使用你的模型" },
  { id: "agents", label: "Agent", description: "目标、工具与执行预算" },
  { id: "skills", label: "Skills", description: "可复用能力与固定版本" },
  {
    id: "skill_sources",
    label: "Skill 来源",
    description: "本地目录与加载范围",
  },
  { id: "mcp", label: "MCP", description: "连接、工具目录与授权" },
  { id: "policies", label: "权限策略", description: "请求范围与实际有效权限" },
  { id: "plugins", label: "插件", description: "静态声明、版本与排空状态" },
  { id: "diagnostics", label: "诊断", description: "服务健康与错误定位" },
];
const fields: Record<
  ConfigKind,
  { key: string; label: string; placeholder?: string }[]
> = {
  models: [],
  agents: [
    {
      key: "model_policy.profile_ref",
      label: "模型配置版本引用",
      placeholder: "demo@1",
    },
    { key: "system_prompt_ref", label: "当前行为说明引用" },
    { key: "system_prompt", label: "行为说明" },
  ],
  mcp: [
    { key: "display_name", label: "连接名称" },
    {
      key: "transport",
      label: "传输方式",
      placeholder: "stdio / streamable_http",
    },
    { key: "endpoint", label: "服务地址" },
    { key: "command", label: "stdio 可执行文件" },
    { key: "credential_ref", label: "凭据引用" },
  ],
  skill_sources: [
    { key: "root", label: "本地目录" },
    { key: "scope", label: "来源范围", placeholder: "user / project" },
  ],
  policies: [
    {
      key: "egress",
      label: "数据出站模式",
      placeholder: "local_only / cloud_allowed",
    },
  ],
};

const fieldValue = (value: ObjectValue, key: string) =>
  key
    .split(".")
    .reduce<unknown>((current, part) => object(current)[part], value);
const setField = (
  value: ObjectValue,
  key: string,
  text: string,
): ObjectValue => {
  const [head, tail] = key.split(".");
  return tail
    ? { ...value, [head]: { ...object(value[head]), [tail]: text } }
    : { ...value, [head]: text };
};

export function Settings({
  capabilities,
  onClose,
  onChanged,
}: {
  capabilities: ObjectValue;
  onClose: () => void;
  onChanged: () => void;
}) {
  const [tab, setTab] = useState<SettingsTab>("models");
  const [records, setRecords] = useState<ObjectValue[]>([]);
  const [selected, setSelected] = useState<ObjectValue | null>(null);
  const [draft, setDraft] = useState<ObjectValue>({});
  const [jsonDraft, setJsonDraft] = useState("{}");
  const [recordId, setRecordId] = useState("");
  const [error, setError] = useState<unknown>();
  const [receipt, setReceipt] = useState("");
  const [report, setReport] = useState<unknown>();
  const [pending, setPending] = useState(false);
  const [confirmSave, setConfirmSave] = useState(false);
  const [pluginDraft, setPluginDraft] = useState(
    '{\n  "id": "my-static-plugin",\n  "version": "1.0.0",\n  "harness_api": ">=1,<2",\n  "contributes": {}\n}',
  );
  const [pluginReview, setPluginReview] = useState<ObjectValue | null>(null);
  const recordKey = (record: ObjectValue) =>
    tab === "plugins"
      ? `${string(object(record.manifest).id)}@${string(object(record.manifest).version)}`
      : id(record);
  const isConfig = !["models", "skills", "plugins", "diagnostics"].includes(
    tab,
  );
  const load = async () => {
    const response =
      tab === "skills"
        ? await api.skills()
        : tab === "plugins"
          ? await api.plugins()
          : tab === "diagnostics"
            ? await api.diagnostics()
            : await api.config(tab as ConfigKind);
    if (tab === "diagnostics") setReport(response);
    else setRecords(items(response));
  };
  useEffect(() => {
    let active = true;
    setSelected(null);
    setRecordId("");
    setDraft({});
    setJsonDraft("{}");
    setRecords([]);
    setReport(null);
    setError(null);
    setReceipt("");
    setConfirmSave(false);
    if (tab === "models") {
      setPending(false);
      return () => {
        active = false;
      };
    }
    setPending(true);
    const call =
      tab === "skills"
        ? api.skills()
        : tab === "plugins"
          ? api.plugins()
          : tab === "diagnostics"
            ? api.diagnostics()
            : api.config(tab as ConfigKind);
    void call
      .then((response) => {
        if (active) {
          if (tab === "diagnostics") setReport(response);
          else setRecords(items(response));
        }
      })
      .catch((error) => {
        if (active) setError(error);
      })
      .finally(() => {
        if (active) setPending(false);
      });
    return () => {
      active = false;
    };
  }, [tab]);
  const update = (next: ObjectValue) => {
    setDraft(next);
    setJsonDraft(pretty(next));
    setConfirmSave(false);
  };
  async function select(record: ObjectValue) {
    setPending(true);
    setError(null);
    setReport(null);
    setReceipt("");
    setConfirmSave(false);
    try {
      const value =
        tab === "skills"
          ? await api.skill(id(record))
          : isConfig
            ? await api.configDetail(tab as ConfigKind, id(record))
            : record;
      setSelected(value);
      setRecordId(recordKey(value) || recordKey(record));
      const editable = { ...value };
      delete editable.revision;
      delete editable.id;
      delete editable.created_at;
      delete editable.updated_at;
      update(editable);
    } catch (error) {
      setError(error);
    } finally {
      setPending(false);
    }
  }
  async function save() {
    setPending(true);
    setError(null);
    try {
      const result = await api.saveConfig(
        tab as ConfigKind,
        recordId,
        new Command({
          ...draft,
          expected_revision: selected?.revision ?? null,
        }),
      );
      setSelected(result);
      setReceipt(`已保存 ${recordId} · revision ${number(result.revision)}`);
      setConfirmSave(false);
      await load();
      onChanged();
    } catch (error) {
      setError(error);
      if (error instanceof ApiError && error.status === 409) {
        setSelected(await api.configDetail(tab as ConfigKind, recordId));
        setReceipt(
          "配置已由其他请求更新。已读取最新版本，草稿保留；请核对差异后重新保存。",
        );
        setConfirmSave(false);
      }
    } finally {
      setPending(false);
    }
  }
  async function diagnostic(
    action: "model" | "mcp" | "catalog" | "authorize" | "refresh" | "toggle",
  ) {
    setPending(true);
    setError(null);
    setReceipt("");
    try {
      const value =
        action === "model"
          ? await api.testModel(
              recordId,
              new Command({
                tests: [
                  "transport",
                  "messages",
                  "capabilities",
                  "tool_calling",
                  "streaming",
                  "usage",
                ],
              }),
            )
          : action === "mcp"
            ? await api.diagnoseMcp(
                recordId,
                new Command({
                  levels: [
                    "transport",
                    "protocol",
                    "catalog",
                    "authentication",
                  ],
                }),
              )
            : action === "catalog"
              ? await api.mcpCatalog(recordId)
              : action === "authorize"
                ? await api.authorizeMcp(recordId, new Command({}))
                : action === "refresh"
                  ? await api.refreshSkills(new Command({ source_ids: [] }))
                  : await api.updateSkill(
                      recordId,
                      new Command({
                        enabled: !selected?.enabled,
                        expected_revision: selected?.revision,
                      }),
                    );
      setReport(value);
      if (action === "toggle")
        setSelected((previous) => ({ ...previous, ...value }));
      if (action === "toggle" || action === "refresh") {
        await load();
        onChanged();
      }
      setReceipt("服务端操作已返回，详见诊断结果与回执。");
    } catch (error) {
      setError(error);
    } finally {
      setPending(false);
    }
  }
  const authUrl = safeExternalUrl(
    object(report).authorization_url ?? object(report).url,
  );
  async function pluginAction(action: "load" | "drain") {
    setPending(true);
    setError(null);
    try {
      const manifest = object(selected?.manifest);
      const result =
        action === "load"
          ? await api.loadPlugin(new Command({ manifest: pluginReview }))
          : await api.drainPlugin(
              string(manifest.id),
              string(manifest.version),
              new Command({}),
            );
      setReport(result);
      setSelected(result);
      setRecordId(
        `${string(object(result.manifest).id)}@${string(object(result.manifest).version)}`,
      );
      setReceipt(
        action === "load"
          ? "声明已注册。新任务使用服务端验证后的贡献项。"
          : "排空请求已受理。新任务停止绑定此版本，已有任务保留固定引用。",
      );
      setPluginReview(null);
      await load();
      onChanged();
    } catch (error) {
      setError(error);
    } finally {
      setPending(false);
    }
  }
  return (
    <div className="settings">
      <header className="settings-header">
        <div>
          <div className="eyebrow">WORKBENCH SETTINGS</div>
          <h1>设置与诊断</h1>
        </div>
        <button disabled={pending} onClick={onClose}>
          返回工作台
        </button>
      </header>
      <div className="settings-layout">
        <nav className="settings-nav" aria-label="设置分类">
          {tabs.map((item) => (
            <button
              className={tab === item.id ? "active" : ""}
              key={item.id}
              onClick={() => setTab(item.id)}
              disabled={pending}
            >
              {item.label}
              <small>{item.description}</small>
            </button>
          ))}
        </nav>
        <main className="settings-content">
          {tab === "models" ? (
            <ModelSettings onChanged={onChanged} onBusyChange={setPending} />
          ) : (
            <>
              <div className="section-heading">
                <div>
                  <h2>{tabs.find((item) => item.id === tab)?.label}</h2>
                  <p>{tabs.find((item) => item.id === tab)?.description}</p>
                </div>
                <div className="actions">
                  {isConfig && (
                    <button
                      onClick={() => {
                        setSelected(null);
                        setRecordId("");
                        update({});
                        setReceipt("新建配置：先填写 ID 与正文，再审阅保存。");
                      }}
                    >
                      ＋ 新建配置
                    </button>
                  )}
                  {tab === "skills" && (
                    <button
                      disabled={pending}
                      onClick={() => void diagnostic("refresh")}
                    >
                      刷新来源
                    </button>
                  )}
                  <button
                    disabled={pending}
                    onClick={() => {
                      void load().catch(setError);
                    }}
                  >
                    刷新
                  </button>
                </div>
              </div>
              <ErrorNotice error={error} />
              {receipt && <Notice tone="success">{receipt}</Notice>}
              {pending && (
                <p role="status" className="muted">
                  正在请求服务端…
                </p>
              )}
              {records.length === 0 &&
                !isConfig &&
                tab !== "diagnostics" &&
                !pending && (
                  <Empty title="暂无记录">配置来源并刷新后查看。</Empty>
                )}
              <div className="settings-records">
                {records.map((record) => (
                  <button
                    className={`list-card ${recordId === recordKey(record) ? "selected" : ""}`}
                    key={recordKey(record) || string(record.name)}
                    onClick={() => void select(record)}
                    disabled={pending}
                  >
                    <strong>
                      {string(
                        record.display_name ?? record.name ?? record.title,
                        recordKey(record),
                      )}
                    </strong>
                    <small>
                      {recordKey(record)} · revision {number(record.revision)}
                      {record.enabled === false ? " · 已停用" : ""}
                      {tab === "plugins" ? ` · ${string(record.status)}` : ""}
                    </small>
                  </button>
                ))}
              </div>
              {isConfig && (
                <section className="config-editor">
                  <h3>{selected ? "编辑配置" : "新建配置"}</h3>
                  <label>
                    配置 ID
                    <input
                      value={recordId}
                      disabled={Boolean(selected)}
                      onChange={(event) => setRecordId(event.target.value)}
                      placeholder="使用可识别的唯一名称"
                    />
                  </label>
                  {fields[tab as ConfigKind].map((field) => (
                    <label key={field.key}>
                      {field.label}
                      {field.key === "system_prompt" ? (
                        <textarea
                          rows={4}
                          value={string(fieldValue(draft, field.key))}
                          onChange={(event) =>
                            update(
                              setField(draft, field.key, event.target.value),
                            )
                          }
                        />
                      ) : (
                        <input
                          value={string(fieldValue(draft, field.key))}
                          placeholder={field.placeholder}
                          onChange={(event) =>
                            update(
                              setField(draft, field.key, event.target.value),
                            )
                          }
                        />
                      )}
                    </label>
                  ))}
                  <details>
                    <summary>高级配置正文</summary>
                    <p className="muted">
                      保留 schema
                      中的其他字段；所有字段由服务端校验。凭据只允许使用引用。
                    </p>
                    <textarea
                      rows={12}
                      className="code-input"
                      value={jsonDraft}
                      onChange={(event) => {
                        setJsonDraft(event.target.value);
                        setConfirmSave(false);
                      }}
                    />
                    <button
                      onClick={() => {
                        try {
                          update(object(JSON.parse(jsonDraft)));
                          setError(null);
                        } catch {
                          setError(new Error("JSON 格式无效，请修正后应用。"));
                        }
                      }}
                    >
                      应用 JSON 到草稿
                    </button>
                  </details>
                  <div className="actions">
                    <button
                      className="primary"
                      disabled={pending || !recordId}
                      onClick={() => setConfirmSave(true)}
                    >
                      审阅更改
                    </button>
                    {tab === "mcp" && selected && (
                      <>
                        <button
                          disabled={pending}
                          onClick={() => void diagnostic("mcp")}
                        >
                          分层诊断
                        </button>
                        <button
                          disabled={pending}
                          onClick={() => void diagnostic("catalog")}
                        >
                          查看工具目录
                        </button>
                        {capabilities.mcp_oauth === true && (
                          <button
                            disabled={pending}
                            onClick={() => void diagnostic("authorize")}
                          >
                            开始授权
                          </button>
                        )}
                      </>
                    )}
                  </div>
                  {confirmSave && (
                    <div className="review-box">
                      <h4>确认配置更改</h4>
                      <p>
                        保存使用 revision{" "}
                        {selected ? number(selected.revision) : "不存在条件"}
                        。现有任务继续使用已固定的配置版本，权限撤销以服务端为准。
                      </p>
                      <div className="diff-columns">
                        <div>
                          <strong>当前服务端版本</strong>
                          <pre>{pretty(selected)}</pre>
                        </div>
                        <div>
                          <strong>待保存正文</strong>
                          <pre>{pretty(draft)}</pre>
                        </div>
                      </div>
                      <button
                        className="primary"
                        disabled={pending}
                        onClick={() => void save()}
                      >
                        保存此配置
                      </button>
                    </div>
                  )}
                  {selected && (
                    <JsonDetail value={selected} label="当前配置与有效权限" />
                  )}
                  {tab === "policies" && (
                    <Notice>
                      实际权限由当前策略与平台配置上界共同约束。平台默认关闭网络与进程执行；需要在本机配置的
                      permissions 中明确启用后，任务仍须遵循工具审批规则。
                    </Notice>
                  )}
                </section>
              )}
              {tab === "skills" && selected && (
                <section className="record">
                  <div className="section-heading">
                    <h3>{string(selected.name, recordId)}</h3>
                    <button
                      disabled={pending}
                      onClick={() => void diagnostic("toggle")}
                    >
                      {selected.enabled === false ? "启用" : "停用"}
                    </button>
                  </div>
                  <p className="muted">
                    来源 {string(selected.source_id)} · hash{" "}
                    {string(selected.content_hash ?? selected.hash)}
                  </p>
                  <SafeText text={string(selected.body ?? selected.content)} />
                  <JsonDetail value={selected} label="固定版本与验证诊断" />
                  <Notice>
                    刷新影响新任务的目录绑定。已运行任务保留原先激活的 Skill
                    快照。
                  </Notice>
                </section>
              )}
              {tab === "plugins" && (
                <section className="config-editor">
                  {selected && (
                    <>
                      <JsonDetail
                        value={selected}
                        label="插件注册状态、版本与诊断"
                      />
                      <button
                        disabled={
                          pending ||
                          selected.status === "draining" ||
                          selected.status === "drained" ||
                          selected.status === "disposed"
                        }
                        onClick={() => void pluginAction("drain")}
                      >
                        排空此版本
                      </button>
                    </>
                  )}
                  <h3>导入静态声明清单</h3>
                  <p className="muted">
                    支持 Skill 来源、MCP 连接配置与 Agent
                    模板。来源保持不可信，权限由任务策略决定。宿主拒绝任意可执行入口及未实现的扩展类别。
                  </p>
                  <label>
                    插件声明 JSON
                    <textarea
                      className="code-input"
                      rows={12}
                      value={pluginDraft}
                      disabled={pending}
                      onChange={(event) => {
                        setPluginDraft(event.target.value);
                        setPluginReview(null);
                      }}
                    />
                  </label>
                  <button
                    disabled={pending}
                    onClick={() => {
                      try {
                        const parsed: unknown = JSON.parse(pluginDraft);
                        if (
                          !parsed ||
                          typeof parsed !== "object" ||
                          Array.isArray(parsed)
                        )
                          throw new Error("清单必须是 JSON 对象。");
                        setPluginReview(object(parsed));
                        setError(null);
                      } catch (error) {
                        setError(error);
                      }
                    }}
                  >
                    审阅声明
                  </button>
                  {pluginReview && (
                    <div className="review-box">
                      <h4>确认导入声明</h4>
                      <pre>{pretty(pluginReview)}</pre>
                      <button
                        className="primary"
                        disabled={pending}
                        onClick={() => void pluginAction("load")}
                      >
                        导入此声明
                      </button>
                    </div>
                  )}
                </section>
              )}
              {report != null && (
                <section className="diagnostic-result">
                  <h3>诊断结果 / 服务端回执</h3>
                  {authUrl && (
                    <a
                      className="button primary"
                      href={authUrl}
                      target="_blank"
                      rel="noopener noreferrer"
                    >
                      在新窗口完成 MCP 授权
                    </a>
                  )}
                  <pre>{pretty(report)}</pre>
                  {tab === "mcp" && (
                    <p className="muted">
                      连接与目录检查不会执行工具。实际示例调用请返回工作台提交任务，以便记录审批和操作账本。
                    </p>
                  )}
                </section>
              )}
            </>
          )}
        </main>
      </div>
    </div>
  );
}
