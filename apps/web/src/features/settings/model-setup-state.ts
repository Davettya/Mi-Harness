import type {
  ModelChoice,
  ModelConnectionTest,
  ModelProvider,
  ModelSetupInput,
} from "../../api/client";
import { id, number, object, string, type ObjectValue } from "../../api/values";

export type ModelContextWindow = 300000 | 1000000;

export function configuredContextWindow(profile: ObjectValue): ModelContextWindow {
  return number(object(profile.limits).context_window) === 1000000
    ? 1000000
    : 300000;
}

export interface ModelDraft {
  providerId: string;
  modelId: string;
  apiKey: string;
  baseUrl: string;
  contextWindow: ModelContextWindow;
  profileId?: string;
  expectedRevision?: number;
  originalProviderId?: string;
  savedKey: boolean;
  needsFreshKey?: boolean;
  legacyEndpoint?: boolean;
}

export const emptyModelDraft = (): ModelDraft => ({
  providerId: "",
  modelId: "",
  apiKey: "",
  baseUrl: "",
  contextWindow: 300000,
  savedKey: false,
});

export function editModelDraft(
  profile: ObjectValue,
  providers: ModelProvider[],
): ModelDraft {
  const original = string(profile.provider_id);
  const known = providers.find((provider) => provider.id === original);
  const endpoint = string(profile.endpoint_ref);
  const normalize = (value: string) => value.replace(/\/+$/, "");
  const nonOfficialEndpoint =
    known &&
    known.id !== "custom" &&
    endpoint &&
    known.default_base_url &&
    normalize(endpoint) !== normalize(known.default_base_url);
  const providerId = known && !nonOfficialEndpoint ? original : "custom";
  const migration = providerId !== original;
  return {
    providerId,
    modelId: string(profile.model_id),
    apiKey: "",
    baseUrl: providerId === "custom" ? endpoint : "",
    contextWindow: configuredContextWindow(profile),
    profileId: id(profile) || string(profile.profile_id),
    expectedRevision: number(profile.revision),
    originalProviderId: original,
    savedKey: Boolean(profile.credential_ref) && !migration,
    needsFreshKey: Boolean(profile.credential_ref) && migration,
    legacyEndpoint: migration,
  };
}

/** Credentials and discovery/test state never carry across provider changes. */
export function switchModelProvider(
  draft: ModelDraft,
  providerId: string,
): ModelDraft {
  return {
    ...draft,
    providerId,
    modelId: "",
    apiKey: "",
    baseUrl: "",
    contextWindow: 300000,
    savedKey: false,
    needsFreshKey: false,
    legacyEndpoint: false,
  };
}

export function canReuseModelKey(draft: ModelDraft): boolean {
  return Boolean(
    draft.profileId &&
      draft.savedKey &&
      draft.providerId === draft.originalProviderId,
  );
}

export function modelSetupBody(
  draft: ModelDraft,
  includeModel = true,
  includeRevision = false,
): ModelSetupInput {
  return {
    provider_id: draft.providerId,
    ...(draft.apiKey.trim() ? { api_key: draft.apiKey.trim() } : {}),
    ...(draft.profileId ? { profile_id: draft.profileId } : {}),
    ...(draft.providerId === "custom" && draft.baseUrl.trim()
      ? { base_url: draft.baseUrl.trim() }
      : {}),
    ...(includeModel
      ? {
          model_id: draft.modelId.trim(),
          context_window: draft.contextWindow,
        }
      : {}),
    ...(includeRevision && draft.expectedRevision !== undefined
      ? { expected_revision: draft.expectedRevision }
      : {}),
  };
}

export function modelDraftError(
  draft: ModelDraft,
  provider: ModelProvider | undefined,
  includeModel = true,
): string {
  if (!provider) return "请选择模型提供方。";
  if (
    (provider.requires_api_key || draft.needsFreshKey) &&
    !draft.apiKey.trim() &&
    !canReuseModelKey(draft)
  )
    return "请填写此提供方的 API Key。";
  if (provider.id === "custom") {
    try {
      const url = new URL(draft.baseUrl);
      if (
        !["http:", "https:"].includes(url.protocol) ||
        url.username ||
        url.password
      )
        return "请输入不含用户名或密码的 HTTP(S) 服务地址。";
    } catch {
      return "请填写自定义服务的完整 URL。";
    }
  }
  if (includeModel && !draft.modelId.trim()) return "请选择或输入模型名称。";
  return "";
}

export function mergeModelChoices(
  catalog: ModelChoice[],
  discovered: ModelChoice[],
): ModelChoice[] {
  const byId = new Map(catalog.map((model) => [model.id, model]));
  for (const model of discovered) if (model.id) byId.set(model.id, model);
  return [...byId.values()];
}

/** A quick connection receipt never authorizes activation or reduced-capability saving. */
export function modelVerificationState(result: ModelConnectionTest | null) {
  const complete = result?.probe_mode === "agent";
  const ready = Boolean(
    complete &&
      result?.verification_token &&
      ["transport", "text", "tool_calling", "tool_pairing"].every(
        (name) => result?.checks?.[name] === true,
      ),
  );
  const missing = (["vision", "streaming"] as const).filter(
    (name) => result?.checks?.[name] !== true,
  );
  return { ready, missing, limited: ready && missing.length > 0 };
}

export function modelCheckLabel(
  result: ModelConnectionTest,
  name: string,
): string {
  if (result.checks?.[name] === true) return "通过";
  if (result.checks?.[name] !== false) return "未检测";
  const failure = result.check_failures?.[name];
  if (failure === "PROBE_TIMEOUT") return "超时，请重试";
  if (failure === "PROBE_RESPONSE_MISMATCH") return "回答未满足检测要求";
  return "未通过，请重试或检查服务状态";
}
