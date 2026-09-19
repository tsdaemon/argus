import { useState } from "react";
import type { InterruptRenderProps } from "@copilotkit/react-core/v2";

type Action = { name: string; args: unknown; description?: string };
type Review = { allowed_decisions: string[] };
type Request = { action_requests: Action[]; review_configs: Review[] };

export function Approvals({ interrupts, resolve }: InterruptRenderProps) {
  const [decisions, setDecisions] = useState<Record<string, "approve" | "reject">>({});
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState("");
  const requests = interrupts.map((interrupt) => ({
    id: interrupt.id,
    request: (interrupt.metadata?.langgraph as { raw?: Request } | undefined)?.raw,
  }));
  const valid = requests.length > 0 && requests.every(({ request }) =>
    request && Array.isArray(request.action_requests) && request.action_requests.length > 0 &&
    request.action_requests.length === request.review_configs?.length,
  );
  const ready = valid && requests.every(({ id, request }) => request!.action_requests.every(
    (_, index) => Boolean(decisions[`${id}:${index}`]),
  ));

  async function submit() {
    setSubmitting(true);
    setError("");
    try {
      // CopilotKit collects ID-addressed answers and resumes once every open
      // interrupt is answered, including simultaneous worker approvals.
      for (const { id, request } of requests) {
        await resolve({ decisions: request!.action_requests.map((_, index) => ({
          type: decisions[`${id}:${index}`],
        })) }, id);
      }
    } catch {
      setError("Could not submit the decision. Reload this conversation to check what is still pending.");
      setSubmitting(false);
    }
  }

  return <section className="approval" aria-label="Tool approval">
    <div className="eyebrow">Your decision</div>
    <h2>Review before argus acts</h2>
    <p>Choose whether to allow each requested operation.</p>
    {!valid && <p role="alert">This request cannot be displayed. Reload the conversation to check its status.</p>}
    {valid && requests.map(({ id, request }) => <div key={id}>
      {request!.action_requests.map((action, index) => <fieldset key={index} disabled={submitting}>
        <legend>{action.name}</legend>
        {action.description && <p>{action.description}</p>}
        <pre>{JSON.stringify(action.args, null, 2)}</pre>
        <div className="decision-buttons">
          {(["reject", "approve"] as const).map((decision) => <button
            key={decision} type="button"
            disabled={!request!.review_configs[index].allowed_decisions.includes(decision)}
            aria-pressed={decisions[`${id}:${index}`] === decision}
            onClick={() => setDecisions((current) => ({ ...current, [`${id}:${index}`]: decision }))}
          >{decision === "approve" ? "Approve" : "Reject"}</button>)}
        </div>
      </fieldset>)}
    </div>)}
    {error && <p role="alert">{error}</p>}
    <button className="primary" disabled={!ready || submitting} onClick={() => void submit()}>
      {submitting ? "Submitting…" : "Submit decisions"}
    </button>
  </section>;
}
