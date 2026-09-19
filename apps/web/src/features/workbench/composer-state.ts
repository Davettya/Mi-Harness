import type { ArtifactRef, SubmitRunInput } from "../../api/types";

export type DraftBlock = { id: string; type: "text"; text: string } | {
  id: string; type: "image" | "file_reference"; state: "uploading" | "ready" | "failed";
  artifactRef?: ArtifactRef; label: string; error?: string;
};
export const emptyDraft = (): DraftBlock[] => [{ id: crypto.randomUUID(), type: "text", text: "" }];
export function insertBlocks(blocks: DraftBlock[], at: string, offset: number, added: DraftBlock[]): DraftBlock[] {
  const index = blocks.findIndex(b => b.id === at);
  const block = blocks[index];
  if (!block || block.type !== "text") return [...blocks, ...added, ...emptyDraft()];
  return [...blocks.slice(0, index), { ...block, text: block.text.slice(0, offset) }, ...added,
    { id: crypto.randomUUID(), type: "text", text: block.text.slice(offset) }, ...blocks.slice(index + 1)];
}
export function finishUpload(blocks: DraftBlock[], id: string, ref?: ArtifactRef, error?: string): DraftBlock[] {
  return blocks.map(b => b.id === id && b.type !== "text" ? { ...b, state: ref ? "ready" : "failed", artifactRef: ref, error } : b);
}
export function moveBlock(blocks: DraftBlock[], sourceId: string, targetId: string, after = false): DraftBlock[] {
  const source = blocks.find(b => b.id === sourceId);
  if (!source || sourceId === targetId || !blocks.some(b => b.id === targetId)) return blocks;
  const next = blocks.filter(b => b.id !== sourceId);
  const target = next.findIndex(b => b.id === targetId);
  next.splice(target + (after ? 1 : 0), 0, source);
  return next;
}
export function toContentParts(blocks: DraftBlock[]): SubmitRunInput["content_parts"] {
  if (blocks.some(b => b.type !== "text" && b.state !== "ready")) throw new Error("请等待上传完成或移除失败附件。");
  return blocks.flatMap(b => b.type === "text" ? (b.text ? [{ type: "text" as const, text: b.text }] : []) :
    [{ type: b.type, artifact_ref: b.artifactRef!, ...(b.type === "image" ? { alt: b.label } : { label: b.label }) }] as NonNullable<SubmitRunInput["content_parts"]>);
}
export function restoreDraft(blocks: DraftBlock[]): DraftBlock[] {
  return blocks.map(b => b.type !== "text" && b.state === "uploading" ? { ...b, state: "failed", error: "上传已中断，请重新选择文件" } : b);
}
