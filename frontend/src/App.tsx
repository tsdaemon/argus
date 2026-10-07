import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ComponentProps, type ReactNode } from "react";
import {
  CopilotKitProvider, CopilotChat, CopilotChatAssistantMessage, CopilotChatConfigurationProvider, CopilotChatMessageView,
  CopilotChatUserMessage,
  useAgent, useCopilotKit, useInterrupt,
} from "@copilotkit/react-core/v2";
import {
  api, ArgusHttpAgent, attachmentAccept, attachmentMaxSize, deleteThread, formatUsd, renameThread, resumeProp, type RunState, type Thread,
} from "./agent";
import { Approvals } from "./Approvals";
import { MessageTime, stampMessages, type Stamp } from "./MessageTime";
import { summarize, ToolCallCard } from "./ToolCall";
import logo from "./logo.png";

const toolRenderers = [ToolCallCard];
const pageSize = 100;
const collapsedKey = "argus.sidebar.collapsed";

type MessageListProps = Parameters<NonNullable<ComponentProps<typeof CopilotChatMessageView>["children"]>>[0];

// Supplying the list's children turns off CopilotKit's virtualization, which it applies past
// 50 messages: its estimated row heights fight the stick-to-bottom scroller and the scroll
// position jumps around in long conversations.
// When each message was sent, and which ones open a new day, for the message components.
const Stamps = createContext<Map<string, Stamp>>(new Map());
// How the last run ended, when it needs saying: shown after its messages, not over the thread.
const RunNotice = createContext<ReactNode>(null);

function MessageList({ messageElements, interruptElement, isRunning, messages }: MessageListProps) {
  const seen = useRef(new Map<string, Date>());
  const stamps = useMemo(() => stampMessages(messages, seen.current), [messages]);
  const notice = useContext(RunNotice);
  return <Stamps.Provider value={stamps}>
    <div data-testid="copilot-message-list" className="copilotKitMessages cpk:flex cpk:flex-col">
      {messageElements}
      {interruptElement}
      {isRunning && messages.at(-1)?.role !== "reasoning" && <div className="cpk:mt-2"><CopilotChatMessageView.Cursor /></div>}
      {!isRunning && notice}
    </div>
  </Stamps.Provider>;
}
function UserMessage(props: ComponentProps<typeof CopilotChatUserMessage>) {
  const author = props.message.metadata?.argus_author;
  const external = author?.kind === "agent";
  const stamp = useContext(Stamps).get(props.message.id);
  return <>
    <MessageTime stamp={stamp} part="day" />
    <div className={external ? "external-message" : undefined}>
      {external ? <div className="message-header">
        <div className="message-author" title={`A2A · ${author.token_id}`}>
          <span className="message-author-badge">Agent</span> {author.name}
        </div>
        <MessageTime stamp={stamp} part="time" />
      </div> : <MessageTime stamp={stamp} part="time" align="end" />}
      <CopilotChatUserMessage {...props} />
    </div>
  </>;
}
function AssistantMessage(props: ComponentProps<typeof CopilotChatAssistantMessage>) {
  const stamp = useContext(Stamps).get(props.message.id);
  // A message that only carries tool calls shows no time of its own.
  const text = typeof props.message.content === "string" && props.message.content.trim();
  return <>
    <MessageTime stamp={stamp} part="day" />
    {/* One flex item, so the list's gap separates messages, not a message from its time. */}
    <div className="assistant-turn">
      {text && <MessageTime stamp={stamp} part="time" align="start" />}
      <CopilotChatAssistantMessage {...props} />
    </div>
  </>;
}
const messageView = {
  children: MessageList,
  userMessage: Object.assign(UserMessage, CopilotChatUserMessage),
  assistantMessage: Object.assign(AssistantMessage, CopilotChatAssistantMessage),
};

function readCollapsed() {
  try { return localStorage.getItem(collapsedKey) === "1"; } catch { return false; }
}

