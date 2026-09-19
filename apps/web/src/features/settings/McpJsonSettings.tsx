import { useEffect, useState } from "react";
import { api, ApiError, Command } from "../../api/client";
import { array, object, string, type ObjectValue } from "../../api/values";
import { ErrorNotice, JsonDetail } from "../../components/common";

export function McpJsonSettings({ onChanged }: { onChanged: () => void }) {
  const [file, setFile] = useState<ObjectValue>({});
  const [text, setText] = useState("");
  const [external, setExternal] = useState<ObjectValue | null>(null);
  const [error, setError] = useState<unknown>();
  const [pending, setPending] = useState(false);
  const [note, setNote] = useState("");
  const [report, setReport] = useState<unknown>();
  const dirty = text !== string(file.text);
  useEffect(() => { let active = true; void api.mcpFile().then(value => { if (active) { setFile(value); setText(string(value.text)); } }).catch(setError); return () => { active = false; }; }, []);
  useEffect(() => {
    let active = true;
    const timer = setInterval(() => { void api.mcpFile().then(value => {
      if (!active) return;
      if (value.hash !== file.hash) setExternal(value);
      else setFile(value);
    }).catch(setError); }, 3000);
    return () => { active = false; clearInterval(timer); };
  }, [file.hash]);
  async function act(action: "validate" | "save" | "reload") {
    setPending(true); setError(null); setNote("");
    try {
      if (action === "validate") { await api.validateMcpFile(text); setNote("语法与配置校验通过；未连接任何 MCP 服务。"); }
      else {
        const result = action === "save" ? await api.saveMcpFile(new Command({ text, expected_hash: file.hash ?? null })) : await api.reloadMcpFile();
        if (action === "reload" && dirty) setExternal(result);
        else { setFile(result); setText(string(result.text)); setExternal(null); }
        setNote(result.load_error ? "文件存在加载错误；继续使用最后有效配置。" : "文件已保存，配置已加载；连接与目录需要单独验证。"); onChanged();
      }
    } catch (error) { setError(error); if (error instanceof ApiError && error.status === 412) setExternal(await api.mcpFile()); }
    finally { setPending(false); }
  }
  return <section><h2>MCP JSON 配置</h2><p><code>{string(file.path)}</code></p>
    <p>文件：{dirty ? "未保存" : "已保存"} · 生效版本：{String(file.revision ?? "未加载")} · hash：{string(file.effective_hash).slice(0, 12)}</p>
    <p>只填写凭据引用。修改配置不授权执行进程或连接服务；启用后仍需单独验证目录。</p>
    <div className="json-editor"><pre aria-hidden="true">{text.split("\n").map((_, i) => i + 1).join("\n")}</pre><textarea aria-label="MCP JSON 原文" spellCheck={false} value={text} disabled={pending} onChange={e => setText(e.target.value)} /></div>
    <button disabled={pending} onClick={() => void act("validate")}>验证语法</button>
    <button disabled={pending} onClick={async () => { try { await api.validateMcpFile(text); setText(JSON.stringify(JSON.parse(text), null, 2) + "\n"); } catch (error) { setError(error); } }}>格式化</button>
    <button disabled={pending || !dirty} onClick={() => void act("save")}>保存并加载</button>
    <button disabled={pending} onClick={() => void act("reload")}>从磁盘重载</button>
    {external && <aside role="status"><p>磁盘内容已变化，网页草稿已保留。核对后可载入磁盘版本，或以该版本为基线继续保存草稿。</p><JsonDetail value={external.text} label="查看磁盘原文" />
      <button onClick={() => { setFile(external); setText(string(external.text)); setExternal(null); }}>载入磁盘版本</button>
      <button onClick={() => { setFile(external); setExternal(null); }}>保留草稿并采用新基线</button></aside>}
    <ErrorNotice error={error} />{Boolean(file.load_error) && <p role="alert">{string(object(file.load_error).message)}</p>}<p role="status">{note}</p>
    {array(file.server_ids).map(value => { const server = string(value); return <div key={server}>{server}
      <button disabled={pending} onClick={async () => { setPending(true); try { setReport(await api.diagnoseMcp(server, new Command({ levels: ["transport", "protocol", "catalog"] }))); } catch (error) { setError(error); } finally { setPending(false); } }}>连接并验证目录</button>
      <button disabled={pending} onClick={async () => { try { await api.revokeMcp(server); setNote(`${server} 已撤销授权，后续调用被阻止。`); } catch (error) { setError(error); } }}>撤销授权</button></div>; })}
    {report !== undefined && <JsonDetail value={report} label="连接与目录验证结果" />}
  </section>;
}
