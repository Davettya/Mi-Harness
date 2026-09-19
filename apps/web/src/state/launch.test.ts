import { afterEach, expect, it, vi } from "vitest";

afterEach(() => {
  vi.unstubAllGlobals();
  vi.resetModules();
});

it("removes the launch fragment and exchanges only once under concurrent mounts", async () => {
  const replaceState = vi.fn();
  vi.stubGlobal("window", {
    location: { hash: "#launch=one-time", pathname: "/", search: "" },
    history: { replaceState },
  });
  vi.stubGlobal("document", { cookie: "" });
  const fetch = vi
    .fn()
    .mockResolvedValueOnce(new Response(null, { status: 204 }))
    .mockResolvedValueOnce(
      new Response(
        JSON.stringify({ owner_id: "local", csrf_token: "csrf", capabilities: {} }),
        { status: 200 },
      ),
    );
  vi.stubGlobal("fetch", fetch);
  const { connectLaunch } = await import("./launch");
  const sessions = await Promise.all([connectLaunch(), connectLaunch()]);
  expect(replaceState).toHaveBeenCalledWith(null, "", "/");
  expect(sessions[0].owner_id).toBe("local");
  expect(fetch).toHaveBeenCalledTimes(2);
  expect(fetch.mock.calls[0][0]).toBe("/api/auth/exchange");
  expect(fetch.mock.calls[1][0]).toBe("/api/auth/session");
});

it("silently creates one loopback session when a direct visit has no cookie", async () => {
  vi.stubGlobal("window", {
    location: { hash: "", pathname: "/", search: "" },
    history: { replaceState: vi.fn() },
  });
  vi.stubGlobal("document", { cookie: "" });
  const fetch = vi
    .fn()
    .mockResolvedValueOnce(
      new Response(JSON.stringify({ code: "AUTH_REQUIRED" }), { status: 401 }),
    )
    .mockResolvedValueOnce(
      new Response(null, {
        status: 204,
        headers: { "X-CSRF-Token": "automatic-csrf" },
      }),
    )
    .mockResolvedValueOnce(
      new Response(
        JSON.stringify({
          owner_id: "local",
          csrf_token: "automatic-csrf",
          capabilities: {},
        }),
        { status: 200 },
      ),
    );
  vi.stubGlobal("fetch", fetch);
  const { connectLaunch } = await import("./launch");
  const [first, second] = await Promise.all([connectLaunch(), connectLaunch()]);
  expect(first.owner_id).toBe("local");
  expect(second.csrf_token).toBe("automatic-csrf");
  expect(fetch.mock.calls.map((call) => call[0])).toEqual([
    "/api/auth/session",
    "/api/auth/local",
    "/api/auth/session",
  ]);
});

it("collects every project page instead of hiding older projects", async () => {
  vi.stubGlobal("document", { cookie: "" });
  const fetch = vi
    .fn()
    .mockResolvedValueOnce(
      new Response(
        JSON.stringify({ items: [{ id: "first" }], next_cursor: "next" }),
      ),
    )
    .mockResolvedValueOnce(
      new Response(
        JSON.stringify({ items: [{ id: "second" }], next_cursor: null }),
      ),
    );
  vi.stubGlobal("fetch", fetch);
  const { api } = await import("../api/client");
  expect((await api.workspaces()).items.map((value) => value.id)).toEqual([
    "first",
    "second",
  ]);
  expect(fetch.mock.calls[1][0]).toContain("cursor=next");
});
