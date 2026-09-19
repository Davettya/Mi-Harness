import { describe, expect, it } from "vitest";
import {
  DEFAULT_SIDEBAR_WIDTH,
  MAX_SIDEBAR_WIDTH,
  MIN_SIDEBAR_WIDTH,
  clampSidebarWidth,
  keyboardSidebarWidth,
  loadProjectSessionGroups,
  storedSidebarWidth,
} from "./sidebar-state";

describe("sidebar width", () => {
  it("clamps persisted and dragged widths to the desktop contract", () => {
    expect(storedSidebarWidth(null)).toBe(DEFAULT_SIDEBAR_WIDTH);
    expect(storedSidebarWidth("not-a-number")).toBe(DEFAULT_SIDEBAR_WIDTH);
    expect(clampSidebarWidth(50)).toBe(MIN_SIDEBAR_WIDTH);
    expect(clampSidebarWidth(800)).toBe(MAX_SIDEBAR_WIDTH);
    expect(clampSidebarWidth(333.6)).toBe(334);
  });

  it("supports keyboard resizing without exceeding the bounds", () => {
    expect(keyboardSidebarWidth(300, "ArrowLeft")).toBe(284);
    expect(keyboardSidebarWidth(300, "ArrowRight")).toBe(316);
    expect(keyboardSidebarWidth(300, "Home")).toBe(MIN_SIDEBAR_WIDTH);
    expect(keyboardSidebarWidth(300, "End")).toBe(MAX_SIDEBAR_WIDTH);
    expect(keyboardSidebarWidth(300, "Enter")).toBeNull();
  });
});

describe("session list filtering", () => {
  it("loads active and archived groups as separate replacements", async () => {
    const rows = {
      alpha: { active: ["current-a"], archived: ["archived-a"] },
      beta: { active: ["current-b"], archived: ["archived-b"] },
    };
    const load = async (project: string, archived: boolean) =>
      rows[project as keyof typeof rows][archived ? "archived" : "active"];

    expect(
      await loadProjectSessionGroups(Object.keys(rows), true, load),
    ).toEqual({
      alpha: ["archived-a"],
      beta: ["archived-b"],
    });
    expect(
      await loadProjectSessionGroups(Object.keys(rows), false, load),
    ).toEqual({
      alpha: ["current-a"],
      beta: ["current-b"],
    });
  });
});
