export const DEFAULT_SIDEBAR_WIDTH = 246;
export const MIN_SIDEBAR_WIDTH = 220;
export const MAX_SIDEBAR_WIDTH = 520;
export const SIDEBAR_WIDTH_STEP = 16;

export function clampSidebarWidth(value: number): number {
  if (!Number.isFinite(value)) return DEFAULT_SIDEBAR_WIDTH;
  return Math.min(
    MAX_SIDEBAR_WIDTH,
    Math.max(MIN_SIDEBAR_WIDTH, Math.round(value)),
  );
}

export function storedSidebarWidth(value: string | null): number {
  return clampSidebarWidth(
    value === null ? DEFAULT_SIDEBAR_WIDTH : Number(value),
  );
}

export function keyboardSidebarWidth(
  current: number,
  key: string,
): number | null {
  if (key === "ArrowLeft")
    return clampSidebarWidth(current - SIDEBAR_WIDTH_STEP);
  if (key === "ArrowRight")
    return clampSidebarWidth(current + SIDEBAR_WIDTH_STEP);
  if (key === "Home") return MIN_SIDEBAR_WIDTH;
  if (key === "End") return MAX_SIDEBAR_WIDTH;
  return null;
}

export async function loadProjectSessionGroups<T>(
  projectIds: string[],
  archived: boolean,
  load: (projectId: string, archived: boolean) => Promise<T[]>,
): Promise<Record<string, T[]>> {
  const groups = await Promise.all(
    projectIds.map(
      async (projectId) =>
        [projectId, await load(projectId, archived)] as const,
    ),
  );
  return Object.fromEntries(groups);
}
