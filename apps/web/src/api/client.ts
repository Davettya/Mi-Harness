import { object, string, type ObjectValue } from "./values";
import type {
  ArtifactRef,
  AuthSessionView,
  RunView,
  SubmitReceipt,
  SubmitRunInput,
  InteractionResponse,
  SessionInput,
} from "./types";

export class ApiError extends Error {
  constructor(
    public status: number,
    public code: string,
    message: string,
    public correlationId = "",
  ) {
    super(message);
  }
}

export interface ModelChoice {
  id: string;
  name: string;
}
export interface ModelProvider {
  id: string;
  name: string;
  requires_api_key: boolean;
  default_base_url?: string;
  allow_custom_base_url?: boolean;
  models: ModelChoice[];
  documentation_url?: string;
}
export interface ModelSetupInput {
  provider_id: string;
  api_key?: string;
  profile_id?: string;
  base_url?: string;
  model_id?: string;
  context_window?: 300000 | 1000000;
  expected_revision?: number;
  verification_token?: string;
  activate?: boolean;
  probe_mode?: "agent" | "connectivity";
  optional_checks?: ("vision" | "streaming")[];
  refresh_verification?: boolean;
  accept_unverified_capabilities?: ("vision" | "streaming")[];
}
export interface ModelDiscovery {
  items: ModelChoice[];
  source: string;
  message?: string;
}
export interface ModelConnectionTest {
  connected: boolean;
  tool_calling: boolean;
  verification_token?: string;
  reused?: boolean;
  checks?: Record<string, boolean>;
  check_failures?: Record<string, string>;
  probe_mode?: "agent" | "connectivity";
  request_count?: number;
  expires_in?: number;
  duration_ms?: number;
  optional_checks?: ("vision" | "streaming")[];
  message?: string;
}
export interface ModelSetupResult {
  profile: ObjectValue;
  id: string;
  revision: number;
  active: boolean;
  message?: string;
}

const setupErrors: Record<string, string> = {
  MODEL_CAPABILITIES_INCOMPLETE:
    "部分能力未通过，原配置未修改。请完整检测后重试，或选择仅启用已验证能力。",
  MODEL_FULL_VERIFICATION_REQUIRED:
    "快速连接检测不能用于激活，请执行完整验证。",
  MODEL_VERIFIED_DRAFT:
    "已验证模型不能被草稿覆盖。请添加独立模型草稿，或验证后保存。",
  MODEL_VERIFICATION_EXPIRED: "验证已过期或配置已变化，请重新验证。",
  MODEL_BASE_URL_INVALID: "自定义服务地址无效，请填写完整的 HTTP(S) URL。",
  MODEL_DEMO_READONLY: "本地演示配置无需编辑。请添加一个新的模型连接。",
  MODEL_PROVIDER_ENDPOINT: "此提供方使用固定服务地址，请重新选择提供方。",
  MODEL_PROFILE_INVALID: "此模型配置不可用，请刷新列表后重新选择。",
  MODEL_NAME_REQUIRED: "请选择或输入模型名称。",
  MODEL_KEY_INVALID: "API Key 格式无效，请检查是否包含多余空格或换行。",
  MODEL_KEY_REQUIRED: "请填写此提供方的 API Key。",
  MODEL_SETUP_RATE_LIMIT: "连接操作过于频繁，请稍后再试。",
  MODEL_AUTH_FAILED: "提供方未接受此 API Key，请核对密钥及其权限。",
  MODEL_NOT_AVAILABLE: "此模型不可用或当前账号无权访问，请选择其他模型。",
  MODEL_PROVIDER_RATE_LIMIT: "提供方限制了请求频率，请稍后再试。",
  MODEL_CONNECTION_TIMEOUT: "连接提供方超时，请检查网络后重试。",
  MODEL_CONNECTION_FAILED: "未能连接提供方，请检查服务地址和网络状态。",
  MODEL_REDIRECT_DENIED: "此服务要求跳转到其他地址，请填写实际服务地址。",
  MODEL_CATALOG_TOO_LARGE: "提供方返回的目录过大，请直接输入模型名称。",
  MODEL_TOOL_UNAVAILABLE: "模型未通过工具调用验证，请选择支持工具调用的模型。",
  MODEL_CREDENTIAL_STORAGE_FAILED:
    "密钥无法保存到系统凭据库，请检查本机凭据服务后重试。",
  MAINTENANCE: "服务正在维护，请稍后保存。",
  MODEL_PROFILE_PINNED:
    "启动配置固定了默认模型，请移除 model_profile_ref 固定项并重启后再保存。",
  SSRF_DENIED: "当前出站策略不允许此服务地址，请检查地址与本机策略。",
  REVISION_CONFLICT: "此模型配置已发生变化。请核对最新设置后重新保存。",
  IDEMPOTENCY_CONFLICT: "此保存请求与先前请求不一致，请重新核对设置。",
  CREDENTIAL_REQUIRED: "请填写此提供方的 API Key。",
  API_KEY_REQUIRED: "请填写此提供方的 API Key。",
  MODEL_SETUP_INVALID: "请检查提供方、模型名称及服务地址。",
  VALIDATION_ERROR: "请检查必填项与服务地址格式。",
  MODEL_PROVIDER_UNKNOWN: "该提供方尚不受支持，请选择列表中的提供方。",
  PROVIDER_NOT_FOUND: "该提供方尚不受支持，请选择列表中的提供方。",
  CREDENTIAL_UNAVAILABLE: "无法读取已保存的密钥，请重新填写 API Key。",
  MODEL_PROBE_FAILED: "连接验证未通过，请检查 API Key、模型名称与服务可用性。",
  MODEL_SETUP_TEST_FAILED:
    "连接验证未通过，请检查 API Key、模型名称与服务可用性。",
  NETWORK_DENIED: "当前本机配置未允许网络访问，请检查平台权限设置。",
  EGRESS_DENIED: "当前策略未允许连接此提供方，请检查出站权限设置。",
};

