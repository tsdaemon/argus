import { useCallback, useEffect, useMemo, useState } from "react";
import {
  CopilotKitProvider, CopilotChat, CopilotChatConfigurationProvider,
  useAgent, useInterrupt, WildcardToolCallRender,
} from "@copilotkit/react-core/v2";
import { api, ArgusHttpAgent, deleteThread, type Thread } from "./agent";
import { Approvals } from "./Approvals";
import logo from "./logo.png";

const toolRenderers = [WildcardToolCallRender];
const pageSize = 100;
const collapsedKey = "argus.sidebar.collapsed";

function readCollapsed() {
  try { return localStorage.getItem(collapsedKey) === "1"; } catch { return false; }
}

function Chat({ thread, onBusy, onFinished }: {
  thread: Thread; onBusy: (busy: boolean) => void; onFinished: () => void;
}) {
  const { agent } = useAgent();
  const [connecting, setConnecting] = useState(true);
  const pending = agent.pendingInterrupts.length > 0;
  useInterrupt({ render: (props) => <Approvals key={props.interrupts.map((i) => i.id).join()} {...props} /> });
  useEffect(() => {
    const subscription = agent.subscribe({
      onRunStartedEvent: () => { onBusy(true); },
      onRunFinalized: () => { setConnecting(false); onBusy(false); onFinished(); },
    });
    return () => { subscription.unsubscribe(); onBusy(false); };
  }, [agent, onBusy, onFinished]);
  return <CopilotChat
    threadId={thread.id} className="argus-chat"
    labels={{
      chatInputPlaceholder: "Ask argus about your home infrastructure…",
      welcomeMessageText: "What would you like to look into?",
      chatDisclaimerText: "Operational changes pause for your approval.",
    }}
    input={{
      showDisclaimer: true, startTranscribeButton: () => null, addMenuButton: () => null,
      ...(connecting || pending ? {
        textArea: { disabled: true }, sendButton: { disabled: true },
      } : {}),
    }}
  />;
}

function Conversation({ thread, onBusy, onFinished }: {
  thread: Thread; onBusy: (busy: boolean) => void; onFinished: () => void;
}) {
  const agents = useMemo(() => ({ default: new ArgusHttpAgent({
    url: "/agent", threadId: thread.id,
  }) }), [thread.id]);
  const [error, setError] = useState("");
  return <CopilotKitProvider agents__unsafe_dev_only={agents} enableInspector={false}
    renderToolCalls={toolRenderers} onError={({ error }) => setError(error.message)}>
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
  const [threads, setThreads] = useState<Thread[]>([]);
  const [active, setActive] = useState<Thread | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [creating, setCreating] = useState(false);
  const [error, setError] = useState("");
  const [launch, setLaunch] = useState(false);
  const [hasMore, setHasMore] = useState(false);
  const [collapsed, setCollapsed] = useState(readCollapsed);
  const [confirming, setConfirming] = useState<string | null>(null);

  function toggleSidebar() {
    const next = !collapsed;
    setCollapsed(next);
    try { localStorage.setItem(collapsedKey, next ? "1" : "0"); } catch { /* not persisted */ }
  }

  const refresh = useCallback(async () => {
    try {
      const rows = await api<Thread[]>(`/api/threads?limit=${pageSize}`);
      setThreads((previous) => [...rows, ...previous.filter((old) => !rows.some((r) => r.id === old.id))]);
      setHasMore(rows.length === pageSize);
      setActive((current) => rows.find((r) => r.id === current?.id) ?? current);
    } catch (e) { setError(String(e)); }
  }, []);
  const onFinished = useCallback(() => { void refresh(); }, [refresh]);

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
        api<Thread[]>(`/api/threads?limit=${pageSize}`),
        api<{ launch_enabled: boolean }>("/api/ui-config"),
      ]);
      setThreads(rows); setHasMore(rows.length === pageSize); setLaunch(config.launch_enabled);
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

  async function newChat() {
    setCreating(true); setError("");
    try {
      const thread = await api<Thread>("/api/threads", { method: "POST" });
      setThreads((rows) => [thread, ...rows]); select(thread);
    } catch (e) { setError(String(e)); }
    finally { setCreating(false); }
  }

  async function more() {
    try {
      const rows = await api<Thread[]>(`/api/threads?limit=${pageSize}&offset=${threads.length}`);
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
      <div className="sidebar-heading">CONVERSATIONS</div>
      <nav className="thread-list">
        {threads.map((thread) => <div key={thread.id} className="thread-row">
          {confirming === thread.id ? <div className="thread-confirm" role="group" aria-label="Confirm deletion">
            <span>Delete this conversation?</span>
            <button className="danger" onClick={() => void remove(thread)}>Delete</button>
            <button onClick={() => setConfirming(null)}>Cancel</button>
          </div> : <>
            <button className="thread-select" disabled={busy}
              aria-current={active?.id === thread.id ? "page" : undefined}
              onClick={() => select(thread)}>
              <span>{thread.title || "New conversation"}</span>
              <time dateTime={thread.updated_at}>{new Date(thread.updated_at).toLocaleDateString(undefined, {
                month: "short", day: "numeric",
              })}</time>
            </button>
            <button className="thread-delete" disabled={busy} aria-label="Delete conversation"
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
        {launch && <a className="label" href="/launch" target="_blank" rel="noreferrer">Open privileged session ↗</a>}
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
