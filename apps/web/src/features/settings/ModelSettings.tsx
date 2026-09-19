import { useEffect, useRef, useState } from "react";
import {
  api,
  ApiError,
  type ModelChoice,
  type ModelConnectionTest,
  type ModelProvider,
} from "../../api/client";
import {
  id,
  items,
  number,
  object,
  string,
  type ObjectValue,
} from "../../api/values";
import { ErrorNotice, Notice, safeExternalUrl } from "../../components/common";
import {
  canReuseModelKey,
  configuredContextWindow,
  editModelDraft,
  emptyModelDraft,
  mergeModelChoices,
  modelDraftError,
  modelSetupBody,
  modelVerificationState,
  modelCheckLabel,
  switchModelProvider,
  type ModelDraft,
} from "./model-setup-state";

export function ModelSettings({
  onChanged,
  onBusyChange,
}: {
  onChanged: () => void;
  onBusyChange: (busy: boolean) => void;
}) {
  const [providers, setProviders] = useState<ModelProvider[]>([]);
  const [profiles, setProfiles] = useState<ObjectValue[]>([]);
  const [activeRef, setActiveRef] = useState("");
  const [draft, setDraft] = useState<ModelDraft>(emptyModelDraft);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<unknown>();
  const [error, setError] = useState<unknown>();
  const [pending, setPending] = useState<"discover" | "test" | "save" | null>(
    null,
  );
  const [discovered, setDiscovered] = useState<ModelChoice[]>([]);
  const [discoveryNote, setDiscoveryNote] = useState("");
  const [test, setTest] = useState<ModelConnectionTest | null>(null);
  const [receipt, setReceipt] = useState("");
  const [acceptLimited, setAcceptLimited] = useState(false);
  const verification = modelVerificationState(test);
  const [conflict, setConflict] = useState<ObjectValue | null>(null);
  const [loadAttempt, setLoadAttempt] = useState(0);
  const actionController = useRef<AbortController | null>(null);
  const mounted = useRef(true);
  const provider = providers.find(
    (provider) => provider.id === draft.providerId,
  );
  const choices = mergeModelChoices(provider?.models ?? [], discovered);
  const busy = loading || pending !== null;
  const hasStoredKey = canReuseModelKey(draft);
  const documentation = safeExternalUrl(provider?.documentation_url);

  useEffect(() => {
    onBusyChange(busy);
  }, [busy, onBusyChange]);
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
      actionController.current?.abort();
      onBusyChange(false);
    };
  }, [onBusyChange]);
  useEffect(() => {
    let current = true;
    setLoading(true);
    setLoadError(null);
    void Promise.all([
      api.modelProviders(),
      api.config("models"),
      api.config("agents"),
    ])
      .then(([catalog, models, agents]) => {
        if (!current) return;
        setProviders(catalog.items);
        setProfiles(items(models));
        const defaultAgent = items(agents).find(
          (agent) => id(agent) === "default",
        );
        setActiveRef(string(object(defaultAgent?.model_policy).profile_ref));
      })
      .catch((error) => {
        if (current) setLoadError(error);
      })
      .finally(() => {
        if (current) setLoading(false);
      });
    return () => {
      current = false;
    };
  }, [loadAttempt]);

  function clearResult() {
    setError(null);
    setTest(null);
    setAcceptLimited(false);
    setReceipt("");
    setConflict(null);
  }
  function chooseProvider(providerId: string) {
    setDraft((previous) => switchModelProvider(previous, providerId));
    setDiscovered([]);
    setDiscoveryNote("");
    clearResult();
  }
  function newModel() {
    setDraft(emptyModelDraft());
    setDiscovered([]);
    setDiscoveryNote("");
    clearResult();
  }
  function editProfile(profile: ObjectValue) {
    if (profile.adapter_id === "demo") {
      newModel();
      setReceipt("当前使用本地演示。选择下方提供方，即可添加并使用真实模型。");
      return;
    }
    setDraft(editModelDraft(profile, providers));
    setDiscovered([]);
    setDiscoveryNote("");
    clearResult();
  }
  function changeConnection(next: Partial<ModelDraft>) {
    setDraft((previous) => ({
      ...previous,
      ...next,
      ...("baseUrl" in next &&
      next.baseUrl !== previous.baseUrl &&
      previous.savedKey
        ? { savedKey: false, needsFreshKey: true }
        : {}),
    }));
    setTest(null);
    setAcceptLimited(false);
    setError(null);
    setReceipt("");
    if ("apiKey" in next || "baseUrl" in next) {
      setDiscovered([]);
      setDiscoveryNote("");
    }
  }
  async function reloadProfiles() {
    const [models, agents] = await Promise.all([
      api.config("models"),
      api.config("agents"),
    ]);
    if (!mounted.current) return;
    setProfiles(items(models));
    setActiveRef(
      string(
        object(
          items(agents).find((agent) => id(agent) === "default")?.model_policy,
        ).profile_ref,
      ),
    );
  }
  async function act(
    action: "discover" | "test" | "save",
    options: {
      activate?: boolean;
      probe_mode?: "agent" | "connectivity";
      optional_checks?: ("vision" | "streaming")[];
    } = {},
  ) {
    const invalid = modelDraftError(draft, provider, action !== "discover");
    if (invalid) {
      setError(new Error(invalid));
      return;
    }
    setPending(action);
    setError(null);
    setReceipt("");

    const controller = new AbortController();
    actionController.current = controller;
    try {
      if (action === "discover") {
        const result = await api.discoverModels(
          modelSetupBody(draft, false),
          controller.signal,
        );
        if (!mounted.current) return;
        setDiscovered(result.items);
        setDiscoveryNote(
          result.source === "official_catalog"
            ? "已载入此提供方公布的模型目录。也可以输入其他模型名称。"
            : `已获取 ${result.items.length} 个模型。目录中没有的模型可直接输入名称。`,
        );
      } else if (action === "test") {
        setTest(null);
        setAcceptLimited(false);
        const result = await api.testModelSetup(
          { ...modelSetupBody(draft), ...options, refresh_verification: true },
          controller.signal,
        );
        if (mounted.current) setTest(result);
      } else {
        let verified = test;
        if (
          options.activate !== false &&
          !modelVerificationState(verified).ready
        ) {
          setAcceptLimited(false);
          verified = await api.testModelSetup(
            { ...modelSetupBody(draft), refresh_verification: true },
            controller.signal,
          );
          if (!mounted.current) return;
          setTest(verified);
        }
        const state = modelVerificationState(verified);
        if (
          options.activate !== false &&
          (!state.ready || (state.limited && !acceptLimited))
        ) {
          setError(
            new Error(
              state.ready
                ? "部分能力未通过，尚未保存。可以重新检测，或选择下方的“仅启用已验证能力”。"
                : "尚未通过文本与工具协议验证，无法保存并使用。",
            ),
          );
          return;
        }
        const result = await api.saveModelSetup(
          {
            ...modelSetupBody(draft, true, true),
            verification_token:
              options.activate === false
                ? undefined
                : verified?.verification_token,
            accept_unverified_capabilities:
              state.limited && acceptLimited ? state.missing : [],
            ...options,
          },
          controller.signal,
        );
        if (!mounted.current) return;
        setDraft(emptyModelDraft());
        setTest(null);
        setAcceptLimited(false);
        setDiscovered([]);
        setDiscoveryNote("");
        setConflict(null);
        setProfiles((previous) => [
          ...previous.filter((profile) => id(profile) !== result.id),
          { ...result.profile, id: result.id },
        ]);
        if (result.active) setActiveRef(`${result.id}@${result.revision}`);
        setReceipt(
          result.active
            ? "模型已保存并使用；现有会话的明确选择保持不变。"
            : "已保存未验证草稿；验证通过前不能用于任务。",
        );
        onChanged();
        try {
          await reloadProfiles();
        } catch {
          setError(
            new Error(
              "模型已经保存，但列表刷新失败。请点击“刷新列表”核对当前使用状态。",
            ),
          );
        }
      }
    } catch (error) {
      if (!mounted.current || controller.signal.aborted) return;
      setError(error);
      if (
        action === "save" &&
        error instanceof ApiError &&
        error.code === "REVISION_CONFLICT" &&
        draft.profileId
      ) {
        try {
          const latest = await api.configDetail("models", draft.profileId);
          if (mounted.current) setConflict(latest);
        } catch {
          /* Keep the original conflict and the draft. */
        }
      }
    } finally {
      if (mounted.current) setPending(null);
      actionController.current = null;
    }
  }

  if (loading)
    return (
      <section className="model-loading" aria-busy="true">
        <span className="model-loading-dot" />
        <h2>模型设置</h2>
        <p role="status">正在读取提供方和已保存的模型…</p>
      </section>
    );
  if (loadError)
    return (
      <section className="model-loading">
        <h2>模型设置暂时无法读取</h2>
        <ErrorNotice error={loadError} />
        <button onClick={() => setLoadAttempt((value) => value + 1)}>
          重新加载
        </button>
      </section>
    );

  return (
    <section className="model-settings">
      <div className="section-heading">
        <div>
          <h2>模型设置</h2>
          <p>选择提供方，连接你希望使用的模型。</p>
        </div>
        <button
          disabled={busy}
          onClick={() => {
            setError(null);
            void reloadProfiles().catch(setError);
          }}
        >
          刷新列表
        </button>
      </div>
      {profiles.length > 0 && (
        <div className="model-profile-list" aria-label="已保存的模型">
          {profiles.map((profile) => {
            const identity = id(profile);
            const providerName =
              profile.adapter_id === "demo"
                ? "本地演示"
                : (providers.find(
                    (provider) =>
                      provider.id ===
                      editModelDraft(profile, providers).providerId,
                  )?.name ?? "自定义服务");
            const active =
              typeof profile.active === "boolean"
                ? profile.active
                : activeRef === `${identity}@${number(profile.revision)}`;
            return (
              <button
                key={identity}
                className={`model-profile ${draft.profileId === identity ? "selected" : ""}`}
                disabled={busy}
                onClick={() => editProfile(profile)}
              >
                <span className="model-provider-mark" aria-hidden="true">
                  {providerName.slice(0, 1).toUpperCase()}
                </span>
                <span>
                  <strong>{string(profile.model_id, providerName)}</strong>
                  <small>
                    {providerName}
                    {profile.adapter_id === "demo" ? " · 确定性演示" : ""}
                    {` · ${configuredContextWindow(profile) === 1000000 ? "1M" : "300K"} 上下文`}
                  </small>
                </span>
                {active ? (
                  <span className="model-active-badge">当前使用</span>
                ) : (
                  <span className="model-edit-label">编辑</span>
                )}
              </button>
            );
          })}
        </div>
      )}
      {receipt && <Notice tone="success">{receipt}</Notice>}
      <form
        className="model-setup-card"
        autoComplete="off"
        onSubmit={(event) => {
          event.preventDefault();
          void act("save");
        }}
      >
        <div className="model-card-heading">
          <div>
            <span className="eyebrow">MODEL CONNECTION</span>
            <h3>{draft.profileId ? "编辑模型" : "添加模型"}</h3>
          </div>
          {draft.profileId && (
            <button
              type="button"
              className="text-button"
              disabled={busy}
              onClick={newModel}
            >
              ＋ 添加其他模型
            </button>
          )}
        </div>
        <div className="model-field">
          <label htmlFor="model-provider">提供方</label>
          <select
            id="model-provider"
            value={draft.providerId}
            disabled={busy}
            onChange={(event) => chooseProvider(event.target.value)}
          >
            <option value="">请选择提供方</option>
            {providers.map((provider) => (
              <option value={provider.id} key={provider.id}>
                {provider.name}
              </option>
            ))}
          </select>
        </div>
        {draft.legacyEndpoint && (
          <Notice>
            此旧配置使用自定义服务地址。请核对地址，并重新填写该服务的 API Key
            后保存。
          </Notice>
        )}
        {draft.providerId === "custom" && (
          <div className="model-field">
            <label htmlFor="model-base-url">服务地址</label>
            <input
              id="model-base-url"
              type="url"
              value={draft.baseUrl}
              disabled={busy}
              placeholder="https://your-service.example/v1"
              onChange={(event) =>
                changeConnection({ baseUrl: event.target.value })
              }
            />
          </div>
        )}
        <div className="model-field">
          <div className="model-field-heading">
            <label
              htmlFor={
                draft.providerId === "ollama" ? undefined : "model-api-key"
              }
            >
              {draft.providerId === "ollama" ? "本地连接" : "API Key"}
            </label>
            {documentation && (
              <a href={documentation} target="_blank" rel="noopener noreferrer">
                查看提供方文档 ↗
              </a>
            )}
          </div>
          <div className="model-key-row">
            {draft.providerId === "ollama" ? (
              <p className="model-local-note">
                Ollama 在本机运行，无需 API Key。
              </p>
            ) : (
              <input
                id="model-api-key"
                type="password"
                autoComplete="new-password"
                spellCheck={false}
                autoCapitalize="none"
                value={draft.apiKey}
                disabled={busy || !provider}
                placeholder={
                  hasStoredKey
                    ? "已保存密钥，留空继续使用"
                    : provider?.requires_api_key === false
                      ? "可选：此服务需要认证时填写"
                      : "请输入 API Key"
                }
                onChange={(event) =>
                  changeConnection({ apiKey: event.target.value })
                }
              />
            )}
            <button
              type="button"
              disabled={busy || !provider}
              onClick={() => void act("test")}
            >
              {pending === "test"
                ? "正在验证…"
                : test
                  ? "重新验证全部能力"
                  : "验证模型能力（含图片）"}
            </button>
          </div>
          {draft.providerId !== "ollama" && (
            <small>
              {hasStoredKey
                ? "已保存的密钥不会回显。填写新密钥会替换原密钥。"
                : "API Key 通过本机服务保存到系统凭据库。"}
            </small>
          )}
        </div>
        <div className="model-field">
          <div className="model-field-heading">
            <label htmlFor="model-name">模型名称</label>
            <button
              type="button"
              className="text-button"
              disabled={busy || !provider}
              onClick={() => void act("discover")}
            >
              {pending === "discover" ? "正在获取…" : "刷新模型列表"}
            </button>
          </div>
          <div className="model-combobox">
            <input
              id="model-name"
              list="model-choices"
              autoComplete="off"
              value={draft.modelId}
              disabled={busy || !provider}
              placeholder="选择或输入模型名称"
              onChange={(event) =>
                changeConnection({ modelId: event.target.value })
              }
            />
          </div>
          <datalist id="model-choices">
            {choices.map((model) => (
              <option value={model.id} key={model.id}>
                {model.name}
              </option>
            ))}
          </datalist>
          <small>
            {discoveryNote ||
              "可从目录选择，也可输入提供方支持的自定义模型 ID。"}
          </small>
        </div>
        <div className="model-field">
          <label htmlFor="model-context-window">上下文窗口</label>
          <select
            id="model-context-window"
            value={draft.contextWindow}
            disabled={busy || !provider}
            onChange={(event) =>
              changeConnection({
                contextWindow:
                  event.target.value === "1000000" ? 1000000 : 300000,
              })
            }
          >
            <option value={300000}>300K（默认）</option>
            <option value={1000000}>1M（模型明确支持时开启）</option>
          </select>
          <small>
            所有模型默认使用 300K。只有提供方文档明确说明当前模型支持 1M
            时才开启；能力验证不会发送百万 token 探测。
          </small>
        </div>
        {test && (
          <div
            className={`model-test-result ${verification.ready && !verification.limited ? "success" : "failure"}`}
            role="status"
          >
            <div>
              <strong>
                {test.probe_mode === "connectivity"
                  ? "快速连接检测结果（不能代替完整验证）"
                  : verification.ready
                    ? verification.limited
                      ? "基础能力通过，部分能力待验证"
                      : "模型完整验证通过"
                    : "基础能力验证未通过"}
              </strong>
              <ul>
                {(
                  [
                    ["transport", "连接"],
                    ["text", "文本"],
                    ["tool_calling", "工具调用"],
                    ["tool_pairing", "工具结果续接"],
                    ["vision", "图片"],
                    ["streaming", "流式输出"],
                    ["usage", "用量统计"],
                  ] as const
                ).map(([key, label]) => (
                  <li key={key}>
                    {label}：{modelCheckLabel(test, key)}
                  </li>
                ))}
              </ul>
              <p>
                {test.reused
                  ? "复用已有结果"
                  : `本次 ${test.request_count ?? 0} 次请求`}
                ； 实测 {((test.duration_ms ?? 0) / 1000).toFixed(1)} 秒。
                {test.probe_mode === "connectivity"
                  ? "保存时仍需执行完整验证。"
                  : "点击重新验证会重新检测；保存可复用 10 分钟内的匹配结果。"}
              </p>
              {verification.limited && (
                <p>
                  未通过不等于模型不支持；可能是网络、超时或响应不符合要求。未验证能力不会启用，原配置在保存前保持不变。
                </p>
              )}
            </div>
          </div>
        )}
        {verification.limited && (
          <label className="model-limited-choice">
            <input
              type="checkbox"
              checked={acceptLimited}
              disabled={busy}
              onChange={(event) => setAcceptLimited(event.target.checked)}
            />
            仅启用已验证能力（不启用
            {verification.missing
              .map((name) => (name === "vision" ? "图片" : "流式输出"))
              .join("、")}
            ；编辑时将替换旧版本的能力配置）
          </label>
        )}
        <ErrorNotice error={error} />
        {conflict && (
          <div className="model-conflict" role="status">
            <strong>服务端已有较新的设置</strong>
            <p>
              当前模型：{string(conflict.model_id)}
              。你的输入仍保留，请先载入新版本再编辑。
            </p>
            <button
              type="button"
              disabled={busy}
              onClick={() => editProfile(conflict)}
            >
              载入最新设置
            </button>
          </div>
        )}
        <div className="model-extra-actions">
          <button
            type="button"
            disabled={busy || !provider}
            onClick={() => void act("test", { probe_mode: "connectivity" })}
          >
            快速连接检测
          </button>
          <button
            type="button"
            disabled={busy || !provider}
            onClick={() => void act("save", { activate: false })}
          >
            仅保存未验证草稿
          </button>
          {pending && pending !== "save" && (
            <button
              type="button"
              onClick={() => actionController.current?.abort()}
            >
              中止检测
            </button>
          )}
        </div>
        <footer className="model-setup-footer">
          <p>
            完整验证包含文本、工具、图片和流式输出；仅通过的能力会启用。保存后，请在已有会话的模型菜单中选择新版本。
          </p>
          <div>
            <button type="button" disabled={busy} onClick={newModel}>
              取消
            </button>
            <button
              className="primary"
              type="submit"
              disabled={busy || !provider}
            >
              {pending === "save"
                ? verification.ready
                  ? "正在本地保存…"
                  : "正在验证并保存…"
                : verification.limited
                  ? "保存已验证能力"
                  : "保存并使用"}
            </button>
          </div>
        </footer>
      </form>
    </section>
  );
}