/** Secrets exist only in this single request. Never use Command or retry caches here. */
async function modelSetupRequest<T>(
  action: "discover" | "test" | "save",
  body: ModelSetupInput,
  signal?: AbortSignal,
): Promise<T> {
  const headers = new Headers({ "Content-Type": "application/json" });
  const token = csrfToken || cookieToken();
  if (token) headers.set("X-CSRF-Token", token);
  if (action === "save") headers.set("Idempotency-Key", crypto.randomUUID());
  let response: Response;
  try {
    response = await fetch(`/api/model-setup/${action}`, {
      method: "POST",
      credentials: "same-origin",
      headers,
      signal,
      body: JSON.stringify(body),
    });
  } catch (error) {
    if (signal?.aborted) throw new DOMException("操作已取消", "AbortError");
    // Never expose transport exception strings, which may contain request material.
    throw new Error(
      action === "save"
        ? "未能确认保存结果，请刷新模型列表核对后再操作。"
        : "连接请求未完成，请检查网络后重试。",
    );
  }
  if (!response.ok) {
    const detail = object(await response.json().catch(() => ({})));
    const code =
      typeof detail.code === "string" && Object.hasOwn(setupErrors, detail.code)
        ? detail.code
        : "MODEL_SETUP_ERROR";
    const message =
      setupErrors[code] ??
      (response.status === 401
        ? "配对已失效，请重新连接工作台。"
        : response.status === 409
          ? setupErrors.REVISION_CONFLICT
          : response.status === 403
            ? "当前权限不允许此连接，请检查本机网络与凭据设置。"
            : response.status === 429
              ? "请求过于频繁，请稍后重试。"
              : "操作未完成，请检查密钥、模型名称与提供方服务状态。");
    throw new ApiError(response.status, code, message);
  }
  try {
    return (await response.json()) as T;
  } catch {
    throw new Error("服务返回的数据无法读取，请刷新列表核对后再操作。");
  }
}

