import { describe, expect, it } from "vitest";
import { finishUpload, insertBlocks, moveBlock, restoreDraft, toContentParts, type DraftBlock } from "./composer-state";
import type { ArtifactRef } from "../../api/types";

const ref = { artifact_id: "a", content_hash: "hash", mime_type: "image/png", size_bytes: 20, owner_id: "local", workspace_id: "w", provenance_ref: "upload" } as ArtifactRef;
describe("ordered composer", () => {
  it("dragging stable block ids preserves pending uploads, duplicates and source draft", () => {
    const original: DraftBlock[] = [
      { id: "text", type: "text", text: "A" },
      { id: "one", type: "image", state: "uploading", label: "one" },
      { id: "two", type: "image", state: "ready", artifactRef: ref, label: "two" },
    ];
    const moved = moveBlock(original, "one", "text");
    expect(moved.map(b => b.id)).toEqual(["one", "text", "two"]);
    const done = finishUpload(moved, "one", ref);
    expect(toContentParts(done)?.map(p => p.type)).toEqual(["image", "text", "image"]);
    expect(moveBlock(done, "one", "two", true).map(b => b.id)).toEqual(["text", "two", "one"]);
    expect(original.map(b => b.id)).toEqual(["text", "one", "two"]);
    expect(moveBlock(done, "missing", "text")).toBe(done);
    expect(moveBlock(done, "one", "one")).toBe(done);
  });
  it("splits text at the cursor and preserves duplicate image positions", () => {
    const blocks: DraftBlock[] = [{ id: "text", type: "text", text: "AB" }];
    const next = insertBlocks(blocks, "text", 1, [{ id: "img", type: "image", state: "ready", artifactRef: ref, label: "image" }]);
    expect(toContentParts(next)?.map(p => p.type)).toEqual(["text", "image", "text"]);
    const twice = [...next, { ...next[1], id: "again" }];
    expect(toContentParts(twice)?.filter(p => p.type === "image")).toHaveLength(2);
  });
  it("out of order upload completion does not reorder blocks or resurrect deletion", () => {
    const pending: DraftBlock[] = ["one", "two"].map(id => ({ id, type: "image", state: "uploading", label: id }));
    const done = finishUpload(finishUpload(pending, "two", ref), "one", ref);
    expect(done.map(b => b.id)).toEqual(["one", "two"]);
    expect(finishUpload(done.filter(b => b.id !== "one"), "one", ref).map(b => b.id)).toEqual(["two"]);
    expect(() => toContentParts(pending)).toThrow("上传");
    expect(restoreDraft(pending)[0]).toMatchObject({ state: "failed" });
  });
});
