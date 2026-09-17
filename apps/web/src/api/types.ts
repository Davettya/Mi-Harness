import type { components, paths } from "./generated/schema";

// Regenerated from the FastAPI application; no independently maintained HTTP DTOs.
export type ApiPath = keyof paths;
export type ArtifactRef = components["schemas"]["ArtifactRef"];
export type SubmitRunInput = components["schemas"]["SubmitRunInput"];
export type SubmitReceipt = components["schemas"]["SubmitReceipt"];
export type RunView = components["schemas"]["RunView"];
export type InteractionResponse = components["schemas"]["InteractionResponse"];
export type RunStatus = components["schemas"]["RunStatus"];
export type AuthSessionView = components["schemas"]["AuthSessionView"];
export type WorkspaceInput = components["schemas"]["WorkspaceInput"];
export type SessionInput = components["schemas"]["SessionInput"];
export type ConfigInput = components["schemas"]["ConfigInput"];
export type MessageView = components["schemas"]["MessageView"];
