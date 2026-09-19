import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import {
  ModelSelector,
  displayModel,
  modelOptionDetail,
  productModels,
  visibleModelSelection,
} from "./ModelSelector";

const demo = {
  profile_ref: "demo@1",
  provider_id: "local-demo",
  model_id: "deterministic-demo-v1",
  capabilities: {},
};
const deepseek = {
  profile_ref: "model-deepseek@3",
  provider_id: "deepseek",
  model_id: "deepseek-flash",
  capabilities: { vision: { status: "verified" } },
};

describe("ModelSelector product presentation", () => {
  it("removes the Demo fixture and never puts profile refs in option details", () => {
    expect(productModels([demo, deepseek])).toEqual([deepseek]);
    expect(modelOptionDetail(deepseek)).toBe("deepseek · 图文");
    expect(modelOptionDetail(deepseek)).not.toContain("model-deepseek@3");
  });

  it("shows the model name for a selected older revision of the same profile", () => {
    expect(displayModel([deepseek], "model-deepseek@2")?.model_id).toBe(
      "deepseek-flash",
    );
    expect(
      visibleModelSelection([demo, deepseek], "demo@1", "model-deepseek@2"),
    ).toBe("model-deepseek@2");

    const html = renderToStaticMarkup(
      <ModelSelector
        models={[demo, deepseek]}
        selected="model-deepseek@2"
        control={{}}
        onChange={() => undefined}
      />,
    );
    expect(html).toContain("deepseek-flash");
    expect(html).not.toContain("model-deepseek@2");
    expect(html).not.toContain("deterministic-demo-v1");
  });
});
