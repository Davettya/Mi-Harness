import { api } from "../../api/client";
import { array, object, string, type ObjectValue } from "../../api/values";
import { MessageContent } from "./MessageContent";

function MessageAttachments({ message }: { message: ObjectValue }) {
  return (
    <>
      {array(message.content_parts)
        .map(object)
        .filter(
          (part) => part.type === "file_reference" || part.type === "image",
        )
        .map((part) => {
          const artifact = object(part.artifact_ref);
          return (
            <a
              className="attachment-link"
              key={string(artifact.artifact_id)}
              href={api.artifactUrl(string(artifact.artifact_id))}
            >
              {string(part.label ?? part.alt, "查看附件")} ↗
            </a>
          );
        })}
    </>
  );
}

export function ConversationMessage({ message }: { message: ObjectValue }) {
  const role = string(message.role);
  const content = (
    <>
      <MessageContent message={message} />
      <MessageAttachments message={message} />
    </>
  );

  if (role === "tool") {
    return (
      <article className="message tool">
        <div className="message-body">
          <details className="tool-message">
            <summary>
              <span className="tool-message-title">工具调用结果</span>
              {message.model_attempt_id != null && <small>已提交</small>}
              <span className="tool-message-action" aria-hidden="true">
                <span className="when-closed">展开</span>
                <span className="when-open">收起</span>
              </span>
            </summary>
            <div className="tool-message-content">{content}</div>
          </details>
        </div>
      </article>
    );
  }

  return (
    <article className={`message ${role}`}>
      <div className="message-avatar">{role === "user" ? "你" : "Mi"}</div>
      <div className="message-body">
        <div className="message-heading">
          {role === "user" ? "你" : "Mi Harness"}
          {message.model_attempt_id != null && <small>已提交</small>}
        </div>
        {content}
      </div>
    </article>
  );
}
