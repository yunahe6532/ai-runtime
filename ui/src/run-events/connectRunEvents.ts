import { normalizeRunEvent } from "./normalizeRunEvent";
import type { NormalizedRunEvent, RawRunEvent } from "./types";

export type ConnectRunEventsOptions = {
  baseUrl?: string;
  reconnectDelayMs?: number;
  initialAfterSeq?: number;
};

export function buildEventsUrl(
  runId: string,
  afterSeq: number,
  baseUrl = "",
): string {
  const prefix = baseUrl.replace(/\/$/, "");
  return `${prefix}/router/agent/runs/${encodeURIComponent(runId)}/events?after_seq=${afterSeq}`;
}

export function connectRunEvents(
  runId: string,
  onEvent: (event: NormalizedRunEvent) => void,
  onDone?: (status?: string) => void,
  options: ConnectRunEventsOptions = {},
): () => void {
  const {
    baseUrl = "",
    reconnectDelayMs = 1000,
    initialAfterSeq = 0,
  } = options;

  let lastSeq = initialAfterSeq;
  let es: EventSource | null = null;
  let closed = false;
  let reconnectTimer: ReturnType<typeof setTimeout> | null = null;

  const clearReconnect = () => {
    if (reconnectTimer) {
      clearTimeout(reconnectTimer);
      reconnectTimer = null;
    }
  };

  const connect = () => {
    if (closed) return;
    clearReconnect();
    es?.close();
    es = new EventSource(buildEventsUrl(runId, lastSeq, baseUrl));

    es.onmessage = (msg) => {
      try {
        const raw = JSON.parse(msg.data) as RawRunEvent;
        if (typeof raw.seq === "number") {
          lastSeq = Math.max(lastSeq, raw.seq);
        }
        onEvent(normalizeRunEvent(raw));
      } catch {
        // ignore malformed frames
      }
    };

    es.addEventListener("done", (evt) => {
      let status: string | undefined;
      try {
        const data = JSON.parse((evt as MessageEvent).data) as { status?: string };
        status = data.status;
      } catch {
        status = undefined;
      }
      onDone?.(status);
      es?.close();
      es = null;
    });

    es.onerror = () => {
      es?.close();
      es = null;
      if (!closed) {
        reconnectTimer = setTimeout(connect, reconnectDelayMs);
      }
    };
  };

  connect();

  return () => {
    closed = true;
    clearReconnect();
    es?.close();
    es = null;
  };
}

/** Test helper: seq filter matching server-side after_seq semantics. */
export function eventsAfterSeq<T extends { seq?: number }>(
  events: T[],
  afterSeq: number,
): T[] {
  return events.filter((ev) => (ev.seq ?? 0) > afterSeq);
}
