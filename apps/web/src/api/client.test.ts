import { afterEach, describe, expect, it, vi } from "vitest";
import { api, ApiError, Command, request } from "./client";

afterEach(() => vi.unstubAllGlobals());
describe("command coordination", () => {
  it("retains the exact original body and idempotency key on retry", async () => {
    vi.stubGlobal("document", { cookie: "harness_csrf=token" });
    const mock = vi.fn().mockImplementation(() =>
      Promise.resolve(
        new Response('{"revision":1}', {
          headers: { "Content-Type": "application/json" },
        }),
      ),
    );
    vi.stubGlobal("fetch", mock);
    const body = { text: "original" };
    const command = new Command(body);
    body.text = "changed elsewhere";
    await request("/api/test", { method: "POST", command });
    await request("/api/test", { method: "POST", command });
    expect(mock.mock.calls[0][1].body).toBe('{"text":"original"}');
    expect(mock.mock.calls[1][1].body).toBe(mock.mock.calls[0][1].body);
    expect(mock.mock.calls[0][1].headers.get("Idempotency-Key")).toBe(
      mock.mock.calls[1][1].headers.get("Idempotency-Key"),
    );
    expect(mock.mock.calls[0][1].headers.get("X-CSRF-Token")).toBe("token");
  });
  it("reports revision conflicts without silently resubmitting a new command", async () => {
    vi.stubGlobal("document", { cookie: "" });
    const fetch = vi.fn().mockResolvedValue(
      new Response('{"code":"REVISION_CONFLICT","message":"版本已改变"}', {
        status: 409,
      }),
    );
    vi.stubGlobal("fetch", fetch);
    await expect(request("/api/test")).rejects.toBeInstanceOf(ApiError);
    expect(fetch).toHaveBeenCalledTimes(1);
  });
});

describe("sensitive model setup requests", () => {
  it("uses CSRF but no command cache or retry for discovery and tests", async () => {
    vi.stubGlobal("document", { cookie: "harness_csrf=setup-csrf" });
    const fetch = vi
      .fn()
      .mockRejectedValueOnce(new Error("transport exposed key-canary"))
      .mockResolvedValueOnce(new Response('{"items":[],"source":"live"}'));
    vi.stubGlobal("fetch", fetch);
    const body = { provider_id: "openai", api_key: "key-canary" };
    await expect(api.discoverModels(body)).rejects.toThrow("连接请求未完成");
    expect(fetch).toHaveBeenCalledTimes(1);
    await api.discoverModels(body);
    for (const [, options] of fetch.mock.calls) {
      expect(options.headers.get("Idempotency-Key")).toBeNull();
      expect(options.headers.get("X-CSRF-Token")).toBe("setup-csrf");
      expect(options.credentials).toBe("same-origin");
      expect(JSON.parse(options.body).api_key).toBe("key-canary");
    }
  });
  it("save generates a per-request key and never reflects an error body or transport detail", async () => {
    vi.stubGlobal("document", { cookie: "" });
    const fetch = vi.fn().mockImplementation(() =>
      Promise.resolve(
        new Response(
          JSON.stringify({
            code: "canary-should-not-display",
            message: "API Key key-canary was rejected",
            body: { api_key: "key-canary" },
          }),
          { status: 422 },
        ),
      ),
    );
    vi.stubGlobal("fetch", fetch);
    const body = {
      provider_id: "openai",
      model_id: "model",
      api_key: "key-canary",
    };
    for (let i = 0; i < 2; i++) {
      try {
        await api.saveModelSetup(body);
        throw new Error("expected rejection");
      } catch (error) {
        expect(error).toBeInstanceOf(ApiError);
        expect(String(error)).not.toContain("canary");
        expect((error as ApiError).code).toBe("MODEL_SETUP_ERROR");
      }
    }
    expect(fetch).toHaveBeenCalledTimes(2);
    expect(fetch.mock.calls[0][1].headers.get("Idempotency-Key")).not.toBe(
      fetch.mock.calls[1][1].headers.get("Idempotency-Key"),
    );
    expect(fetch.mock.calls[0][0]).toBe("/api/model-setup/save");
  });
  it("keeps a pinned default model error distinct from a revision conflict", async () => {
    vi.stubGlobal("document", { cookie: "" });
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(
          JSON.stringify({
            code: "MODEL_PROFILE_PINNED",
            message: "raw detail must be ignored",
          }),
          { status: 409 },
        ),
      ),
    );
    await expect(
      api.saveModelSetup({ provider_id: "ollama", model_id: "local" }),
    ).rejects.toMatchObject({
      code: "MODEL_PROFILE_PINNED",
      message:
        "启动配置固定了默认模型，请移除 model_profile_ref 固定项并重启后再保存。",
    });
  });
});
