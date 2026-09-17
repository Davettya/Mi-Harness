import { memo } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { safeExternalUrl } from "./common";

/** Render model text without interpreting raw HTML or fetching remote images. */
export const MarkdownText = memo(function MarkdownText({ text }: { text: string }) {
  return (
    <div className="markdown-text">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        urlTransform={(url) => safeExternalUrl(url) ?? ""}
        components={{
          a: ({ href, children, title }) => href ? (
            <a href={href} title={title} target="_blank" rel="noopener noreferrer">{children}</a>
          ) : <span>{children}</span>,
          img: ({ src, alt }) => typeof src === "string" && src ? (
            <a href={src} target="_blank" rel="noopener noreferrer">{alt || "查看图片"} ↗</a>
          ) : <span>{alt || "图片"}</span>,
          table: ({ children }) => <div className="markdown-table"><table>{children}</table></div>,
        }}
      >{text}</ReactMarkdown>
    </div>
  );
});
