import { useState } from "react";
import { defineToolCallRenderer } from "@copilotkit/react-core/v2";

type Args = Record<string, unknown>;

// The one line shown next to the tool name; anything without a formatter shows its
// short scalar arguments.
const summaries: Record<string, (args: Args) => string> = {
  ssh_run: (a) => `${a.host ?? "?"} $ ${a.command ?? ""}`,
  task: (a) => String(a.description ?? ""),
  read_file: (a) => String(a.file_path ?? a.path ?? ""),
  write_file: (a) => String(a.file_path ?? a.path ?? ""),
  edit_file: (a) => String(a.file_path ?? a.path ?? ""),
};

export function summarize(name: string, args: Args): string {
  const custom = summaries[name];
  if (custom) return custom(args);
  return Object.entries(args)
    .filter(([, v]) => ["string", "number", "boolean"].includes(typeof v))
    .map(([k, v]) => `${k}: ${String(v)}`)
    .join(" · ");
}

// A classified call's result starts with `[risk: <class> · <note>]`
// (`risk_header` in argus.agent.classification).
const riskLine = /^\[risk: (read|mutate|destructive) · (.*)\]\n?/;

function splitRisk(result: string) {
  const match = riskLine.exec(result);
  if (!match) return { risk: null, note: "", output: result };
  return { risk: match[1], note: match[2], output: result.slice(match[0].length) };
}

const riskLabel: Record<string, string> = { read: "read", mutate: "change", destructive: "destructive" };

export const ToolCallCard = defineToolCallRenderer({
  name: "*",
  render: ({ name, args, result, status }) => {
    const [open, setOpen] = useState(false);
    const summary = summarize(name, (args ?? {}) as Args);
    const text = result === undefined ? undefined : typeof result === "string" ? result : JSON.stringify(result, null, 2);
    const { risk, note, output } = splitRisk(text ?? "");
    const state = String(status);

    return <div className="tool-call" data-open={open || undefined}>
      <button className="tool-call-head" onClick={() => setOpen(!open)} aria-expanded={open}>
        <span className="tool-call-chevron" aria-hidden="true">›</span>
        <span className="tool-call-name">{name}</span>
        {summary && <code className="tool-call-summary" title={summary}>{summary}</code>}
        {risk && <span className="tool-call-risk" data-risk={risk} title={note}>{riskLabel[risk]}</span>}
        <span className="tool-call-status" data-status={state}>
          {state === "complete" ? "done" : state === "executing" ? "running" : "pending"}
        </span>
      </button>
      {open && <div className="tool-call-body">
        {risk && <><div className="eyebrow">Classification</div><p className="tool-call-note">{note}</p></>}
        <div className="eyebrow">Arguments</div>
        <pre>{JSON.stringify(args ?? {}, null, 2)}</pre>
        {text !== undefined && <><div className="eyebrow">Result</div><pre>{output}</pre></>}
      </div>}
    </div>;
  },
});
