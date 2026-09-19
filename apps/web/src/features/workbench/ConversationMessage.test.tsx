import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { ConversationMessage } from "./ConversationMessage";

describe("ConversationMessage", () => {
  it("renders tool output collapsed and without a conversation avatar", () => {
    const html = renderToStaticMarkup(
      <ConversationMessage
        message={{
          message_id: "tool-1",
          role: "tool",
          content_parts: [
            {
              type: "text",
              text: JSON.stringify({ summary: "读取完成", count: 2 }),
            },
          ],
        }}
      />,
    );

    expect(html).toContain('<article class="message tool">');
    expect(html).toContain('<details class="tool-message">');
    expect(html).not.toContain("<details open");
    expect(html).toContain("工具调用结果");
    expect(html).toContain("读取完成");
    expect(html).not.toContain("message-avatar");
  });

  it("keeps the avatar and heading for assistant messages", () => {
    const html = renderToStaticMarkup(
      <ConversationMessage
        message={{
          message_id: "assistant-1",
          role: "assistant",
          content_parts: [{ type: "text", text: "完成" }],
        }}
      />,
    );

    expect(html).toContain("message-avatar");
    expect(html).toContain("Mi Harness");
    expect(html).not.toContain("tool-message");
  });
});
