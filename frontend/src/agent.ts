import { HttpAgent, runHttpRequest, transformHttpEventStream } from "@ag-ui/client";
import type { RunAgentInput } from "@ag-ui/core";

/** `forwardedProps` key of a run that continues an interrupted one (`argus.agent.api.RESUME_PROP`). */
export const resumeProp = "argus_resume";
type ResumeProps = Record<string, unknown>;

/** A run that stopped partway (e.g. the server restarted) and the calls a resume would run. */
export interface RunState {
  stalled: boolean;
  pending: { name: string; args: Record<string, unknown>; class: "read" | "mutate" | "destructive" }[];
}

/** CopilotKit's chat lifecycle connects through this read-only history stream. */
export class ArgusHttpAgent extends HttpAgent {
  protected connect(input: RunAgentInput) {
    return transformHttpEventStream(runHttpRequest(() => this.fetch(
      `/api/threads/${encodeURIComponent(input.threadId)}/connect?run_id=${encodeURIComponent(input.runId)}`,
      { headers: { Accept: "text/event-stream" }, signal: this.abortController.signal },
    )));
  }

  protected requestInit(input: RunAgentInput): RequestInit {
    // The transcript may include archived messages outside the model's summarized
    // context. Send only the new user turn; LangGraph owns the rest of its state.
    // An approval or a resume of an interrupted run continues from the checkpoint: no messages.
    const continuing = Boolean(input.resume?.length || (input.forwardedProps as ResumeProps | undefined)?.[resumeProp]);
    return super.requestInit({
      ...input,
      state: {},
      messages: continuing ? [] : input.messages.filter((m) => m.role === "user").slice(-1).map(inlineTextFiles),
    });
  }
}

export interface Thread {
  source: "human" | "agent";
  author?: { name: string; token_id: string } | null;
  id: string;
  title: string | null;
  /** null while the title is the first message's opening. */
  title_source: "generated" | "user" | null;
  created_at: string;
  updated_at: string;
  /** Model spend so far, in USD. */
  cost_usd: number;
}

export function formatUsd(usd: number): string {
  if (usd >= 1) return `$${usd.toFixed(2)}`;
  return usd >= 0.001 ? `$${usd.toFixed(3)}` : "<$0.001";
}

/** The session is gone or was never there: go through the login form and come back. */
function toLogin(): never {
  window.location.assign(`/login?next=${encodeURIComponent(window.location.pathname + window.location.search)}`);
  throw new Error("Login required.");
}

export async function deleteThread(id: string): Promise<void> {
  const response = await fetch(`/api/threads/${id}`, { method: "DELETE" });
  if (response.status === 401) toLogin();
  // 404 means it is already gone, which is the state the caller wanted.
  if (!response.ok && response.status !== 404) {
    throw new Error(`Could not delete the conversation (${response.status}).`);
  }
}

export async function renameThread(id: string, title: string): Promise<Thread> {
  const response = await fetch(`/api/threads/${id}`, {
    method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ title }),
  });
  if (response.status === 401) toLogin();
  if (!response.ok) throw new Error(`Could not rename the conversation (${response.status}).`);
  return response.json() as Promise<Thread>;
}

export async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, init);
  if (response.status === 401) toLogin();
  if (!response.ok) throw new Error(`Could not load argus (${response.status}). Check the server and database, then retry.`);
  return response.json() as Promise<T>;
}

// Attachments the chat accepts: images and PDFs go to the model as they are; text files
// (logs, configs) are inlined as text by `inlineTextFiles`.
export const attachmentAccept = [
  "image/*", "application/pdf", "text/*", "application/json", "application/xml",
  ".log", ".conf", ".cfg", ".ini", ".toml", ".yaml", ".yml", ".json", ".sh", ".md", ".csv",
].join(",");
export const attachmentMaxSize = 10 * 1024 * 1024;

const textMime = /^(text\/|application\/(json|xml|x-yaml|yaml|x-sh|toml))/;
const textExtension = /\.(log|conf|cfg|ini|toml|ya?ml|json|sh|md|csv|txt)$/i;

type Part = { type: string; text?: string; source?: { type: string; value?: string; mimeType?: string }; metadata?: unknown };

/** A text file attachment becomes a text part, since providers handle text documents
 * unevenly; images and PDFs pass through for the model to read natively. */
function inlineTextFiles<M extends { content?: unknown }>(message: M): M {
  if (!Array.isArray(message.content)) return message;
  return {
    ...message,
    content: (message.content as Part[]).map((part) => {
      const source = part.source;
      const filename = (part.metadata as { filename?: string } | undefined)?.filename ?? "";
      const isText = part.type === "document" && source?.type === "data" && source.value !== undefined &&
        (textMime.test(source.mimeType ?? "") || textExtension.test(filename));
      if (!isText) return part;
      const bytes = Uint8Array.from(atob(source!.value!), (c) => c.charCodeAt(0));
      const text = new TextDecoder().decode(bytes);
      return { type: "text", text: `Attached file ${filename || "(unnamed)"}:\n\`\`\`\n${text}\n\`\`\`` };
    }),
  };
}
