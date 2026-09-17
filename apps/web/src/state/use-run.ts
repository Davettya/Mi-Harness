import { useEffect, useRef, useState } from "react";
import { api } from "../api/client";
import { connectEvents, type Connection } from "../api/event-stream";
import { emptyRun, runReducer, type RunAction } from "./run-reducer";

export function useRun(runId: string) {
  const [projection, setProjection] = useState(emptyRun());
  const current = useRef(projection);
  const [connection, setConnection] = useState<Connection>("disconnected");
  const [error, setError] = useState("");
  const refresh = useRef<() => Promise<void>>(async () => {});
  useEffect(() => {
    const controller = new AbortController();
    current.current = emptyRun(runId);
    setProjection(current.current);
    setError("");
    if (!runId) {
      setConnection("disconnected");
      return () => controller.abort();
    }
    const dispatch = (action: RunAction) => {
      current.current = runReducer(current.current, action);
      if (!controller.signal.aborted) setProjection(current.current);
      return !current.current.needsRecovery;
    };
    const recover = async () => {
      const snapshot = await api.snapshot(runId, controller.signal);
      if (!controller.signal.aborted)
        dispatch({ type: "snapshot", value: snapshot });
    };
    refresh.current = recover;
    void (async () => {
      try {
        await recover();
        if (controller.signal.aborted) return;
        await connectEvents({
          runId,
          signal: controller.signal,
          cursor: () => current.current.lastAppliedDurableSeq,
          onEvent: (value) => {
            const accepted = dispatch({ type: "event", value });
            if (
              ["run.completed", "run.failed", "run.cancelled"].includes(
                String(value.type),
              )
            ) {
              void recover().catch((error) => {
                if (!controller.signal.aborted)
                  setError(
                    error instanceof Error ? error.message : "任务快照刷新失败",
                  );
              });
            }
            return accepted;
          },
          onRecover: recover,
          onConnection: (state, reason) => {
            if (controller.signal.aborted) return;
            setConnection(state);
            setError(reason ?? "");
            if (state === "reconnecting") dispatch({ type: "disconnected" });
          },
        });
      } catch (error) {
        if (!controller.signal.aborted) {
          setError(error instanceof Error ? error.message : "任务读取失败。");
          setConnection("disconnected");
        }
      }
    })();
    return () => controller.abort();
  }, [runId]);
  return { projection, connection, error, refresh: () => refresh.current() };
}
