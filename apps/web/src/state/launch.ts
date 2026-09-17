import { api } from "../api/client";

// Consume before rendering; share the promise across React StrictMode effects.
let connection: Promise<void> | undefined;
export function connectLaunch(): Promise<void> {
  if (connection) return connection;
  const ticket = new URLSearchParams(window.location.hash.slice(1)).get(
    "launch",
  );
  if (!ticket) return Promise.resolve();
  window.history.replaceState(
    null,
    "",
    window.location.pathname + window.location.search,
  );
  connection = api
    .exchange(ticket)
    .then(() => undefined)
    .catch((error) => {
      connection = undefined;
      throw error;
    });
  return connection;
}
