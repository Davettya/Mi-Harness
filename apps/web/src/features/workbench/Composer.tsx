import { useLayoutEffect, useRef, type ReactNode } from "react";
import { api } from "../../api/client";
import { finishUpload, type DraftBlock } from "./composer-state";
import {
  draftLength,
  editorSelection,
  placeSelection,
  readEditor,
  replaceDraftRange,
} from "./inline-draft";

export function Composer({
  blocks,
  onChange,
  workspace,
  disabled,
  onSend,
  children,
  sendButton,
}: {
  blocks: DraftBlock[];
  onChange: (update: (old: DraftBlock[]) => DraftBlock[]) => void;
  workspace: string;
  disabled: boolean;
  onSend: () => void;
  children: ReactNode;
  sendButton: ReactNode;
}) {
  const editor = useRef<HTMLDivElement>(null);
  const picker = useRef<HTMLInputElement>(null);
  const files = useRef(new Map<string, File>());
  const uploaded = useRef(new Map<string, DraftBlock>());
  const history = useRef<DraftBlock[][]>([]);
  const future = useRef<DraftBlock[][]>([]);
  const cursor = useRef({
    start: draftLength(blocks),
    end: draftLength(blocks),
  });
  const pendingCursor = useRef<number | null>(null);
  const composing = useRef(false);
  const current = useRef(blocks);
  current.current = blocks;

  function rememberCursor() {
    if (editor.current)
      cursor.current =
        editorSelection(editor.current, current.current) ?? cursor.current;
  }
  function change(next: DraftBlock[]) {
    history.current = [...history.current.slice(-99), current.current];
    future.current = [];
    current.current = next;
    onChange(() => next);
  }
  function replace(added: DraftBlock[]) {
    rememberCursor();
    const { start, end } = cursor.current;
    pendingCursor.current = start + draftLength(added);
    change(replaceDraftRange(current.current, start, end, added));
  }
  async function upload(id: string, file: File) {
    files.current.set(id, file);
    onChange((old) =>
      old.map((b) =>
        b.id === id && b.type !== "text"
          ? { ...b, state: "uploading", error: undefined }
          : b,
      ),
    );
    try {
      const ref = await api.upload(workspace, file, crypto.randomUUID());
      const original = uploaded.current.get(id);
      if (original)
        uploaded.current.set(id, finishUpload([original], id, ref)[0]);
      onChange((old) => finishUpload(old, id, ref));
    } catch (error) {
      const message = error instanceof Error ? error.message : "上传失败";
      const original = uploaded.current.get(id);
      if (original)
        uploaded.current.set(
          id,
          finishUpload([original], id, undefined, message)[0],
        );
      onChange((old) => finishUpload(old, id, undefined, message));
    }
  }
  function add(selected: File[]) {
    if (!workspace || disabled || !selected.length) return;
    const added = selected.map(
      (file) =>
        ({
          id: crypto.randomUUID(),
          type: file.type.startsWith("image/") ? "image" : "file_reference",
          state: "uploading",
          label: file.name,
        }) as DraftBlock,
    );
    added.forEach((block) => uploaded.current.set(block.id, block));
    replace(added);
    added.forEach((block, index) => void upload(block.id, selected[index]));
  }
  function undo(redo = false) {
    const source = redo ? future : history,
      target = redo ? history : future;
    const previous = source.current.pop();
    if (!previous) return;
    target.current.push(current.current);
    const next = previous.map((b) =>
      b.type === "text" ? b : (uploaded.current.get(b.id) ?? b),
    );
    pendingCursor.current = draftLength(next);
    current.current = next;
    onChange(() => next);
  }

  useLayoutEffect(() => {
    const root = editor.current;
    if (!root || composing.current) return;
    const selection = editorSelection(root, blocks);
    // Leave native typing/IME DOM intact; update only structure or attachment status.
    const signature = (value: DraftBlock[]) =>
      JSON.stringify(
        value
          .filter((b) => b.type !== "text" || b.text)
          .map((b) => (b.type === "text" ? { type: b.type, text: b.text } : b)),
      );
    const last = blocks.at(-1);
    const needsCaretEnd = Boolean(
      last?.type === "text" && last.text.endsWith("\n"),
    );
    if (
      signature(readEditor(root, blocks)) !== signature(blocks) ||
      root.dataset.rendered !==
        JSON.stringify(blocks.filter((b) => b.type !== "text")) ||
      Boolean(root.querySelector("[data-caret-end]")) !== needsCaretEnd
    ) {
      const fragment = document.createDocumentFragment();
      for (const block of blocks) {
        if (block.type === "text") {
          fragment.append(document.createTextNode(block.text));
          continue;
        }
        const chip = document.createElement("span");
        chip.dataset.attachment = block.id;
        chip.contentEditable = "false";
        chip.className = `inline-attachment ${block.state}`;
        chip.title = `${block.label}${block.error ? `：${block.error}` : ""}`;
        const icon = document.createElement("span");
        icon.className = "attachment-icon";
        icon.textContent = block.type === "image" ? "▧" : "▤";
        icon.setAttribute("aria-hidden", "true");
        const label = document.createElement("span");
        label.className = "attachment-name";
        label.textContent = block.label;
        chip.append(icon, label);
        if (block.state !== "ready") {
          const status = document.createElement("span");
          status.className = "attachment-status";
          status.textContent = block.state === "uploading" ? "上传中…" : "失败";
          chip.append(status);
        }
        if (block.state === "failed" && files.current.has(block.id)) {
          const retry = document.createElement("button");
          retry.type = "button";
          retry.dataset.retry = block.id;
          retry.textContent = "重试";
          retry.setAttribute("aria-label", `重试上传 ${block.label}`);
          retry.disabled = disabled;
          chip.append(retry);
        }
        const remove = document.createElement("button");
        remove.type = "button";
        remove.dataset.remove = block.id;
        remove.textContent = "×";
        remove.setAttribute("aria-label", `删除附件 ${block.label}`);
        remove.disabled = disabled;
        chip.append(remove);
        fragment.append(chip);
      }
      // Chromium needs a final line box to place a caret after a trailing newline.
      // This view-only BR is excluded from serialization and selection offsets.
      if (needsCaretEnd) {
        const end = document.createElement("br");
        end.dataset.caretEnd = "";
        fragment.append(end);
      }
      root.replaceChildren(fragment);
      root.dataset.rendered = JSON.stringify(
        blocks.filter((b) => b.type !== "text"),
      );
      if (selection && pendingCursor.current === null)
        placeSelection(root, selection.start, selection.end);
    }
    root.querySelectorAll("button").forEach((button) => {
      button.disabled = disabled;
    });
    if (pendingCursor.current !== null) {
      root.focus();
      placeSelection(root, pendingCursor.current);
      pendingCursor.current = null;
      rememberCursor();
    }
  }, [blocks, disabled]);

  return (
    <>
      <div
        ref={editor}
        className="inline-editor"
        role="textbox"
        aria-label="消息输入框"
        aria-multiline="true"
        aria-disabled={disabled}
        contentEditable={!disabled}
        suppressContentEditableWarning
        data-placeholder="输入消息，或粘贴图片…"
        onSelect={rememberCursor}
        onBlur={rememberCursor}
        onCompositionStart={() => {
          composing.current = true;
        }}
        onCompositionEnd={() => {
          composing.current = false;
          if (editor.current)
            change(readEditor(editor.current, current.current));
        }}
        onInput={() => {
          if (!composing.current && editor.current) {
            rememberCursor();
            change(readEditor(editor.current, current.current));
          }
        }}
        onPointerDown={(e) => {
          if ((e.target as HTMLElement).closest("button")) e.preventDefault();
        }}
        onClick={(e) => {
          if (disabled) return;
          const target = (e.target as HTMLElement).closest("button");
          if (target?.dataset.retry) {
            const file = files.current.get(target.dataset.retry);
            if (file) void upload(target.dataset.retry, file);
          }
          if (target?.dataset.remove) {
            let offset = 0;
            for (const block of current.current) {
              if (block.id === target.dataset.remove) break;
              offset += block.type === "text" ? block.text.length : 1;
            }
            pendingCursor.current = offset;
            change(replaceDraftRange(current.current, offset, offset + 1, []));
          }
        }}
        onPaste={(e) => {
          e.preventDefault();
          if (disabled) return;
          const selected = Array.from(e.clipboardData.files);
          if (selected.length) add(selected);
          else
            replace([
              {
                id: crypto.randomUUID(),
                type: "text",
                text: e.clipboardData
                  .getData("text/plain")
                  .replace(/\r\n?/g, "\n"),
              },
            ]);
        }}
        onDragOver={(e) => {
          e.preventDefault();
          e.dataTransfer.dropEffect = e.dataTransfer.types.includes("Files")
            ? "copy"
            : "none";
        }}
        onDrop={(e) => {
          e.preventDefault();
          if (disabled || !e.dataTransfer.files.length) return;
          const caret = (
            document as Document & {
              caretRangeFromPoint?: (x: number, y: number) => Range | null;
            }
          ).caretRangeFromPoint?.(e.clientX, e.clientY);
          if (caret && editor.current?.contains(caret.startContainer)) {
            const selection = window.getSelection();
            selection?.removeAllRanges();
            selection?.addRange(caret);
          }
          add(Array.from(e.dataTransfer.files));
        }}
        onKeyDown={(e) => {
          if (disabled || e.nativeEvent.isComposing || composing.current)
            return;
          if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "z") {
            e.preventDefault();
            undo(e.shiftKey);
          } else if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "y") {
            e.preventDefault();
            undo(true);
          } else if (e.key === "Enter") {
            e.preventDefault();
            if (e.ctrlKey || e.metaKey) onSend();
            else
              replace([{ id: crypto.randomUUID(), type: "text", text: "\n" }]);
          }
        }}
      />
      <div className="composer-bottom">
        <div className="composer-options">
          <button
            className="attach-button"
            type="button"
            aria-label="添加图片或文件"
            title="添加图片或文件"
            disabled={!workspace || disabled}
            onMouseDown={(e) => e.preventDefault()}
            onClick={() => {
              rememberCursor();
              picker.current?.click();
            }}
          >
            +
          </button>
          {children}
        </div>
        {sendButton}
      </div>
      <input
        ref={picker}
        type="file"
        multiple
        hidden
        aria-label="选择附件"
        onChange={(e) => {
          add(Array.from(e.target.files ?? []));
          e.target.value = "";
        }}
      />
    </>
  );
}
