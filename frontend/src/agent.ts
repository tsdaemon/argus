import { HttpAgent, runHttpRequest, transformHttpEventStream } from "@ag-ui/client";
import type { RunAgentInput } from "@ag-ui/core";

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
    return super.requestInit({
      ...input,
      state: {},
      messages: input.resume?.length ? [] : input.messages.filter((m) => m.role === "user").slice(-1),
    });
  }
}

export interface Thread {
  id: string;
  title: string | null;
  created_at: string;
  updated_at: string;
}

export async function deleteThread(id: string): Promise<void> {
  const response = await fetch(`/api/threads/${id}`, { method: "DELETE" });
  // 404 means it is already gone, which is the state the caller wanted.
  if (!response.ok && response.status !== 404) {
    throw new Error(`Could not delete the conversation (${response.status}).`);
  }
}

export async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, init);
  if (!response.ok) throw new Error(`Could not load argus (${response.status}). Check the server and database, then retry.`);
  return response.json() as Promise<T>;
}
