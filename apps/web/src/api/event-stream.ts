import { api, ApiError } from "./client";
import { object, string, type ObjectValue } from "./values";

export interface SseFrame {
  event: string;
  data: string;
  id?: string;
}
/** Incremental SSE parser: supports fragmented UTF-8 via TextDecoder upstream, CRLF, comments and multi-line data. */
export class SseParser {
  private buffer = "";
  push(chunk: string): SseFrame[] {
    this.buffer += chunk;
    const frames: SseFrame[] = [];
    let match: RegExpExecArray | null;
    while ((match = /\r?\n\r?\n/.exec(this.buffer))) {
      const raw = this.buffer.slice(0, match.index);
      this.buffer = this.buffer.slice(match.index + match[0].length);
      let event = "message";
      let eventId: string | undefined;
      const data: string[] = [];
      for (const line of raw.split(/\r?\n/)) {
        if (line.startsWith(":")) continue;
        const colon = line.indexOf(":");
        const name = colon === -1 ? line : line.slice(0, colon);
        const value =
          colon === -1 ? "" : line.slice(colon + 1).replace(/^ /, "");
        if (name === "data") data.push(value);
        if (name === "event") event = value;
        if (name === "id" && !value.includes("\0")) eventId = value;
      }
      if (data.length)
        frames.push({ event, data: data.join("\n"), id: eventId });
    }
    if (this.buffer.length > 2_000_000)
      throw new Error("事件数据超过客户端上限，需要重新读取快照。");
    return frames;
  }
}

export type Connection =
  | "connecting"
  | "connected"
  | "reconnecting"
  | "disconnected";
type StreamOptions = {
  runId: string;
  signal: AbortSignal;
  cursor: () => number;
  onEvent: (event: ObjectValue) => boolean;
  onRecover: () => Promise<void>;
  onConnection: (connection: Connection, reason?: string) => void;
};
function delay(ms: number, signal: AbortSignal) {
  return new Promise<void>((resolve) => {
    const done = () => {
      clearTimeout(timer);
      signal.removeEventListener("abort", done);
      resolve();
    };
    const timer = setTimeout(done, ms);
    signal.addEventListener("abort", done, { once: true });
  });
}

export async function connectEvents(options: StreamOptions): Promise<void> {
  let attempts = 0;
  let first = true;
  while (!options.signal.aborted) {
    try {
      options.onConnection(first ? "connecting" : "reconnecting");
      const cursor = options.cursor();
      const headers = new Headers({ Accept: "text/event-stream" });
      if (!first) headers.set("Last-Event-ID", `${options.runId}:${cursor}`);
      const response = await fetch(
        api.eventUrl(options.runId) + (first ? `?after_seq=${cursor}` : ""),
        { headers, credentials: "same-origin", signal: options.signal },
      );
      first = false;
      if (!response.ok) {
        const body = object(await response.json().catch(() => ({})));
        if (
          response.status === 409 &&
          ["CURSOR_EXPIRED", "INVALID_CURSOR"].includes(string(body.code))
        ) {
          await options.onRecover();
          continue;
        }
        throw new ApiError(
          response.status,
          string(body.code),
          string(body.message, `事件连接失败 (${response.status})`),
        );
      }
      if (!response.body) throw new Error("浏览器未提供可读事件流。");
      options.onConnection("connected");
      attempts = 0;
      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      const parser = new SseParser();
      let recover = false;
      try {
        while (!options.signal.aborted) {
          const { value, done } = await reader.read();
          if (done) break;
          for (const frame of parser.push(
            decoder.decode(value, { stream: true }),
          )) {
            const event = object(JSON.parse(frame.data));
            if (event.type !== frame.event || event.run_id !== options.runId)
              throw new Error("事件信封与流标识不一致。");
            if (
              event.durability === "durable" &&
              frame.id !== `${options.runId}:${event.seq}`
            )
              throw new Error("耐久事件游标不一致。");
            if (event.durability === "ephemeral" && frame.id !== undefined)
              throw new Error("临时事件不能携带耐久游标。");
            if (!options.onEvent(event)) {
              recover = true;
              break;
            }
          }
          if (recover) break;
        }
      } finally {
        await reader.cancel().catch(() => undefined);
        reader.releaseLock();
      }
      if (recover) {
        await options.onRecover();
        continue;
      }
      if (!options.signal.aborted)
        options.onConnection(
          "reconnecting",
          "连接已断开，正在补读已提交事件。",
        );
    } catch (error) {
      if (options.signal.aborted) break;
      if (
        error instanceof ApiError &&
        [401, 403, 404, 422].includes(error.status)
      ) {
        options.onConnection("disconnected", error.message);
        return;
      }
      options.onConnection(
        "reconnecting",
        error instanceof Error ? error.message : "事件连接中断。",
      );
    }
    await delay(Math.min(1_000 * 2 ** attempts++, 15_000), options.signal);
  }
}
