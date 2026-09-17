/** UI narrowing helpers. These are view projections, never replacement transport schemas. */
export type ObjectValue = Record<string, unknown>;
export const object = (value: unknown): ObjectValue =>
  value !== null && typeof value === "object" && !Array.isArray(value)
    ? (value as ObjectValue)
    : {};
export const array = (value: unknown): unknown[] =>
  Array.isArray(value) ? value : [];
export const string = (value: unknown, fallback = ""): string =>
  typeof value === "string" ? value : fallback;
export const number = (value: unknown, fallback = 0): number =>
  typeof value === "number" ? value : fallback;
export const id = (value: unknown): string => {
  const v = object(value);
  return string(
    v.id ??
      v.run_id ??
      v.session_id ??
      v.workspace_id ??
      v.agent_spec_id ??
      v.skill_id ??
      v.memory_id,
  );
};
export const items = (value: unknown): ObjectValue[] =>
  array(object(value).items ?? value).map(object);
export const pretty = (value: unknown): string =>
  JSON.stringify(value, null, 2) ?? "";
export const textParts = (value: unknown): string =>
  array(value)
    .map((part) => string(object(part).text))
    .filter(Boolean)
    .join("\n");
export const timestamp = (value: unknown): string =>
  value ? new Date(string(value)).toLocaleString() : "尚未更新";
