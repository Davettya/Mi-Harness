import { useEffect, useRef, useState, type FormEvent } from "react";
import { api, ApiError, Command } from "../../api/client";
import type { ArtifactRef, SubmitRunInput } from "../../api/types";
import {
  array,
  id,
  items,
  number,
  object,
  string,
  textParts,
  timestamp,
  type ObjectValue,
} from "../../api/values";
import {
  Empty,
  ErrorNotice,
  JsonDetail,
  Modal,
  Notice,
  SafeText,
  Status,
} from "../../components/common";
import { InteractionCard } from "../interactions/InteractionCard";
import { Inspector, type Panel } from "../inspectors/Inspector";
import { Settings } from "../settings/Settings";
import { connectLaunch } from "../../state/launch";
import { MarkdownText } from "../../components/MarkdownText";
import { useRun } from "../../state/use-run";
import { streamText, terminal } from "../../state/run-reducer";

function savedSelection() {
  try {
    return object(JSON.parse(localStorage.getItem("harness.selection") || "{}"));
  } catch {
    return {};
  }
}

function MessageContent({ message }: { message: ObjectValue }) {
  const text = textParts(message.content_parts);
  if (message.role === "tool") {
    try {
      const value = object(JSON.parse(text));
      if (value.summary)
        return (
          <>
            <SafeText text={string(value.summary)} />
            <JsonDetail value={value} label="完整工具结果" />
          </>
        );
    } catch {
      /* Plain text tool output stays inert. */
    }
  }
  return message.role === "assistant" ? <MarkdownText text={text} /> : <SafeText text={text} />;
}

function Pairing({ onPaired }: { onPaired: () => void }) {
  const [ticket, setTicket] = useState("");
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<unknown>();
  return (
    <main className="pairing">
      <section>
        <div className="brand-mark">
          h<span>·</span>
        </div>
        <div className="eyebrow">YOUR LOCAL AGENT WORKSPACE</div>
        <h1>让想法开始行动。</h1>
        <p>
          请通过启动脚本自动连接。需要手动连接时，可使用 harness pair
          生成备用口令。
        </p>
        <form
          onSubmit={async (event) => {
            event.preventDefault();
            setPending(true);
            setError(null);
            try {
              await api.exchange(ticket);
              setTicket("");
              onPaired();
            } catch (error) {
              setError(error);
            } finally {
              setPending(false);
            }
          }}
        >
          <label>
            一次性配对口令
            <input
              autoFocus
              type="password"
              autoComplete="off"
              value={ticket}
              onChange={(event) => setTicket(event.target.value)}
              required
              placeholder="粘贴本机显示的口令"
            />
          </label>
          <ErrorNotice error={error} />
          <button className="primary" disabled={pending}>
            {pending ? "正在配对…" : "连接工作台 →"}
          </button>
        </form>
        <small>也可重新运行启动脚本，自动打开并连接工作台。</small>
      </section>
      <aside>
        <span className="pairing-orbit orbit-a" />
        <span className="pairing-orbit orbit-b" />
        <div className="pairing-message">
          A little direction.
          <br />
          <em>A lot of possibility.</em>
          <p>从一个目标，到看得见的成果。</p>
        </div>
      </aside>
    </main>
  );
}

