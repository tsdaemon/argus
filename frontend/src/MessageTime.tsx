// The server stamps archived messages with `metadata.argus_time` (when argus first saw them).
// CopilotKit keeps its own copy of a message it already shows, so a message that arrived in
// this page's live run lacks the stamp until a reload; it uses the time the page first saw it.
export interface Stamp { at: Date; firstOfDay: boolean }

type Stamped = { id: string; metadata?: unknown };

function sentAt(message: Stamped): Date | null {
  const at = (message.metadata as { argus_time?: string } | undefined)?.argus_time;
  return at ? new Date(at) : null;
}

function dayKey(date: Date) {
  return `${date.getFullYear()}-${date.getMonth()}-${date.getDate()}`;
}

export function stampMessages(messages: Stamped[], seen: Map<string, Date>): Map<string, Stamp> {
  const stamps = new Map<string, Stamp>();
  let previousDay = "";
  for (const message of messages) {
    let at = sentAt(message);
    if (!at) {
      if (!seen.has(message.id)) seen.set(message.id, new Date());
      at = seen.get(message.id)!;
    }
    const day = dayKey(at);
    stamps.set(message.id, { at, firstOfDay: day !== previousDay });
    previousDay = day;
  }
  return stamps;
}

/** "Today" and "Yesterday" in the browser's language, else a date; the year only when it differs. */
export function dayLabel(at: Date, now = new Date()): string {
  const midnight = (d: Date) => new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime();
  const days = Math.round((midnight(at) - midnight(now)) / 86_400_000);
  if (days === 0 || days === -1) {
    const word = new Intl.RelativeTimeFormat(undefined, { numeric: "auto" }).format(days, "day");
    return word.charAt(0).toUpperCase() + word.slice(1);
  }
  return at.toLocaleDateString(undefined, {
    weekday: "short", day: "numeric", month: "short",
    ...(at.getFullYear() !== now.getFullYear() ? { year: "numeric" } : {}),
  });
}

export function MessageTime({ stamp, part, align }: {
  stamp: Stamp | undefined; part: "day" | "time"; align?: "start" | "end";
}) {
  if (!stamp) return null;
  if (part === "day") {
    return stamp.firstOfDay ? <div className="day-divider" role="separator"><span>{dayLabel(stamp.at)}</span></div> : null;
  }
  return <time className="message-time" data-align={align} dateTime={stamp.at.toISOString()}
    title={stamp.at.toLocaleString()}>
    {stamp.at.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" })}
  </time>;
}
