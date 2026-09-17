import {
  array,
  id,
  number,
  object,
  string,
  type ObjectValue,
} from "../api/values";

export interface TransientStream {
  messageId: string;
  streamId: string;
  chunks: Record<number, string>;
  lastChunk: number;
  interrupted: boolean;
}
export interface RunProjection {
  runId: string;
  snapshotRevision: number;
  lastAppliedDurableSeq: number;
  serverStatus: string;
  messages: Record<string, ObjectValue>;
  tools: Record<string, ObjectValue>;
  interactions: Record<string, ObjectValue>;
  artifacts: Record<string, ObjectValue>;
  children: Record<string, ObjectValue>;
  streams: Record<string, TransientStream>;
  usage: ObjectValue;
  context: ObjectValue;
  snapshot: ObjectValue;
  needsRecovery: boolean;
  updatedAt: string;
}
export const emptyRun = (runId = ""): RunProjection => ({
  runId,
  snapshotRevision: 0,
  lastAppliedDurableSeq: 0,
  serverStatus: "",
  messages: {},
  tools: {},
  interactions: {},
  artifacts: {},
  children: {},
  streams: {},
  usage: {},
  context: {},
  snapshot: {},
  needsRecovery: false,
  updatedAt: "",
});
const indexed = (value: unknown, key: string): Record<string, ObjectValue> =>
  Object.fromEntries(
    array(value)
      .map(object)
      .filter((v) => string(v[key]) || id(v))
      .map((v) => [string(v[key]) || id(v), v]),
  );
const upsert = (
  current: Record<string, ObjectValue>,
  key: string,
  value: ObjectValue,
) => {
  const previous = current[key];
  if (
    previous &&
    typeof value.revision === "number" &&
    number(previous.revision) > value.revision
  )
    return current;
  return { ...current, [key]: { ...previous, ...value } };
};
export type RunAction =
  | { type: "snapshot"; value: ObjectValue }
  | { type: "event"; value: ObjectValue }
  | { type: "disconnected" };

export function runReducer(
  state: RunProjection,
  action: RunAction,
): RunProjection {
  if (action.type === "disconnected")
    return {
      ...state,
      streams: Object.fromEntries(
        Object.entries(state.streams).map(([key, value]) => [
          key,
          { ...value, interrupted: true },
        ]),
      ),
    };
  const value = action.value;
  if (action.type === "snapshot") {
    if (
      state.runId === value.run_id &&
      number(value.last_seq) < state.lastAppliedDurableSeq
    )
      return state;
    return {
      ...emptyRun(string(value.run_id)),
      snapshot: value,
      snapshotRevision: number(value.revision),
      lastAppliedDurableSeq: number(value.last_seq),
      serverStatus: string(value.status, "queued"),
      messages: indexed(value.messages, "message_id"),
      tools: indexed(value.tools, "operation_id"),
      interactions: indexed(value.interactions, "interaction_id"),
      artifacts: indexed(value.artifacts, "artifact_id"),
      children: indexed(value.children, "child_run_id"),
      usage: object(value.usage_summary),
      context: object(value.context_summary),
      updatedAt: string(value.updated_at),
    };
  }
  if (value.run_id !== state.runId || state.needsRecovery) return state;
  const eventType = string(value.type);
  const payload = object(value.data);
  if (value.durability === "ephemeral") {
    if (
      eventType !== "message.delta" ||
      value.seq !== null ||
      state.messages[string(payload.message_id)]
    )
      return state;
    const streamId = string(payload.stream_id);
    const chunk = number(payload.chunk_index, -1);
    if (!streamId || chunk < 0 || !Number.isInteger(chunk)) return state;
    const old = state.streams[streamId];
    if (old?.chunks[chunk] !== undefined) return state;
    return {
      ...state,
      streams: {
        ...state.streams,
        [streamId]: {
          messageId: string(payload.message_id),
          streamId,
          chunks: { ...old?.chunks, [chunk]: string(payload.text) },
          lastChunk: Math.max(old?.lastChunk ?? -1, chunk),
          interrupted: old?.interrupted ?? false,
        },
      },
    };
  }
  const seq = number(value.seq, -1);
  if (
    value.durability !== "durable" ||
    !Number.isInteger(seq) ||
    seq <= state.lastAppliedDurableSeq
  )
    return state;
  if (seq !== state.lastAppliedDurableSeq + 1)
    return { ...state, needsRecovery: true };
  let next = {
    ...state,
    lastAppliedDurableSeq: seq,
    updatedAt: string(value.timestamp),
  };
  if (
    eventType.startsWith("run.") &&
    number(payload.revision) >= state.snapshotRevision
  )
    next = {
      ...next,
      snapshotRevision: number(payload.revision),
      serverStatus: string(payload.status, state.serverStatus),
    };
  if (eventType === "message.committed")
    next = {
      ...next,
      messages: upsert(state.messages, string(payload.message_id), payload),
      streams: Object.fromEntries(
        Object.entries(state.streams).filter(
          ([, stream]) => stream.messageId !== payload.message_id,
        ),
      ),
    };
  if (eventType.startsWith("tool."))
    next.tools = upsert(state.tools, string(payload.operation_id), {
      ...payload,
      event_type: eventType,
    });
  if (eventType === "interaction.required")
    next.interactions = upsert(
      state.interactions,
      string(payload.interaction_id),
      { ...payload, status: "open" },
    );
  if (eventType === "interaction.resolved")
    next.interactions = upsert(
      state.interactions,
      string(payload.interaction_id),
      { ...payload, status: "resolved" },
    );
  if (eventType === "artifact.created") {
    const artifact = object(payload.artifact_ref);
    next.artifacts = upsert(state.artifacts, string(artifact.artifact_id), {
      ...artifact,
      display_name: payload.display_name,
      operation_id: payload.operation_id,
    });
  }
  if (eventType.startsWith("child."))
    next.children = upsert(
      state.children,
      string(payload.child_run_id),
      payload,
    );
  if (eventType === "context.compacted")
    next.context = { ...state.context, compaction: payload };
  if (
    eventType === "usage.updated" &&
    number(payload.revision) >= number(state.usage.revision)
  )
    next.usage = payload;
  return next;
}

export const streamText = (stream: TransientStream) =>
  Object.keys(stream.chunks)
    .map(Number)
    .sort((a, b) => a - b)
    .map((key) => stream.chunks[key])
    .join("");
export const statusLabels: Record<string, string> = {
  "": "正在读取状态",
  queued: "排队中",
  running: "运行中",
  waiting_user: "等待确认",
  waiting_children: "等待子任务",
  recovering: "正在恢复",
  needs_review: "需要核对",
  cancelling: "正在取消",
  completed: "已完成",
  failed: "运行失败",
  cancelled: "已取消",
};
export const terminal = (status: string) =>
  ["completed", "failed", "cancelled"].includes(status);
