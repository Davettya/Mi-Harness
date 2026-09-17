import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { MarkdownText } from "./MarkdownText";

const render = (text: string) => renderToStaticMarkup(<MarkdownText text={text} />);

describe("model Markdown rendering", () => {
  it("renders headings, emphasis, lists, quotes and fenced code", () => {
    const html = render('# 标题\n\n**重点**和`inline`\n\n1. 步骤\n2. 下一步\n\n> 引用\n\n```python\nprint("<safe>")\n```');
    expect(html).toContain('<h1>标题</h1>');
    expect(html).toContain('<strong>重点</strong>');
    expect(html).toContain('<code>inline</code>');
    expect(html).toContain('<ol>');
    expect(html).toContain('<blockquote>');
    expect(html).toContain('class="language-python"');
    expect(html).toContain('&lt;safe&gt;');
  });
  it("supports GFM tables, task lists and strikethrough", () => {
    const html = render('| 项目 | 状态 |\n| --- | --- |\n| Markdown | 完成 |\n\n- [x] 完成\n- [ ] 待办\n\n~~旧文字~~');
    expect(html).toContain('<table>');
    expect(html).toContain('<th>项目</th>');
    expect(html).toContain('type="checkbox"');
    expect(html).toContain('disabled=""');
    expect(html).toContain('<del>旧文字</del>');
  });
  it("keeps raw HTML inert and rejects active or local navigation URLs", () => {
    const html = render('<script>alert(1)</script>\n\n<img src=x onerror=alert(1)>\n\n[bad](javascript:alert%281%29) [data](data:text/html,test) [local](/api/auth/logout)');
    expect(html).not.toContain('<script');
    expect(html).not.toContain('<img');
    expect(html).not.toContain('href=');
    expect(html).toContain('bad');
  });
  it("opens web links safely and makes images opt-in links", () => {
    const html = render('[来源](https://example.com/reference)\n\n![示意图](https://example.com/tracker.png)');
    expect(html).toContain('href="https://example.com/reference"');
    expect(html).toContain('rel="noopener noreferrer"');
    expect(html).toContain('target="_blank"');
    expect(html).toContain('示意图');
    expect(html).not.toContain('<img');
  });
  it("renders incomplete streaming code fences and completed output consistently", () => {
    const partial = render('```js\nconst x = 1;');
    const complete = render('```js\nconst x = 1;\n```');
    expect(partial).toContain('<pre><code class="language-js">const x = 1;');
    expect(complete).toBe(partial);
  });
});
