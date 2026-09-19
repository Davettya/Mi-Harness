import { emptyDraft, type DraftBlock } from "./composer-state";

// UTF-16 text offsets; each attachment occupies one atomic position.
export const draftLength = (blocks: DraftBlock[]) =>
  blocks.reduce((n, b) => n + (b.type === "text" ? b.text.length : 1), 0);
export function replaceDraftRange(
  blocks: DraftBlock[],
  start: number,
  end: number,
  added: DraftBlock[],
): DraftBlock[] {
  const before: DraftBlock[] = [],
    after: DraftBlock[] = [];
  let offset = 0;
  for (const block of blocks) {
    const length = block.type === "text" ? block.text.length : 1;
    if (offset + length <= start) before.push(block);
    else if (offset >= end) after.push(block);
    else if (block.type === "text") {
      if (start > offset)
        before.push({ ...block, text: block.text.slice(0, start - offset) });
      if (end < offset + length)
        after.push({
          ...block,
          id: crypto.randomUUID(),
          text: block.text.slice(end - offset),
        });
    }
    offset += length;
  }
  const result: DraftBlock[] = [];
  for (const block of [...before, ...added, ...after]) {
    const last = result.at(-1);
    if (block.type === "text" && last?.type === "text") last.text += block.text;
    else if (block.type !== "text" || block.text) result.push({ ...block });
  }
  return result.length ? result : emptyDraft();
}

export function readEditor(root: Node, known: DraftBlock[]): DraftBlock[] {
  const result: DraftBlock[] = [];
  const append = (text: string) => {
    const last = result.at(-1);
    if (last?.type === "text") last.text += text;
    else if (text) result.push({ id: crypto.randomUUID(), type: "text", text });
  };
  const walk = (node: Node) => {
    if (node.nodeType === Node.TEXT_NODE) {
      append(node.textContent ?? "");
      return;
    }
    if (node instanceof HTMLElement) {
      if (node.dataset.caretEnd !== undefined) return;
      const id = node.dataset.attachment;
      if (id) {
        const block = known.find((b) => b.id === id && b.type !== "text");
        if (block) result.push(block);
        return;
      }
      if (node.tagName === "BR") {
        append("\n");
        return;
      }
      if ((node.tagName === "DIV" || node.tagName === "P") && result.length)
        append("\n");
    }
    node.childNodes.forEach(walk);
  };
  root.childNodes.forEach(walk);
  return result;
}

export function editorSelection(
  root: HTMLElement,
  known: DraftBlock[],
): { start: number; end: number } | null {
  const selection = window.getSelection();
  if (!selection?.rangeCount) return null;
  const range = selection.getRangeAt(0);
  if (
    !root.contains(range.startContainer) ||
    !root.contains(range.endContainer)
  )
    return null;
  const prefix = document.createRange();
  prefix.selectNodeContents(root);
  prefix.setEnd(range.startContainer, range.startOffset);
  const start = draftLength(readEditor(prefix.cloneContents(), known));
  prefix.setEnd(range.endContainer, range.endOffset);
  return { start, end: draftLength(readEditor(prefix.cloneContents(), known)) };
}

export function placeSelection(root: HTMLElement, start: number, end = start) {
  const point = (position: number): [Node, number] => {
    let offset = 0;
    for (let i = 0; i < root.childNodes.length; i++) {
      const node = root.childNodes[i];
      const length =
        node.nodeType === Node.TEXT_NODE
          ? (node.textContent?.length ?? 0)
          : node instanceof HTMLElement && node.dataset.caretEnd !== undefined
            ? 0
            : 1;
      if (position <= offset + length) {
        if (node.nodeType === Node.TEXT_NODE)
          return [node, Math.max(0, position - offset)];
        return [root, i + (position > offset ? 1 : 0)];
      }
      offset += length;
    }
    return [root, root.childNodes.length];
  };
  const range = document.createRange();
  range.setStart(...point(start));
  range.setEnd(...point(end));
  const selection = window.getSelection();
  selection?.removeAllRanges();
  selection?.addRange(range);
}
