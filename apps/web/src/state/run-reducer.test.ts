import { describe, expect, it } from "vitest";
import { emptyRun, runReducer, streamText } from "./run-reducer";

const event = (seq: number, type: string, data: unknown) => ({
  type: "event" as const,
  value: {
    schema_version: 1,
    run_id: "run",
    event_id: `e${seq}`,
    type,
    durability: "durable",
    seq,
    timestamp: "2026-09-17T00:00:00Z",
    data,
  },
});
const delta = (chunk: number, text: string) => ({
  type: "event" as const,
  value: {
    run_id: "run",
    durability: "ephemeral",
    seq: null,
    type: "message.delta",
    data: {
      message_id: "message",
      stream_id: "stream",
      chunk_index: chunk,
      text,
    },
  },
});

describe("durable event projection", () => {
  it("deduplicates durable events and refuses to advance across a sequence gap", () => {
    const first = runReducer(
      emptyRun("run"),
      event(1, "run.started", { status: "running", revision: 2 }),
    );
    expect(first.lastAppliedDurableSeq).toBe(1);
    expect(
      runReducer(
        first,
        event(1, "run.started", { status: "queued", revision: 1 }),
      ),
    ).toBe(first);
    const gap = runReducer(
      first,
      event(3, "run.completed", { status: "completed", revision: 3 }),
    );
    expect(gap.needsRecovery).toBe(true);
    expect(gap.serverStatus).toBe("running");
    expect(gap.lastAppliedDurableSeq).toBe(1);
    expect(
      runReducer(
        gap,
        event(2, "run.completed", { status: "completed", revision: 3 }),
      ),
    ).toBe(gap);
  });
  it("recovers using consistent snapshots and does not roll back to an old snapshot", () => {
    const initial = runReducer(
      emptyRun("run"),
      event(1, "run.started", { status: "running", revision: 2 }),
    );
    const recovered = runReducer(initial, {
      type: "snapshot",
      value: {
        run_id: "run",
        status: "waiting_user",
        revision: 4,
        last_seq: 7,
        messages: [{ message_id: "saved", role: "assistant" }],
        interactions: [{ interaction_id: "ask", status: "open" }],
      },
    });
    expect(recovered.lastAppliedDurableSeq).toBe(7);
    expect(recovered.messages.saved).toBeDefined();
    expect(recovered.needsRecovery).toBe(false);
    expect(
      runReducer(recovered, {
        type: "snapshot",
        value: { run_id: "run", last_seq: 2 },
      }),
    ).toBe(recovered);
  });
  it("merges temporary chunks by index without advancing cursor and replaces them with commit", () => {
    let state = runReducer(emptyRun("run"), delta(1, "世界"));
    state = runReducer(state, delta(0, "你好"));
    state = runReducer(state, delta(1, "duplicated"));
    expect(state.lastAppliedDurableSeq).toBe(0);
    expect(streamText(state.streams.stream)).toBe("你好世界");
    state = runReducer(
      state,
      event(1, "message.committed", {
        message_id: "message",
        role: "assistant",
        content_parts: [{ type: "text", text: "最终结果" }],
      }),
    );
    expect(Object.keys(state.streams)).toHaveLength(0);
    expect(state.messages.message).toBeDefined();
    expect(runReducer(state, delta(2, "late"))).toBe(state);
  });
  it("disconnect changes only transient presentation, never server status or cancellation", () => {
    let state = runReducer(
      emptyRun("run"),
      event(1, "run.started", { status: "running", revision: 2 }),
    );
    state = runReducer(state, delta(0, "暂时输出"));
    const disconnected = runReducer(state, { type: "disconnected" });
    expect(disconnected.serverStatus).toBe("running");
    expect(disconnected.streams.stream.interrupted).toBe(true);
    expect(disconnected.lastAppliedDurableSeq).toBe(1);
  });
  it("late revisions cannot reopen resolved interaction or revert run", () => {
    let state = runReducer(
      emptyRun("run"),
      event(1, "interaction.resolved", {
        interaction_id: "ask",
        revision: 4,
        resolution: "accepted",
      }),
    );
    state = runReducer(
      state,
      event(2, "interaction.required", { interaction_id: "ask", revision: 3 }),
    );
    expect(state.interactions.ask.status).toBe("resolved");
    state = runReducer(
      state,
      event(3, "run.completed", { status: "completed", revision: 10 }),
    );
    state = runReducer(
      state,
      event(4, "run.started", { status: "running", revision: 9 }),
    );
    expect(state.serverStatus).toBe("completed");
    expect(state.lastAppliedDurableSeq).toBe(4);
  });
  it("ignores other run identities and prevents ephemeral events from writing state", () => {
    const initial = emptyRun("other");
    expect(
      runReducer(initial, event(1, "run.completed", { status: "completed" })),
    ).toBe(initial);
    const state = emptyRun("run");
    expect(
      runReducer(state, {
        type: "event",
        value: {
          run_id: "run",
          durability: "ephemeral",
          seq: null,
          type: "run.completed",
          data: { status: "completed" },
        },
      }),
    ).toBe(state);
  });
});
