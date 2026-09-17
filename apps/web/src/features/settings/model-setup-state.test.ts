import { describe, expect, it } from "vitest";
import type { ModelProvider } from "../../api/client";
import {
  canReuseModelKey,
  editModelDraft,
  emptyModelDraft,
  mergeModelChoices,
  modelDraftError,
  modelSetupBody,
  switchModelProvider,
} from "./model-setup-state";

const providers: ModelProvider[] = [
  { id: "openai", name: "OpenAI", requires_api_key: true, models: [] },
  { id: "ollama", name: "Ollama", requires_api_key: false, models: [] },
  { id: "custom", name: "自定义", requires_api_key: false, models: [] },
];
describe("model configuration intent", () => {
  it("editing never reads the old key and omits a blank replacement key", () => {
    const draft = editModelDraft(
      {
        id: "saved",
        revision: 4,
        provider_id: "openai",
        model_id: "existing",
        credential_ref: "vault-ref",
      },
      providers,
    );
    expect(draft.apiKey).toBe("");
    expect(canReuseModelKey(draft)).toBe(true);
    expect(modelDraftError(draft, providers[0])).toBe("");
    expect(modelSetupBody(draft, true, true)).toEqual({
      provider_id: "openai",
      model_id: "existing",
      profile_id: "saved",
      expected_revision: 4,
    });
    expect(modelSetupBody(draft, false)).toEqual({
      provider_id: "openai",
      profile_id: "saved",
    });
  });
  it("changing provider clears typed credentials and model and cannot reuse the old provider credential", () => {
    const current = editModelDraft(
      {
        id: "saved",
        revision: 4,
        provider_id: "openai",
        model_id: "existing",
        credential_ref: "vault-ref",
      },
      providers,
    );
    const switched = switchModelProvider(
      { ...current, apiKey: "secret", baseUrl: "https://old.invalid" },
      "anthropic",
    );
    expect(switched).toMatchObject({
      apiKey: "",
      modelId: "",
      baseUrl: "",
      savedKey: false,
      profileId: "saved",
      expectedRevision: 4,
    });
    expect(canReuseModelKey(switched)).toBe(false);
    expect(canReuseModelKey(switchModelProvider(switched, "openai"))).toBe(
      false,
    );
  });
  it("local Ollama needs no key and custom model names are valid without catalog membership", () => {
    const draft = {
      ...emptyModelDraft(),
      providerId: "ollama",
      modelId: "my-local-model:custom",
      baseUrl: "https://unexpected.invalid",
    };
    expect(modelDraftError(draft, providers[1])).toBe("");
    expect(modelSetupBody(draft)).toEqual({
      provider_id: "ollama",
      model_id: "my-local-model:custom",
    });
  });
  it("only custom accepts an explicit endpoint, and userinfo in a URL is rejected", () => {
    const draft = {
      ...emptyModelDraft(),
      providerId: "custom",
      modelId: "custom-id",
      baseUrl: "http://127.0.0.1:9999/v1",
    };
    expect(modelDraftError(draft, providers[2])).toBe("");
    expect(modelSetupBody(draft)).toMatchObject({ base_url: draft.baseUrl });
    expect(
      modelDraftError(
        { ...draft, baseUrl: "https://key@example.invalid" },
        providers[2],
      ),
    ).toContain("不含用户名或密码");
    expect(
      modelDraftError({ ...draft, baseUrl: "file:///tmp/local" }, providers[2]),
    ).not.toBe("");
  });
  it("discovered names replace stale catalog names without losing catalog options", () => {
    expect(
      mergeModelChoices(
        [
          { id: "a", name: "old" },
          { id: "b", name: "B" },
        ],
        [
          { id: "a", name: "A" },
          { id: "c", name: "C" },
        ],
      ),
    ).toEqual([
      { id: "a", name: "A" },
      { id: "b", name: "B" },
      { id: "c", name: "C" },
    ]);
  });
  it("keeps a legacy proxy address explicit and requires a fresh key for conservative migration", () => {
    const draft = editModelDraft(
      {
        id: "proxy",
        provider_id: "openai",
        model_id: "old",
        endpoint_ref: "https://proxy.example/v1",
        credential_ref: "vault",
        revision: 1,
      },
      [
        { ...providers[0], default_base_url: "https://api.openai.com/v1" },
        providers[2],
      ],
    );
    expect(draft).toMatchObject({
      providerId: "custom",
      baseUrl: "https://proxy.example/v1",
      savedKey: false,
      needsFreshKey: true,
      legacyEndpoint: true,
    });
    expect(modelDraftError(draft, providers[2])).toContain("API Key");
    expect(
      modelSetupBody({ ...draft, apiKey: "new-fixture-key" }, true, true),
    ).toMatchObject({
      provider_id: "custom",
      base_url: "https://proxy.example/v1",
      api_key: "new-fixture-key",
      profile_id: "proxy",
    });
  });
});
