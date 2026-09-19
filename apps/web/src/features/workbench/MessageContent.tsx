import { useState } from "react";
import { api } from "../../api/client";
import { array, object, string, textParts, type ObjectValue } from "../../api/values";
import { SafeText, JsonDetail } from "../../components/common";
import { MarkdownText } from "../../components/MarkdownText";

function ImagePart({ part }: { part: ObjectValue }) {
  const [failed, setFailed] = useState(false);
  const ref = object(part.artifact_ref);
  const alt = string(part.alt, "用户上传的图片");
  return failed ? <span role="img" aria-label={alt}>图片不可用：{alt}</span> :
    <a href={api.artifactUrl(string(ref.artifact_id), true)} target="_blank" rel="noreferrer"><img className="message-image" src={api.artifactUrl(string(ref.artifact_id), true)} alt={alt} onError={() => setFailed(true)} /></a>;
}
export function MessageContent({ message }: { message: ObjectValue }) {
  if (message.role === "tool") {
    try { const data = object(JSON.parse(textParts(message.content_parts))); if (data.summary) return <><SafeText text={string(data.summary)} /><JsonDetail value={data} label="完整工具结果" /></>; } catch { /* inert text */ }
  }
  return <>{array(message.content_parts).map((value, index) => {
    const part = object(value);
    if (part.type === "image" && part.artifact_ref) return <ImagePart key={index} part={part} />;
    if (part.type === "file_reference") return <a key={index} href={api.artifactUrl(string(object(part.artifact_ref).artifact_id))}>{string(part.label, "附件")}</a>;
    return message.role === "assistant" ? <MarkdownText key={index} text={string(part.text)} /> : <SafeText key={index} text={string(part.text)} />;
  })}{Boolean(message.profile_ref) && <small className="message-model">实际模型：{string(message.profile_ref)}</small>}</>;
}
