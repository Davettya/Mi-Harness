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
  const fetch = vi.fn().mockResolvedValue(new Response(null, { status: 204 }));
  vi.stubGlobal("fetch", fetch);
  const { connectLaunch } = await import("./launch");
  await Promise.all([connectLaunch(), connectLaunch()]);
  expect(replaceState).toHaveBeenCalledWith(null, "", "/");
  expect(fetch).toHaveBeenCalledTimes(1);
  expect(fetch.mock.calls[0][0]).toBe("/api/auth/exchange");
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
