import { describe, expect, it } from "vitest";
import { draftLength, replaceDraftRange } from "./inline-draft";
import {
  finishUpload,
  toContentParts,
  type DraftBlock,
} from "./composer-state";
import type { ArtifactRef } from "../../api/types";
const text = (value: string): DraftBlock => ({
  id: crypto.randomUUID(),
  type: "text",
  text: value,
});
const image: DraftBlock = {
  id: "image",
  type: "image",
  label: "测试.png",
  state: "uploading",
};
const ref = { artifact_id: "a", mime_type: "image/png" } as ArtifactRef;
describe("continuous inline draft", () => {
  it("inserts at a Chinese cursor and keeps text around duplicate attachments", () => {
    const original = [text("前文后文")];
    const next = replaceDraftRange(original, 2, 2, [
      image,
      { ...image, id: "second" },
    ]);
    expect(next.map((b) => (b.type === "text" ? b.text : b.id))).toEqual([
      "前文",
      "image",
      "second",
      "后文",
    ]);
    expect(draftLength(next)).toBe(6);
    expect(original[0]).toMatchObject({ text: "前文后文" });
  });
  it("replaces a selection across text and attachments without leaking chip labels", () => {
    const result = replaceDraftRange([text("ABC"), image, text("DEF")], 1, 6, [
      text("新\n内容"),
    ]);
    expect(result).toHaveLength(1);
    expect(result[0]).toMatchObject({ text: "A新\n内容F" });
  });
  it("deleting a chip joins neighboring text and late completion never resurrects it", () => {
    const result = replaceDraftRange([text("A"), image, text("B")], 1, 2, []);
    expect(finishUpload(result, "image", ref)).toEqual(result);
    expect(toContentParts(result)).toEqual([{ type: "text", text: "AB" }]);
  });
  it("handles empty drafts, boundaries, emoji UTF-16 offsets and upload order", () => {
    const start = replaceDraftRange([], 0, 0, [text("🙂尾")]);
    const next = replaceDraftRange(start, 2, 2, [image]);
    expect(
      toContentParts(finishUpload(next, "image", ref))?.map((p) => p.type),
    ).toEqual(["text", "image", "text"]);
    expect(replaceDraftRange(next, 0, draftLength(next), [])[0]).toMatchObject({
      type: "text",
      text: "",
    });
    expect(replaceDraftRange(start, 3, 3, [text("部")])[0]).toMatchObject({
      text: "🙂尾部",
    });
  });
});
