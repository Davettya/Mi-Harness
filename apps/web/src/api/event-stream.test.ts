import { describe, expect, it } from "vitest";
import { SseParser } from "./event-stream";

describe("SSE framing", () => {
  it("accepts partial CRLF frames, comments and multi-line payloads", () => {
    const parser = new SseParser();
    expect(
      parser.push(
        ': heartbeat\r\n\r\nid: run:1\r\nevent: message.committed\r\ndata: {"a":\r',
      ),
    ).toEqual([]);
    expect(parser.push("\ndata: 1}\r\n\r\n")).toEqual([
      { id: "run:1", event: "message.committed", data: '{"a":\n1}' },
    ]);
  });
  it("does not inherit a durable ID into temporary frames", () => {
    const frames = new SseParser().push(
      "id: r:1\nevent: run.started\ndata: {}\n\nevent: message.delta\ndata: {}\n\n",
    );
    expect(frames[0].id).toBe("r:1");
    expect(frames[1].id).toBeUndefined();
  });
  it("bounds incomplete frames to stop an unbounded buffer", () => {
    expect(() => new SseParser().push("x".repeat(2_000_001))).toThrow("超过");
  });
});
