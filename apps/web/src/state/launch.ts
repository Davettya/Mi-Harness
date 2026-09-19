import { api } from "../api/client";
import { ApiError } from "../api/client";
import type { AuthSessionView } from "../api/types";

// Consume launch credentials and local auto-pairing once across React StrictMode effects.
let connection: Promise<AuthSessionView> | undefined;
export function connectLaunch(): Promise<AuthSessionView> {
  if (connection) return connection;
  const ticket = new URLSearchParams(window.location.hash.slice(1)).get(
    "launch",
  );
  if (ticket)
    window.history.replaceState(
      null,
      "",
      window.location.pathname + window.location.search,
    );
  connection = (async () => {
    if (ticket) {
      try {
        await api.exchange(ticket);
      } catch (error) {
        if (!(error instanceof ApiError && error.status === 401)) throw error;
      }
    }
    try {
      return await api.authSession();
    } catch (error) {
      if (!(error instanceof ApiError && error.status === 401)) throw error;
      await api.localPair();
      return api.authSession();
    }
  })().finally(() => {
      connection = undefined;
    });
  return connection;
}