/** Stored only in memory. An identical retry retains its original body and key. */
export class Command<T = unknown> {
  readonly key = crypto.randomUUID();
  readonly serializedBody: string;
  constructor(readonly body: T) {
    this.serializedBody = JSON.stringify(body);
  }
}

let csrfToken = "";
const unresolvedCommands = new Map<string, string>();
const unresolvedUploads = new Map<string, string>();
function cookieToken(): string {
  const entry = document.cookie
    .split("; ")
    .find((value) => value.startsWith("harness_csrf="));
  return entry
    ? decodeURIComponent(entry.substring(entry.indexOf("=") + 1))
    : "";
}

export async function request<T = unknown>(
  path: string,
  options: {
    method?: string;
    command?: Command;
    signal?: AbortSignal;
    headers?: HeadersInit;
    form?: FormData;
  } = {},
): Promise<T> {
  const headers = new Headers(options.headers);
  const token = csrfToken || cookieToken();
  if (token) headers.set("X-CSRF-Token", token);
  if (options.command) {
    headers.set("Idempotency-Key", options.command.key);
    headers.set("Content-Type", "application/json");
  }
  const commandIdentity =
    options.command && path !== "/api/auth/exchange"
      ? `${options.method ?? "GET"} ${path} ${options.command.serializedBody}`
      : undefined;
  if (commandIdentity) {
    headers.set(
      "Idempotency-Key",
      unresolvedCommands.get(commandIdentity) ?? options.command!.key,
    );
    unresolvedCommands.set(commandIdentity, headers.get("Idempotency-Key")!);
  }
  const response = await fetch(path, {
    method: options.method ?? "GET",
    credentials: "same-origin",
    headers,
    signal: options.signal,
    body: options.form ?? options.command?.serializedBody,
  });
  const refreshed = response.headers.get("X-CSRF-Token");
  if (refreshed) csrfToken = refreshed;
  if (!response.ok) {
    const error = object(await response.json().catch(() => ({})));
    if (
      commandIdentity &&
      response.status < 500 &&
      response.status !== 408 &&
      response.status !== 429
    )
      unresolvedCommands.delete(commandIdentity);
    throw new ApiError(
      response.status,
      string(error.code, "HTTP_ERROR"),
      string(error.message, `请求失败 (${response.status})`),
      string(error.correlation_id),
    );
  }
  if (commandIdentity) unresolvedCommands.delete(commandIdentity);
  if (response.status === 204) return undefined as T;
  return response.json() as Promise<T>;
}

const resource = (value: string) => encodeURIComponent(value);
const mutate = (path: string, method: string, command: Command) =>
  request<ObjectValue>(path, { method, command });
export type ConfigKind =
  | "models"
  | "agents"
  | "mcp"
  | "skill_sources"
  | "policies";

