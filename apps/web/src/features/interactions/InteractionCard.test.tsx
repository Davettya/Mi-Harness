import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { SchemaFields } from "./InteractionCard";

describe("SchemaFields", () => {
  it("renders a const true confirmation as a constrained boolean choice", () => {
    const html = renderToStaticMarkup(
      <SchemaFields
        schema={{
          type: "object",
          properties: { continue: { const: true } },
          required: ["continue"],
        }}
        value={{}}
        onChange={() => undefined}
        disabled={false}
      />,
    );

    expect(html).toContain("确认继续");
    expect(html).toContain('<option value="true">是</option>');
    expect(html).not.toContain('<option value="false">否</option>');
    expect(html).not.toContain('type="text"');
  });
});