function Chat({ thread, onBusy, onFinished }: {
  thread: Thread; onBusy: (busy: boolean) => void; onFinished: () => void;
}) {
  const { agent } = useAgent();
  const { copilotkit } = useCopilotKit();
  const [connecting, setConnecting] = useState(true);
  const [stalled, setStalled] = useState<RunState | null>(null);
  const [resuming, setResuming] = useState(false);
  const [uploadError, setUploadError] = useState("");
  const [runError, setRunError] = useState("");
  const pending = agent.pendingInterrupts.length > 0;
  const http = agent as unknown as ArgusHttpAgent;
  const stopping = useCallback(() => http.stopping, [http]);
  useInterrupt({ render: (props) => <Approvals key={props.interrupts.map((i) => i.id).join()} {...props} /> });
  useEffect(() => {
    const subscription = agent.subscribe({
      onRunStartedEvent: () => { onBusy(true); setStalled(null); setUploadError(""); setRunError(""); },
      onRunErrorEvent: ({ event }) => {
        if (stopping()) return;
        setRunError(event.message || "The run failed.");
        // A run that failed partway can usually carry on from its last step.
        api<RunState>(`/api/threads/${thread.id}/run-state`)
          .then((state) => setStalled(state.stalled ? state : null)).catch(() => {});
      },
      onRunFinalized: () => {
        setConnecting(false); setResuming(false); onBusy(false); onFinished();
        const stop = stopping();
        if (stop) {
          // Show the transcript as the server closed it, ending in "Stopped by the operator."
          void stop.then(() => { http.stopping = null; return copilotkit.connectAgent({ agent }); });
        }
      },
    });
    return () => { subscription.unsubscribe(); onBusy(false); };
  }, [agent, copilotkit, http, stopping, thread.id, onBusy, onFinished]);

  const resume = useCallback(() => {
    setResuming(true);
    void copilotkit.runAgent({ agent, forwardedProps: { [resumeProp]: true } });
  }, [agent, copilotkit]);

  // Once the transcript is loaded: a run cut off partway resumes by itself when it would
  // only read, and otherwise asks first.
  useEffect(() => {
    if (connecting) return;
    let cancelled = false;
    api<RunState>(`/api/threads/${thread.id}/run-state`).then((state) => {
      if (cancelled || !state.stalled) return;
      if (state.pending.every((call) => call.class === "read")) resume();
      else setStalled(state);
    }).catch(() => { /* the chat still works; the run stays interrupted */ });
    return () => { cancelled = true; };
  }, [connecting, thread.id, resume]);

  const notice = runError || (stalled && !resuming)
    ? <section className={runError ? "run-notice run-failed" : "run-notice"}
      aria-label={runError ? "Failed run" : "Interrupted run"} role={runError ? "alert" : undefined}>
      <div className="eyebrow">{runError ? "Run failed" : "Interrupted run"}</div>
      {runError && <pre>{runError}</pre>}
      {stalled ? <>
        <p>{stalled.pending.length
          ? "These calls had not finished. Resuming runs them again; sending a new message cancels them instead."
          : "Resume to retry from the last completed step, or send a new message instead."}</p>
        {stalled.pending.length > 0 && <ul>{stalled.pending.map((call, i) => <li key={i}>
          <code>{call.name} {summarize(call.name, call.args)}</code>
          <span className="tool-call-risk" data-risk={call.class}>{call.class === "mutate" ? "change" : call.class}</span>
        </li>)}</ul>}
        <button className="primary" onClick={resume}>Resume</button>
      </> : <p>Send a message to carry on.</p>}
    </section>
    : null;

  return <RunNotice.Provider value={notice}>
  {resuming && <div className="resume-notice" role="status">Resuming the interrupted run…</div>}
  {uploadError && <div className="resume-notice" role="alert">{uploadError}</div>}
  <CopilotChat
    threadId={thread.id} className="argus-chat" messageView={messageView}
    attachments={{
      enabled: true, accept: attachmentAccept, maxSize: attachmentMaxSize,
      onUploadFailed: (failure) => setUploadError(`Could not attach that file: ${failure.message}`),
    }}
    labels={{
      chatInputPlaceholder: "Ask argus about your home infrastructure…",
      welcomeMessageText: "What would you like to look into?",
      chatDisclaimerText: "Operational changes pause for your approval.",
    }}
    input={{
      showDisclaimer: true, startTranscribeButton: () => null,
      ...(connecting || pending ? {
        textArea: { disabled: true }, sendButton: { disabled: true },
      } : {}),
    }}
  />
  </RunNotice.Provider>;
}