/** All routes live here. UI components never construct API URLs. */
export const api = {
  availableModels: (session?: string) =>
    request<ObjectValue>(
      `/api/models/available${session ? `?session_id=${resource(session)}` : ""}`,
    ),
  preferences: (session: string) =>
    request<ObjectValue>(`/api/sessions/${resource(session)}/preferences`),
  savePreferences: (session: string, command: Command) =>
    mutate(`/api/sessions/${resource(session)}/preferences`, "PATCH", command),
  selectModel: (run: string, command: Command) =>
    mutate(`/api/runs/${resource(run)}/model-selection`, "POST", command),
  mcpFile: () => request<ObjectValue>("/api/mcp-config/file"),
  saveMcpFile: (command: Command) =>
    mutate("/api/mcp-config/file", "PUT", command),
  validateMcpFile: (text: string) =>
    request<ObjectValue>("/api/mcp-config/validate", {
      method: "POST",
      command: new Command({ text }),
    }),
  reloadMcpFile: () =>
    mutate("/api/mcp-config/reload", "POST", new Command({})),
  revokeMcp: (server: string) =>
    mutate(`/api/mcp/${resource(server)}/revoke`, "POST", new Command({})),
  authSession: () => request<AuthSessionView>("/api/auth/session"),
  localPair: () => request("/api/auth/local", { method: "POST" }),
  exchange: (ticket: string) =>
    request("/api/auth/exchange", {
      method: "POST",
      command: new Command({ ticket }),
    }),
  logout: () => mutate("/api/auth/logout", "POST", new Command({})),
  workspaces: () => allPages("/api/projects"),
  createWorkspace: (command: Command<{ name: string; roots: string[] }>) =>
    mutate("/api/projects", "POST", command),
  updateProject: (project: string, command: Command) =>
    mutate(`/api/projects/${resource(project)}`, "PATCH", command),
  removeProject: (
    project: string,
    command: Command<{ expected_revision: number }>,
  ) => mutate(`/api/projects/${resource(project)}`, "DELETE", command),
  chooseFolder: () =>
    request<{ path: string | null; cancelled: boolean }>(
      "/api/local/folder-picker",
      { method: "POST" },
    ),
  sessions: (workspace: string, archived = false) =>
    allPages(
      `/api/sessions?workspace_id=${resource(workspace)}&archived=${archived}`,
    ),
  createSession: (command: Command<SessionInput>) =>
    mutate("/api/sessions", "POST", command),
  session: (session: string) =>
    request<ObjectValue>(`/api/sessions/${resource(session)}`),
  updateSession: (session: string, command: Command) =>
    mutate(`/api/sessions/${resource(session)}`, "PATCH", command),
  submit: (session: string, command: Command<SubmitRunInput>) =>
    request<SubmitReceipt>(`/api/sessions/${resource(session)}/runs`, {
      method: "POST",
      command,
    }),
  snapshot: (run: string, signal?: AbortSignal) =>
    request<RunView>(`/api/runs/${resource(run)}?view=snapshot`, { signal }),
  eventUrl: (run: string) => `/api/runs/${resource(run)}/events`,
  cancel: (run: string, command: Command) =>
    mutate(`/api/runs/${resource(run)}/cancel`, "POST", command),
  steer: (run: string, command: Command) =>
    mutate(`/api/runs/${resource(run)}/input-commands`, "POST", command),
  branch: (session: string, command: Command) =>
    mutate(`/api/sessions/${resource(session)}/branches`, "POST", command),
  operation: (operation: string) =>
    request<ObjectValue>(`/api/operations/${resource(operation)}`),
  reconcile: (operation: string, command: Command) =>
    mutate(`/api/operations/${resource(operation)}/reconcile`, "POST", command),
  interaction: (interaction: string) =>
    request<ObjectValue>(`/api/interactions/${resource(interaction)}`),
  respond: (interaction: string, command: Command<InteractionResponse>) =>
    mutate(
      `/api/interactions/${resource(interaction)}/respond`,
      "POST",
      command,
    ),
  context: (run: string) =>
    request<ObjectValue>(`/api/runs/${resource(run)}/context`),
  pinContext: (run: string, command: Command) =>
    mutate(`/api/runs/${resource(run)}/context/pins`, "POST", command),
  agents: () => request("/api/agents"),
  config: (kind: ConfigKind) => request(`/api/config/${kind}`),
  configDetail: (kind: ConfigKind, configId: string) =>
    request<ObjectValue>(`/api/config/${kind}/${resource(configId)}`),
  saveConfig: (kind: ConfigKind, configId: string, command: Command) =>
    mutate(`/api/config/${kind}/${resource(configId)}`, "PUT", command),
  modelProviders: () =>
    request<{ items: ModelProvider[] }>("/api/model-providers"),
  discoverModels: (body: ModelSetupInput, signal?: AbortSignal) =>
    modelSetupRequest<ModelDiscovery>("discover", body, signal),
  testModelSetup: (body: ModelSetupInput, signal?: AbortSignal) =>
    modelSetupRequest<ModelConnectionTest>("test", body, signal),
  saveModelSetup: (body: ModelSetupInput, signal?: AbortSignal) =>
    modelSetupRequest<ModelSetupResult>("save", body, signal),
  testModel: (model: string, command: Command) =>
    mutate(`/api/models/${resource(model)}/test`, "POST", command),
  skills: () => request("/api/skills"),
  skill: (skill: string) =>
    request<ObjectValue>(`/api/skills/${resource(skill)}`),
  refreshSkills: (command: Command) =>
    mutate("/api/skills/refresh", "POST", command),
  updateSkill: (skill: string, command: Command) =>
    mutate(`/api/skills/${resource(skill)}`, "PATCH", command),
  plugins: () => request("/api/plugins"),
  loadPlugin: (command: Command) => mutate("/api/plugins", "POST", command),
  drainPlugin: (plugin: string, version: string, command: Command) =>
    mutate(
      `/api/plugins/${resource(plugin)}/versions/${resource(version)}/drain`,
      "POST",
      command,
    ),
  mcpCatalog: (server: string) =>
    request<ObjectValue>(`/api/mcp/${resource(server)}/catalog`),
  diagnoseMcp: (server: string, command: Command) =>
    mutate(`/api/mcp/${resource(server)}/diagnose`, "POST", command),
  authorizeMcp: (server: string, command: Command) =>
    mutate(`/api/mcp/${resource(server)}/authorize`, "POST", command),
  memories: (workspace: string) =>
    request(`/api/memories?workspace_id=${resource(workspace)}`),
  memory: (memory: string) =>
    request<ObjectValue>(`/api/memories/${resource(memory)}`),
  reviewMemory: (memory: string, command: Command) =>
    mutate(`/api/memories/${resource(memory)}/review`, "POST", command),
  updateMemory: (memory: string, command: Command) =>
    mutate(`/api/memories/${resource(memory)}`, "PATCH", command),
  forgetMemory: (memory: string, revision: number, command: Command) =>
    request(`/api/memories/${resource(memory)}`, {
      method: "DELETE",
      command,
      headers: { "If-Match": String(revision) },
    }),
  artifactUrl: (artifact: string, preview = false) =>
    `/api/artifacts/${resource(artifact)}?disposition=${preview ? "preview" : "attachment"}`,
  upload: async (workspace: string, file: File, key: string) => {
    const identity = JSON.stringify([
      workspace,
      file.name,
      file.size,
      file.lastModified,
      file.type,
    ]);
    const originalKey = unresolvedUploads.get(identity) ?? key;
    unresolvedUploads.set(identity, originalKey);
    const form = new FormData();
    form.set("workspace_id", workspace);
    form.set("file", file);
    try {
      const result = await request<ArtifactRef>("/api/artifacts", {
        method: "POST",
        form,
        headers: { "Idempotency-Key": originalKey },
      });
      unresolvedUploads.delete(identity);
      return result;
    } catch (error) {
      if (
        error instanceof ApiError &&
        error.status < 500 &&
        ![408, 429].includes(error.status)
      )
        unresolvedUploads.delete(identity);
      throw error;
    }
  },
  health: () => request("/health/ready"),
  metrics: () => request("/api/diagnostics/metrics"),
  diagnostics: async () => {
    const results = await Promise.allSettled([
      request("/health/ready"),
      request("/api/diagnostics/metrics"),
    ]);
    return {
      health:
        results[0].status === "fulfilled"
          ? results[0].value
          : { error: String(results[0].reason) },
      metrics:
        results[1].status === "fulfilled"
          ? results[1].value
          : { error: String(results[1].reason) },
    };
  },
};

async function allPages(path: string): Promise<{ items: ObjectValue[] }> {
  const collected: ObjectValue[] = [];
  let cursor = "";
  do {
    const page = await request<{
      items: ObjectValue[];
      next_cursor?: string | null;
    }>(
      path +
        (path.includes("?") ? "&" : "?") +
        "limit=100" +
        (cursor ? `&cursor=${resource(cursor)}` : ""),
    );
    collected.push(...page.items);
    cursor = page.next_cursor || "";
  } while (cursor);
  return { items: collected };
}