export function App() {
  const [auth, setAuth] = useState<ObjectValue | null>(null);
  const [authLoading, setAuthLoading] = useState(true);
  const [authError, setAuthError] = useState<unknown>();
  const [settings, setSettings] = useState(false);
  const [workspaces, setWorkspaces] = useState<ObjectValue[]>([]);
  const [workspaceId, setWorkspaceId] = useState<string>(
    () => string(savedSelection().project),
  );
  const [projectSessions, setProjectSessions] = useState<
    Record<string, ObjectValue[]>
  >({});
  const [collapsed, setCollapsed] = useState<Record<string, boolean>>({});
  const [sessionId, setSessionId] = useState<string>(
    () => string(savedSelection().session),
  );
  const [session, setSession] = useState<ObjectValue>({});
  const [runId, setRunId] = useState("");
  const [branchId, setBranchId] = useState("");
  const [agents, setAgents] = useState<ObjectValue[]>([]);
  const [agentId, setAgentId] = useState("default");
  const [newSessionAgent, setNewSessionAgent] = useState("default");
  const [skills, setSkills] = useState<ObjectValue[]>([]);
  const [selectedSkills, setSelectedSkills] = useState<string[]>([]);
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const [attachments, setAttachments] = useState<Record<string, ObjectValue[]>>(
    {},
  );
  const [pending, setPending] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [error, setError] = useState<unknown>();
  const [receipt, setReceipt] = useState("");
  const [retry, setRetry] = useState<Command<SubmitRunInput> | null>(null);
  const [panel, setPanel] = useState<Panel | null>(null);
  const [navigationOpen, setNavigationOpen] = useState(false);
  const [archived, setArchived] = useState(() => savedSelection().archived === true);
  const [modal, setModal] = useState<
    "workspace" | "rename" | "branch" | "steer" | null
  >(null);
  const [modalText, setModalText] = useState("");
  const [rootPaths, setRootPaths] = useState<string[]>([""]);
  const [editingProject, setEditingProject] = useState<ObjectValue | null>(
    null,
  );
  const [pickingFolder, setPickingFolder] = useState(false);
  useEffect(() => {
    if (workspaceId) {
      try {
        localStorage.setItem(
          "harness.selection",
          JSON.stringify({ project: workspaceId, session: sessionId, archived }),
        );
      } catch {
        /* Browser storage is optional. */
      }
    }
  }, [workspaceId, sessionId, archived]);
  const [messageLimit, setMessageLimit] = useState(100);
  useEffect(() => setMessageLimit(100), [runId]);
  const conversationRef = useRef<HTMLDivElement>(null);
  const followLatest = useRef(true);
  useEffect(() => {
    followLatest.current = true;
  }, [runId]);
  const {
    projection: run,
    connection,
    error: connectionError,
    refresh: refreshRun,
  } = useRun(runId);
  const capabilities = object(auth?.capabilities);
  useEffect(() => {
    const element = conversationRef.current;
    if (element && followLatest.current)
      element.scrollTop = element.scrollHeight;
  }, [run.messages, run.streams, run.interactions]);
  const fileInput = useRef<HTMLInputElement>(null);
  const sessionRequest = useRef(0);
  const composerKey = sessionId || `workspace:${workspaceId}`;
  const draft = drafts[composerKey] ?? "";
  const attached = attachments[composerKey] ?? [];
  const currentAgent = agents.find((value) => id(value) === agentId);
  const branches = array(session.branches).map(object);
  const currentBranch =
    branches.find(
      (branch) => id(branch) === branchId || branch.branch_id === branchId,
    ) ?? object(session.branch ?? session.default_branch);
  const runList = array(session.runs).map(object);
  const selectRun = (nextRunId: string) => {
    setRunId(nextRunId);
    const selectedRun = runList.find((value) => id(value) === nextRunId);
    if (selectedRun?.branch_id) setBranchId(string(selectedRun.branch_id));
  };
  const setDraft = (text: string) => {
    setDrafts((previous) => ({ ...previous, [composerKey]: text }));
    setRetry(null);
  };

  async function authenticate() {
    setAuthLoading(true);
    setAuthError(null);
    try {
      await connectLaunch();
      setAuth(await api.authSession());
    } catch (error) {
      if (!(error instanceof ApiError && error.status === 401))
        setAuthError(error);
      setAuth(null);
    } finally {
      setAuthLoading(false);
    }
  }
  useEffect(() => {
    void authenticate();
  }, []);
  async function loadCatalogs() {
    const results = await Promise.allSettled([
      api.workspaces(),
      api.agents(),
      api.skills(),
    ]);
    results.forEach((result, index) => {
      if (result.status === "rejected") {
        setError(result.reason);
        return;
      }
      if (index === 0) {
        const values = items(result.value);
        setWorkspaces(values);
        setWorkspaceId((previous) =>
          values.some((value) => id(value) === previous)
            ? previous
            : id(values[0]),
        );
      }
      if (index === 1) setAgents(items(result.value));
      if (index === 2) setSkills(items(result.value));
    });
  }
  useEffect(() => {
    if (auth) void loadCatalogs();
  }, [auth]);
  const refreshSessions = async () => {
    const groups = await Promise.all(
      workspaces.map(
        async (project) =>
          [
            id(project),
            items(await api.sessions(id(project), archived)),
          ] as const,
      ),
    );
    setProjectSessions(Object.fromEntries(groups));
  };
  useEffect(() => {
    let active = true;
    void Promise.all(
      workspaces.map(
        async (project) =>
          [
            id(project),
            items(await api.sessions(id(project), archived)),
          ] as const,
      ),
    )
      .then((groups) => {
        if (active) setProjectSessions(Object.fromEntries(groups));
      })
      .catch((error) => {
        if (active) setError(error);
      });
    return () => {
      active = false;
    };
  }, [workspaces, archived]);
  function selectConversation(project: string, conversation: string) {
    if (project === workspaceId && conversation === sessionId) return;
    ++sessionRequest.current;
    setWorkspaceId(project);
    setSessionId(conversation);
    setSession({});
    setRunId("");
    setBranchId("");
    setNavigationOpen(false);
  }
  async function startSession(project: string) {
    setPending(true);
    setError(null);
    try {
      const value = await api.createSession(
        new Command({
          workspace_id: project,
          agent_spec_id: newSessionAgent,
          title: "新会话",
        }),
      );
      setArchived(false);
      setCollapsed((previous) => ({ ...previous, [project]: false }));
      selectConversation(project, id(value));
      setProjectSessions((previous) => ({
        ...previous,
        [project]: [value, ...(previous[project] || [])],
      }));
    } catch (error) {
      setError(error);
    } finally {
      setPending(false);
    }
  }
  function editProject(project?: ObjectValue) {
    setEditingProject(project || null);
    setRootPaths(
      project ? array(project.roots).map((value) => string(value)) : [""],
    );
    openModal("workspace", project ? string(project.name) : "");
  }
  async function chooseFolder() {
    setPickingFolder(true);
    setError(null);
    try {
      const result = await api.chooseFolder();
      if (result.path) {
        const path = result.path;
        setRootPaths((previous) => [
          ...new Set([...previous.filter((value) => value.trim()), path]),
        ]);
        setModalText(
          (previous) =>
            previous || path.split(/[\\/]/).filter(Boolean).pop() || "新项目",
        );
      }
    } catch (error) {
      setError(error);
    } finally {
      setPickingFolder(false);
    }
  }
  async function loadSession(target: string, selectLatest = false) {
    const requestId = ++sessionRequest.current;
    const value = await api.session(target);
    if (requestId !== sessionRequest.current) return;
    setSession(value);
    setAgentId(
      string(
        value.agent_spec_id ?? object(value.session).agent_spec_id,
        "default",
      ),
    );
    const defaultBranch = object(value.branch ?? value.default_branch);
    const available = array(value.branches).map(object);
    const initialBranch =
      string(defaultBranch.branch_id, id(defaultBranch)) ||
      string(available[0]?.branch_id, id(available[0]));
    setBranchId((previous) =>
      available.some(
        (branch) => string(branch.branch_id, id(branch)) === previous,
      )
        ? previous
        : initialBranch,
    );
    if (selectLatest) {
      const runs = array(value.runs).map(object);
      const latestId = string(value.active_run_id) || id(runs[runs.length - 1]);
      setRunId(latestId);
      const latest = runs.find((run) => id(run) === latestId);
      if (latest?.branch_id) setBranchId(string(latest.branch_id));
    }
  }
  useEffect(() => {
    setReceipt("");
    setError(null);
    setRetry(null);
    if (auth && sessionId) void loadSession(sessionId, true).catch(setError);
    else {
      ++sessionRequest.current;
      setSession({});
      setRunId("");
    }
    return () => {
      ++sessionRequest.current;
    };
  }, [sessionId, auth]);
  useEffect(() => {
    if (runId && sessionId && terminal(run.serverStatus)) {
      void loadSession(sessionId).catch(setError);
      void refreshSessions().catch(setError);
    }
  }, [runId, run.serverStatus]);

  async function createSession() {
    if (!workspaceId) {
      setModal("workspace");
      return "";
    }
    const value = await api.createSession(
      new Command({
        workspace_id: workspaceId,
        agent_spec_id: agentId,
        title: draft.slice(0, 48) || "新会话",
      }),
    );
    const newId = string(
      value.session_id,
      id(object(value.session)) || id(value),
    );
    setDrafts((previous) => ({ ...previous, [newId]: draft }));
    setAttachments((previous) => ({ ...previous, [newId]: attached }));
    setSessionId(newId);
    await refreshSessions();
    return newId;
  }
  async function send(command?: Command<SubmitRunInput>) {
    if (!draft.trim() && !attached.length && !command) return;
    setPending(true);
    setError(null);
    setReceipt("");
    try {
      const targetSession = sessionId || (await createSession());
      if (!targetSession) return;
      if (
        sessionId &&
        string(session.title) === "新会话" &&
        runList.length === 0 &&
        draft.trim()
      ) {
        await api.updateSession(
          sessionId,
          new Command({
            title: draft.trim().slice(0, 48),
            expected_revision: session.revision,
          }),
        );
      }
      let targetBranch = branchId;
      let revision = number(currentBranch.revision, 1);
      if (!targetBranch || !sessionId) {
        const latest = await api.session(targetSession);
        const branch = object(
          latest.branch ?? latest.default_branch ?? array(latest.branches)[0],
        );
        targetBranch = string(branch.branch_id, id(branch));
        revision = number(branch.revision, 1);
      }
      const next =
        command ??
        new Command<SubmitRunInput>({
          branch_id: targetBranch,
          expected_branch_revision: revision,
          agent_spec_revision: number(currentAgent?.revision, 1),
          content_parts: draft.trim() ? [{ type: "text", text: draft }] : [],
          attachment_refs: attached.map((artifact) => {
            const { display_name: _name, ...reference } = artifact;
            return reference as ArtifactRef;
          }),
          selected_skill_refs: selectedSkills.map((skillId) => {
            const skill = skills.find((value) => id(value) === skillId);
            return {
              skill_id: skillId,
              content_hash: string(skill?.content_hash ?? skill?.hash),
            };
          }),
        });
      setRetry(next);
      const result = await api.submit(targetSession, next);
      setRunId(string(result.run_id));
      setReceipt("任务已由服务端受理。");
      setDrafts((previous) => ({
        ...previous,
        [composerKey]: "",
        [targetSession]: "",
      }));
      setAttachments((previous) => ({
        ...previous,
        [composerKey]: [],
        [targetSession]: [],
      }));
      setRetry(null);
      await loadSession(targetSession);
      await refreshSessions();
    } catch (error) {
      setError(error);
      if (error instanceof ApiError && error.status === 409) {
        setRetry(null);
        if (sessionId) await loadSession(sessionId);
        setReceipt("已读取最新会话版本，草稿保留。请核对后重新提交。");
      }
    } finally {
      setPending(false);
    }
  }
  async function upload(files: FileList | null) {
    if (!files || !workspaceId) return;
    setUploading(true);
    setError(null);
    try {
      for (const file of Array.from(files)) {
        const reference = await api.upload(
          workspaceId,
          file,
          crypto.randomUUID(),
        );
        setAttachments((previous) => ({
          ...previous,
          [composerKey]: [
            ...(previous[composerKey] ?? []),
            { ...reference, display_name: file.name },
          ],
        }));
      }
    } catch (error) {
      setError(error);
    } finally {
      setUploading(false);
      if (fileInput.current) fileInput.current.value = "";
    }
  }
  async function modalSubmit(event: FormEvent) {
    event.preventDefault();
    setPending(true);
    setError(null);
    try {
      if (modal === "workspace") {
        const roots = rootPaths.map((value) => value.trim()).filter(Boolean);
        const result = editingProject
          ? await api.updateProject(
              id(editingProject),
              new Command({
                name: modalText,
                roots,
                expected_revision: editingProject.revision,
              }),
            )
          : await api.createWorkspace(new Command({ name: modalText, roots }));
        await loadCatalogs();
        if (!editingProject) selectConversation(id(result), "");
      }
      if (modal === "rename") {
        await api.updateSession(
          sessionId,
          new Command({
            title: modalText,
            expected_revision:
              session.revision ?? object(session.session).revision,
          }),
        );
        await refreshSessions();
        await loadSession(sessionId);
      }
      if (modal === "steer") {
        const receipt = await api.steer(
          runId,
          new Command({
            text: modalText,
            expected_input_revision:
              run.snapshot.latest_input_revision ?? run.snapshot.input_revision,
          }),
        );
        setReceipt(
          `运行中指令已受理 · ${string(receipt.command_id ?? receipt.id, "等待安全边界应用")}`,
        );
        await refreshRun();
      }
      if (modal === "branch") {
        const ref = run.snapshot.checkpoint_ref ?? currentBranch.checkpoint_ref;
        const result = await api.branch(
          sessionId,
          new Command({
            source_checkpoint_ref: ref,
            side_effect_policy: "preserve_external",
            expected_branch_revision: currentBranch.revision,
          }),
        );
        await loadSession(sessionId);
        setBranchId(string(result.branch_id, id(result)));
        setReceipt("会话分支已创建。外部文件和副作用保持原状。");
      }
      setModal(null);
      setModalText("");
    } catch (error) {
      setError(error);
      if (
        modal === "steer" &&
        error instanceof ApiError &&
        error.status === 409
      )
        await refreshRun();
    } finally {
      setPending(false);
    }
  }
  async function cancel() {
    setPending(true);
    setError(null);
    try {
      const result = await api.cancel(
        runId,
        new Command({ reason: "用户从工作台请求取消" }),
      );
      setReceipt(`取消请求已受理，实际状态：${string(result.status)}。`);
    } catch (error) {
      setError(error);
    } finally {
      setPending(false);
    }
  }
  const openModal = (type: typeof modal, value = "") => {
    setModal(type);
    setModalText(value);
    setError(null);
  };
  if (authLoading)
    return (
      <div className="loading-screen">
        <div className="brand-mark">
          h<span>·</span>
        </div>
        <p>正在连接本地工作台…</p>
      </div>
    );
  if (!auth)
    return (
      <>
        <ErrorNotice error={authError} />
        <Pairing onPaired={() => void authenticate()} />
      </>
    );
  if (settings)
    return (
      <Settings
        capabilities={capabilities}
        onClose={() => setSettings(false)}
        onChanged={() => void loadCatalogs()}
      />
    );
  return (
    <div className={`workbench ${panel ? "with-inspector" : ""}`}>
      <aside className={`sidebar ${navigationOpen ? "open" : ""}`}>
        <div className="brand">
          <div className="brand-mark small">
            h<span>·</span>
          </div>
          <span>
            harness<small>LOCAL WORKSPACE</small>
          </span>
          <button
            className="mobile-only icon-button"
            aria-label="关闭导航"
            onClick={() => setNavigationOpen(false)}
          >
            ×
          </button>
        </div>
        <div className="section-caption project-caption">
          <span>项目</span>
          <button className="text-button" onClick={() => editProject()}>
            ＋ 添加项目
          </button>
        </div>
        <div className="section-caption">
          <span>{archived ? "已归档会话" : "所有项目与会话"}</span>
          <button
            className="text-button"
            onClick={() => {
              setArchived(!archived);
              selectConversation(workspaceId, "");
            }}
          >
            {archived ? "返回" : "归档"}
          </button>
        </div>
        {agents.length > 1 && <label className="new-session-agent">
          新会话使用
          <select aria-label="新会话 Agent" value={newSessionAgent} onChange={(event) => setNewSessionAgent(event.target.value)}>
            {agents.map((agent) => <option key={id(agent)} value={id(agent)}>{string(agent.name, id(agent))}</option>)}
          </select>
        </label>}
        <nav className="project-list" aria-label="项目与会话">
          {workspaces.map((project) => {
            const projectId = id(project);
            const conversations = projectSessions[projectId] || [];
            const roots = array(project.roots).map((value) => string(value));
            return (
              <section
                key={projectId}
                className={`project-group ${workspaceId === projectId ? "selected" : ""}`}
              >
                <div className="project-heading">
                  <button
                    className="project-toggle"
                    aria-label={`${collapsed[projectId] ? "展开" : "折叠"}${string(project.name)}`}
                    aria-expanded={!collapsed[projectId]}
                    onClick={() =>
                      setCollapsed((previous) => ({
                        ...previous,
                        [projectId]: !previous[projectId],
                      }))
                    }
                  >
                    {collapsed[projectId] ? "▸" : "▾"}
                  </button>
                  <button
                    className="project-name"
                    title={roots.join("\n")}
                    onClick={() =>
                      selectConversation(projectId, id(conversations[0]))
                    }
                  >
                    <strong>▱ {string(project.name)}</strong>
                    <small>{roots.length} 个源文件夹</small>
                  </button>
                  <button
                    className="icon-button"
                    aria-label={`在${string(project.name)}中新建会话`}
                    disabled={pending}
                    onClick={() => void startSession(projectId)}
                  >
                    ＋
                  </button>
                  <button
                    className="icon-button"
                    aria-label={`${string(project.name)}项目设置`}
                    onClick={() => editProject(project)}
                  >
                    ⋯
                  </button>
                </div>
                {!collapsed[projectId] && (
                  <div className="session-list project-conversations">
                    {conversations.map((value) => (
                      <button
                        key={id(value)}
                        className={sessionId === id(value) ? "active" : ""}
                        onClick={() => selectConversation(projectId, id(value))}
                      >
                        <span className="session-symbol">◇</span>
                        <span>
                          <strong>{string(value.title, "新会话")}</strong>
                          <small>{timestamp(value.updated_at)}</small>
                        </span>
                      </button>
                    ))}
                    {!conversations.length && (
                      <button
                        className="project-empty"
                        disabled={pending}
                        onClick={() => void startSession(projectId)}
                      >
                        {archived
                          ? "暂无归档会话 · 新建会话"
                          : "＋ 新建第一个会话"}
                      </button>
                    )}
                  </div>
                )}
              </section>
            );
          })}
          {!workspaces.length && (
            <div className="sidebar-empty">
              添加一个项目，把相关文件夹和会话放在一起。
              <button onClick={() => editProject()}>选择源文件夹</button>
            </div>
          )}
        </nav>
        <footer className="sidebar-footer">
          <div className="local-indicator">
            <span />
            本机运行 · 数据归属你的项目
          </div>
          <button onClick={() => setSettings(true)}>
            ⚙ 设置与诊断 <span>↗</span>
          </button>
          <button
            className="text-button"
            onClick={async () => {
              try {
                await api.logout();
                setAuth(null);
                setDrafts({});
                setAttachments({});
                setSession({});
                setRunId("");
              } catch (error) {
                setError(error);
              }
            }}
          >
            断开连接
          </button>
        </footer>
      </aside>
      <main className="main-workspace">
        <header className="topbar">
          <div className="breadcrumb">
            <button
              className="mobile-only icon-button"
              aria-label="打开导航"
              onClick={() => setNavigationOpen(true)}
            >
              ☰
            </button>
            <span>
              {string(
                workspaces.find((value) => id(value) === workspaceId)?.name,
                "工作台",
              )}
            </span>
            <span>/</span>
            <strong>
              {string(session.title ?? object(session.session).title, "新会话")}
            </strong>
          </div>
          <div className="topbar-actions">
            {sessionId && (
              <>
                <button
                  className="text-button"
                  onClick={() =>
                    openModal(
                      "rename",
                      string(session.title ?? object(session.session).title),
                    )
                  }
                >
                  重命名
                </button>
                <button
                  className="text-button"
                  disabled={pending}
                  onClick={async () => {
                    setPending(true);
                    try {
                      await api.updateSession(
                        sessionId,
                        new Command({
                          archived: !archived,
                          expected_revision:
                            session.revision ??
                            object(session.session).revision,
                        }),
                      );
                      await refreshSessions();
                      setSessionId("");
                    } catch (error) {
                      setError(error);
                    } finally {
                      setPending(false);
                    }
                  }}
                >
                  {archived ? "取消归档" : "归档"}
                </button>
              </>
            )}
            <span className="environment-tag">LOCAL</span>
          </div>
        </header>
        <div className="run-toolbar">
          <div className="run-info">
            {runId ? (
              <>
                <Status status={run.serverStatus} />
                <span
                  className={`connection ${connection}`}
                  title={connectionError}
                >
                  {connection === "connected"
                    ? "事件已同步"
                    : connection === "disconnected"
                      ? "事件未连接"
                      : "正在重新连接"}
                </span>
                <small title={run.updatedAt}>{timestamp(run.updatedAt)}</small>
              </>
            ) : (
              <span className="muted">准备好，开始下一件事。</span>
            )}
          </div>
          <div className="panel-tabs">
            {(
              [
                "context",
                "tools",
                "artifacts",
                ...(capabilities.children ? ["tasks"] : []),
                ...(capabilities.memory ? ["memory"] : []),
              ] as Panel[]
            ).map((name) => (
              <button
                key={name}
                className={panel === name ? "active" : ""}
                onClick={() => setPanel(panel === name ? null : name)}
              >
                {
                  {
                    context: "上下文",
                    tools: "工具",
                    artifacts: "产物",
                    tasks: "任务树",
                    memory: "记忆",
                  }[name]
                }
              </button>
            ))}
          </div>
        </div>
        {runId && (
          <div className="run-controls">
            <select
              aria-label="查看任务"
              value={runId}
              onChange={(event) => selectRun(event.target.value)}
            >
              {!runList.some((value) => id(value) === runId) && (
                <option value={runId}>{runId.slice(0, 12)}</option>
              )}
              {runList.map((value, index) => (
                <option key={id(value)} value={id(value)}>
                  任务 {index + 1} ·{" "}
                  {id(value) === runId
                    ? run.serverStatus
                    : string(value.status)}
                </option>
              ))}
            </select>
            {branches.length > 1 && (
              <select
                aria-label="当前会话分支"
                value={branchId}
                onChange={(event) => {
                  setBranchId(event.target.value);
                  const candidates = runList.filter(
                    (run) => run.branch_id === event.target.value,
                  );
                  if (candidates.length)
                    setRunId(id(candidates[candidates.length - 1]));
                }}
              >
                {branches.map((branch) => (
                  <option
                    key={string(branch.branch_id, id(branch))}
                    value={string(branch.branch_id, id(branch))}
                  >
                    {string(
                      branch.title ?? branch.name,
                      string(branch.branch_id, id(branch)).slice(0, 12),
                    )}
                  </option>
                ))}
              </select>
            )}
            {capabilities.branches === true && (
              <button
                className="text-button"
                disabled={
                  !run.snapshot.checkpoint_ref && !currentBranch.checkpoint_ref
                }
                title="从已保存的 checkpoint 创建分支"
                onClick={() => openModal("branch")}
              >
                创建分支
              </button>
            )}
            {capabilities.steering === true && !terminal(run.serverStatus) && (
              <button
                className="text-button"
                disabled={!["queued", "running"].includes(run.serverStatus)}
                title={
                  ["queued", "running"].includes(run.serverStatus)
                    ? "在下一安全边界提交补充指令"
                    : "当前状态请先处理交互或等待恢复，再提交补充指令"
                }
                onClick={() => openModal("steer")}
              >
                调整当前任务
              </button>
            )}
            {!terminal(run.serverStatus) && (
              <button
                className="danger text-button"
                disabled={pending}
                onClick={() => void cancel()}
              >
                取消任务
              </button>
            )}
          </div>
        )}
        <div
          className="conversation"
          aria-live="polite"
          ref={conversationRef}
          onScroll={(event) => {
            const element = event.currentTarget;
            followLatest.current =
              element.scrollHeight - element.scrollTop - element.clientHeight <
              100;
          }}
        >
          {!runId && (
            <section className="welcome">
              <div className="eyebrow">SPACE TO THINK. TOOLS TO DO.</div>
              <h1>今天，我们完成什么？</h1>
              <p>
                描述你的目标。Harness 会保留过程、调用工具，
                <br />
                在需要你做决定时停下来。
              </p>
              <div className="suggestions">
                {[
                  "梳理项目结构，给出下一步实施建议",
                  "阅读资料，整理带来源的研究笔记",
                  "制定计划，并逐步完成一个开发任务",
                ].map((text, index) => (
                  <button key={text} onClick={() => setDraft(text)}>
                    <span>0{index + 1}</span>
                    {text}
                    <b>↗</b>
                  </button>
                ))}
              </div>
              <p className="welcome-note">
                本地项目 · 可核对的执行过程 · 明确的权限边界
              </p>
            </section>
          )}
          {runId && Object.values(run.messages).length === 0 && (
            <Empty title="任务已进入工作流程">
              消息与工具执行结果会在提交后显示。
            </Empty>
          )}
          {Object.keys(run.messages).length > messageLimit && (
            <button
              className="text-button"
              onClick={() => {
                followLatest.current = false;
                setMessageLimit((value) => value + 100);
              }}
            >
              显示较早的 100 条消息（还剩{" "}
              {Object.keys(run.messages).length - messageLimit} 条）
            </button>
          )}
          {Object.values(run.messages)
            .slice(-messageLimit)
            .map((message) => (
              <article
                className={`message ${string(message.role)}`}
                key={string(message.message_id)}
              >
                <div className="message-avatar">
                  {message.role === "user"
                    ? "你"
                    : message.role === "tool"
                      ? "◇"
                      : "h·"}
                </div>
                <div className="message-body">
                  <div className="message-heading">
                    {message.role === "user"
                      ? "你"
                      : message.role === "tool"
                        ? "工具结果"
                        : "Harness"}
                    {message.model_attempt_id != null && <small>已提交</small>}
                  </div>
                  <MessageContent message={message} />
                  {array(message.content_parts)
                    .map(object)
                    .filter(
                      (part) =>
                        part.type === "file_reference" || part.type === "image",
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
                </div>
              </article>
            ))}
          {Object.values(run.streams).map((stream) => (
            <article className="message assistant" key={stream.streamId}>
              <div className="message-avatar">h·</div>
              <div className="message-body">
                <div className="message-heading">
                  Harness{" "}
                  <small>
                    {stream.interrupted
                      ? "连接中断，等待已提交结果"
                      : "正在生成"}
                  </small>
                </div>
                <MarkdownText text={streamText(stream)} />
              </div>
            </article>
          ))}
          {Object.values(run.interactions)
            .filter((value) =>
              ["open", "pending_binding"].includes(string(value.status)),
            )
            .map((record) => (
              <InteractionCard
                runEnded={terminal(run.serverStatus)}
                key={string(record.interaction_id)}
                record={record}
                onChanged={() => void loadSession(sessionId).catch(setError)}
              />
            ))}
          {run.serverStatus === "needs_review" && (
            <Notice>
              有工具结果需要人工核对。
              <button className="text-button" onClick={() => setPanel("tools")}>
                查看操作证据 →
              </button>
            </Notice>
          )}
          {run.snapshot.error != null && (
            <JsonDetail value={run.snapshot.error} label="查看任务错误" />
          )}
        </div>
        <section className="composer-area">
          <ErrorNotice error={error} />
          {connectionError && runId && (
            <Notice>{connectionError} 浏览器连接变化不会取消任务。</Notice>
          )}
          {receipt && (
            <div className="receipt" role="status">
              ✓ {receipt}
            </div>
          )}
          <form
            className="composer"
            onSubmit={(event) => {
              event.preventDefault();
              void send();
            }}
          >
            <textarea
              aria-label="任务目标"
              value={draft}
              onChange={(event) => setDraft(event.target.value)}
              rows={3}
              placeholder={
                runId && !terminal(run.serverStatus)
                  ? "添加一个新任务，它将在当前任务之后排队…"
                  : "描述目标，或添加文件作为参考…"
              }
              onKeyDown={(event) => {
                if ((event.ctrlKey || event.metaKey) && event.key === "Enter") {
                  event.preventDefault();
                  if (!pending && !uploading) void send();
                }
              }}
            />
            <div className="attachment-list">
              {attached.map((artifact) => (
                <span key={string(artifact.artifact_id)}>
                  {string(artifact.display_name, "附件")}
                  <button
                    type="button"
                    aria-label="移除此附件"
                    onClick={() => {
                      setAttachments((previous) => ({
                        ...previous,
                        [composerKey]: attached.filter(
                          (value) => value !== artifact,
                        ),
                      }));
                      setRetry(null);
                    }}
                  >
                    ×
                  </button>
                </span>
              ))}
            </div>
            <div className="composer-bottom">
              <div className="composer-options">
                <button
                  type="button"
                  className="icon-button"
                  aria-label="添加附件"
                  disabled={!workspaceId || uploading}
                  onClick={() => fileInput.current?.click()}
                >
                  ＋
                </button>
                <input
                  type="file"
                  multiple
                  hidden
                  ref={fileInput}
                  onChange={(event) => void upload(event.target.files)}
                />
                <select
                  aria-label="选择 Agent"
                  value={agentId}
                  disabled={Boolean(sessionId)}
                  title={
                    sessionId
                      ? "Agent 已绑定此会话；可在侧栏选择新会话使用的 Agent"
                      : "选择此会话的 Agent"
                  }
                  onChange={(event) => {
                    setAgentId(event.target.value);
                    setRetry(null);
                  }}
                >
                  <option value="default">默认 Agent</option>
                  {agents
                    .filter((agent) => id(agent) !== "default")
                    .map((agent) => (
                      <option key={id(agent)} value={id(agent)}>
                        {string(agent.name, id(agent))}
                      </option>
                    ))}
                </select>
                {skills.length > 0 && (
                  <details className="skill-picker">
                    <summary>
                      Skills{" "}
                      {selectedSkills.length > 0
                        ? `(${selectedSkills.length})`
                        : ""}
                    </summary>
                    <div>
                      {skills
                        .filter((skill) => skill.enabled !== false)
                        .map((skill) => (
                          <label key={id(skill)}>
                            <input
                              type="checkbox"
                              checked={selectedSkills.includes(id(skill))}
                              onChange={(event) => {
                                setSelectedSkills((previous) =>
                                  event.target.checked
                                    ? [...previous, id(skill)]
                                    : previous.filter(
                                        (value) => value !== id(skill),
                                      ),
                                );
                                setRetry(null);
                              }}
                            />
                            {string(skill.name, id(skill))}
                          </label>
                        ))}
                    </div>
                  </details>
                )}
              </div>
              <button
                className="send-button"
                disabled={
                  pending || uploading || (!draft.trim() && !attached.length)
                }
                aria-label="发送任务"
              >
                {pending ? "提交中…" : uploading ? "上传中…" : "开始任务 ↑"}
              </button>
            </div>
          </form>
          <div className="composer-footnote">
            <span>
              {currentAgent?.model_profile_id === "demo"
                ? "当前使用确定性演示模型"
                : "执行与授权以服务端记录为准"}
            </span>
            <span>Ctrl + Enter 发送</span>
          </div>
          {retry && !pending && (
            <button className="text-button" onClick={() => void send(retry)}>
              使用原幂等键重试提交
            </button>
          )}
        </section>
      </main>
      {panel && (
        <Inspector
          panel={panel}
          run={run}
          workspace={workspaceId}
          onSelectRun={selectRun}
          onClose={() => setPanel(null)}
        />
      )}
      {modal && (
        <Modal
          title={
            {
              workspace: editingProject ? "项目设置" : "添加项目",
              rename: "重命名会话",
              branch: "创建会话分支",
              steer: "调整当前任务",
            }[modal]
          }
          onClose={() => setModal(null)}
        >
          <form onSubmit={(event) => void modalSubmit(event)}>
            {modal === "branch" ? (
              <Notice>
                从当前已保存 checkpoint
                创建独立历史。真实文件与已经发生的外部操作不会回滚。
                <JsonDetail
                  value={
                    run.snapshot.checkpoint_ref ?? currentBranch.checkpoint_ref
                  }
                  label="分支起点"
                />
              </Notice>
            ) : (
              <label>
                {modal === "workspace"
                  ? "项目名称"
                  : modal === "rename"
                    ? "会话名称"
                    : "补充指令"}
                <textarea
                  autoFocus
                  required
                  rows={modal === "steer" ? 5 : 2}
                  value={modalText}
                  onChange={(event) => setModalText(event.target.value)}
                />
              </label>
            )}
            {modal === "workspace" && (
              <div className="project-folders">
                <p className="muted">
                  源文件夹 ·
                  第一个作为默认工作目录。项目内所有会话共享这些文件。
                </p>
                {rootPaths.map((path, index) => (
                  <div className="folder-row" key={index}>
                    <input
                      aria-label={`源文件夹 ${index + 1}`}
                      value={path}
                      placeholder="C:\\Users\\…\\Projects\\MyProject"
                      onChange={(event) =>
                        setRootPaths((previous) =>
                          previous.map((value, position) =>
                            position === index ? event.target.value : value,
                          ),
                        )
                      }
                    />
                    <button
                      type="button"
                      aria-label={`移除源文件夹 ${index + 1}`}
                      onClick={() =>
                        setRootPaths((previous) =>
                          previous.filter((_, position) => position !== index),
                        )
                      }
                    >
                      ×
                    </button>
                  </div>
                ))}
                <div className="folder-actions">
                  <button
                    type="button"
                    disabled={pickingFolder}
                    onClick={() => void chooseFolder()}
                  >
                    {pickingFolder ? "请在系统窗口中选择…" : "选择文件夹…"}
                  </button>
                  <button
                    type="button"
                    onClick={() =>
                      setRootPaths((previous) => [...previous, ""])
                    }
                  >
                    手动添加路径
                  </button>
                </div>
              </div>
            )}
            {modal === "steer" && (
              <p className="muted">
                此内容仅加入当前运行的安全输入边界，不会另建排队任务。
              </p>
            )}
            <ErrorNotice error={error} />
            <button
              className="primary"
              disabled={
                pending ||
                pickingFolder ||
                (modal === "workspace" &&
                  !rootPaths.some((value) => value.trim()))
              }
            >
              {pending
                ? "正在提交…"
                : modal === "branch"
                  ? "创建分支"
                  : "保存并提交"}
            </button>
          </form>
        </Modal>
      )}
    </div>
  );
}