function Conversation({ thread, onBusy, onFinished }: {
  thread: Thread; onBusy: (busy: boolean) => void; onFinished: () => void;
}) {
  const agents = useMemo(() => ({ default: new ArgusHttpAgent({
    url: "/agent", threadId: thread.id,
  }) }), [thread.id]);
  const [error, setError] = useState("");
  return <CopilotKitProvider agents__unsafe_dev_only={agents} enableInspector={false}
    renderToolCalls={toolRenderers} onError={({ error, code }) => {
      // A failed run is shown in the transcript, under the run.
      if (code !== "agent_run_error_event") setError(error.message);
    }}>
    <CopilotChatConfigurationProvider threadId={thread.id}>
      {error && <div className="error" role="alert">
        <p>{error}</p>
        <button onClick={() => location.reload()}>Reload conversation</button>
      </div>}
      <Chat thread={thread} onBusy={onBusy} onFinished={onFinished} />
    </CopilotChatConfigurationProvider>
  </CopilotKitProvider>;
}

export default function App() {
  const [origin, setOrigin] = useState<"all" | "human" | "a2a">("all");
  const originQuery = `&origin=${origin}`;
  const [threads, setThreads] = useState<Thread[]>([]);
  const [active, setActive] = useState<Thread | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [creating, setCreating] = useState(false);
  const [error, setError] = useState("");
  const [launch, setLaunch] = useState(false);
  const [authEnabled, setAuthEnabled] = useState(false);
  const [hasMore, setHasMore] = useState(false);
  const [collapsed, setCollapsed] = useState(readCollapsed);
  const [confirming, setConfirming] = useState<string | null>(null);
  const [renaming, setRenaming] = useState<string | null>(null);
  const [spent, setSpent] = useState<number | null>(null);
  const loadSpent = useCallback(() => {
    api<{ total_usd: number }>("/api/costs").then((c) => setSpent(c.total_usd), () => setSpent(null));
  }, []);

  function toggleSidebar() {
    const next = !collapsed;
    setCollapsed(next);
    try { localStorage.setItem(collapsedKey, next ? "1" : "0"); } catch { /* not persisted */ }
  }

  const refresh = useCallback(async () => {
    try {
      const rows = await api<Thread[]>(`/api/threads?limit=${pageSize}${originQuery}`);
      setThreads((previous) => [...rows, ...previous.filter((old) => !rows.some((r) => r.id === old.id))]);
      setHasMore(rows.length === pageSize);
      setActive((current) => rows.find((r) => r.id === current?.id) ?? current);
      loadSpent();
    } catch (e) { setError(String(e)); }
  }, [originQuery, loadSpent]);
  // The server names a conversation just after its run ends, so look once more for the title.
  const onFinished = useCallback(() => {
    void refresh();
    window.setTimeout(() => void refresh(), 5000);
  }, [refresh]);

  function select(thread: Thread) {
    if (busy) return;
    setActive(thread);
    const url = new URL(location.href);
    url.searchParams.set("thread", thread.id);
    history.replaceState(null, "", url);
  }

  async function load() {
    setLoading(true); setError("");
    try {
      const [rows, config] = await Promise.all([
        api<Thread[]>(`/api/threads?limit=${pageSize}${originQuery}`),
        api<{ launch_enabled: boolean; auth_enabled: boolean }>("/api/ui-config"),
      ]);
      setThreads(rows); setHasMore(rows.length === pageSize); setLaunch(config.launch_enabled); setAuthEnabled(config.auth_enabled);
      loadSpent();
      const wanted = new URL(location.href).searchParams.get("thread");
      // The selected thread can be older than the first page.
      if (wanted && /^[0-9a-f-]{36}$/i.test(wanted)) {
        const selected = rows.find((r) => r.id === wanted);
        if (selected) setActive(selected);
        else {
          const found = await api<Thread>(`/api/threads/${wanted}`);
          setActive(found);
        }
      } else if (rows.length) select(rows[0]);
    } catch (e) { setError(String(e)); }
    finally { setLoading(false); }
  }
  useEffect(() => { void load(); }, []);

  async function remove(thread: Thread) {
    setError("");
    try {
      await deleteThread(thread.id);
    } catch (e) { setError(String(e)); return; }
    finally { setConfirming(null); }
    const remaining = threads.filter((t) => t.id !== thread.id);
    setThreads(remaining);
    if (active?.id === thread.id) {
      const next = remaining[0] ?? null;
      setActive(next);
      const url = new URL(location.href);
      if (next) url.searchParams.set("thread", next.id); else url.searchParams.delete("thread");
      history.replaceState(null, "", url);
    }
  }

  async function rename(thread: Thread, title: string) {
    setRenaming(null);
    title = title.trim();
    if (!title || title === thread.title) return;
    setError("");
    try {
      const updated = await renameThread(thread.id, title);
      setThreads((rows) => rows.map((r) => r.id === updated.id ? { ...r, ...updated } : r));
      setActive((current) => current?.id === updated.id ? { ...current, ...updated } : current);
    } catch (e) { setError(String(e)); }
  }

  async function newChat() {
    setCreating(true); setError("");
    try {
      const thread = await api<Thread>("/api/threads", { method: "POST" });
      setOrigin("all"); setThreads((rows) => [thread, ...rows]); select(thread);
    } catch (e) { setError(String(e)); }
    finally { setCreating(false); }
  }

  async function changeOrigin(next: "all" | "human" | "a2a") {
    if (busy || loading) return;
    setOrigin(next); setLoading(true); setError("");
    try {
      const query = `&origin=${next}`;
      const rows = await api<Thread[]>(`/api/threads?limit=${pageSize}${query}`);
      setThreads(rows); setHasMore(rows.length === pageSize);
      setActive(null);
      const url = new URL(location.href); url.searchParams.delete("thread");
      history.replaceState(null, "", url);
      if (rows.length) select(rows[0]);
    } catch (e) { setError(String(e)); }
    finally { setLoading(false); }
  }

  async function more() {
    try {
      const rows = await api<Thread[]>(`/api/threads?limit=${pageSize}&offset=${threads.length}${originQuery}`);
      setThreads((current) => [...current, ...rows.filter((r) => !current.some((c) => c.id === r.id))]);
      setHasMore(rows.length === pageSize);
    } catch (e) { setError(String(e)); }
  }

  return <div className="app-shell">
    <aside className="sidebar" aria-label="Conversations" data-collapsed={collapsed || undefined}>
      <div className="sidebar-top">
        <a className="brand" href="/" aria-label="argus home"><img className="brand-mark" src={logo} alt="" />
          <span className="label">argus<small>SEE EVERYTHING</small></span>
        </a>
        <button className="collapse-toggle" onClick={toggleSidebar} aria-expanded={!collapsed}
          aria-label={collapsed ? "Expand sidebar" : "Collapse sidebar"}
          data-tooltip={collapsed ? "Expand sidebar" : "Collapse sidebar"}>
          <svg viewBox="0 0 20 20" width="20" height="20" fill="none" stroke="currentColor" strokeWidth="1.6"
            strokeLinecap="round" aria-hidden="true"><rect x="2.5" y="3.5" width="15" height="13" rx="3.2" /><path d="M8 3.8v12.4" /></svg>
        </button>
      </div>
      <button className="new-chat" disabled={busy || creating || loading} onClick={() => void newChat()}
        aria-label={collapsed ? "New conversation" : undefined} data-tooltip={collapsed ? "New conversation" : undefined}>
        <span aria-hidden="true">＋</span> <span className="label">{creating ? "Creating…" : "New conversation"}</span>
      </button>
      <div className="conversation-tabs" role="tablist" aria-label="Conversation source">
        {([ ["all", "All"], ["human", "Mine"], ["a2a", "Agents"] ] as const).map(([value, label]) =>
          <button key={value} role="tab" aria-selected={origin === value} disabled={busy || loading}
            onClick={() => void changeOrigin(value)}>{label}</button>)}
      </div>
      <div className="sidebar-heading">CONVERSATIONS</div>
      <nav className="thread-list">
        {threads.map((thread) => <div key={thread.id} className="thread-row">
          {confirming === thread.id ? <div className="thread-confirm" role="group" aria-label="Confirm deletion">
            <span>Delete this conversation?</span>
            <button className="danger" onClick={() => void remove(thread)}>Delete</button>
            <button onClick={() => setConfirming(null)}>Cancel</button>
          </div> : renaming === thread.id ? <input className="thread-rename" autoFocus maxLength={100}
            aria-label="Conversation title" defaultValue={thread.title ?? ""}
            onFocus={(e) => e.currentTarget.select()}
            onBlur={(e) => void rename(thread, e.currentTarget.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") e.currentTarget.blur();
              if (e.key === "Escape") setRenaming(null);
            }} /> : <>
            <button className="thread-select" disabled={busy}
              aria-current={active?.id === thread.id ? "page" : undefined}
              onClick={() => select(thread)}>
              <span>{thread.title || "New conversation"}</span>
              {thread.origin === "a2a" && <small className="thread-author">Agent · {thread.author?.name || "External agent"}</small>}
              <div className="thread-meta">
                <time dateTime={thread.updated_at}>{new Date(thread.updated_at).toLocaleDateString(undefined, {
                  month: "short", day: "numeric",
                })}</time>
                {thread.cost_usd > 0 && <small className="thread-cost" title="Model spend">{formatUsd(thread.cost_usd)}</small>}
              </div>
            </button>
            <button className="thread-action thread-edit" aria-label="Rename conversation"
              data-tooltip="Rename conversation" onClick={() => setRenaming(thread.id)}>
              <svg viewBox="0 0 20 20" width="16" height="16" fill="none" stroke="currentColor" strokeWidth="1.6"
                strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
                <path d="M13.25 3.75l3 3L7 16H4v-3l9.25-9.25zM11.5 5.5l3 3" />
              </svg>
            </button>
            <button className="thread-action thread-delete" disabled={busy} aria-label="Delete conversation"
              data-tooltip="Delete conversation" onClick={() => setConfirming(thread.id)}>
              <svg viewBox="0 0 20 20" width="16" height="16" fill="none" stroke="currentColor" strokeWidth="1.6"
                strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
                <path d="M4 6h12M8 6V4.5h4V6M6 6l.7 9.5h6.6L14 6M8.5 9v4M11.5 9v4" />
              </svg>
            </button>
          </>}
        </div>)}
        {!loading && !threads.length && <p className="muted">Your conversations will appear here.</p>}
        {hasMore && <button disabled={busy} onClick={() => void more()}>Load older conversations</button>}
      </nav>
      <div className="sidebar-footer">
        <span className="status-dot" data-busy={busy || undefined} />
        <span className="label" role="status">{busy ? "Working…" : "Ready"}</span>
        {spent !== null && <span className="label spent" title="Model spend across all conversations">
          {formatUsd(spent)} spent</span>}
        {launch && <a className="label" href="/launch" target="_blank" rel="noreferrer">Open privileged session ↗</a>}
        {authEnabled && <a className="label" href="/settings/tokens">API tokens</a>}
        {authEnabled && <form method="post" action="/logout"><button className="link" type="submit">Log out</button></form>}
      </div>
    </aside>
    <main>
      {error && <div className="error" role="alert"><p>{error}</p>
        <button onClick={() => void load()} disabled={busy}>Retry</button></div>}
      {loading || creating ? <div className="empty" role="status">Loading conversation…</div> : active ?
        <Conversation key={active.id} thread={active} onBusy={setBusy} onFinished={onFinished} /> :
        <div className="empty"><img className="empty-mark" src={logo} alt="" />
          <div className="eyebrow">A WATCHFUL EYE ON YOUR HOME</div>
          <h2>What needs a closer look?</h2>
          <p>Investigate a service, review container logs, or work through a problem with argus.</p>
          <button className="primary" disabled={creating} onClick={() => void newChat()}>Start a conversation</button>
        </div>}
    </main>
  </div>;
}
