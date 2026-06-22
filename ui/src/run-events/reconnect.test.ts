import { describe, expect, it } from "vitest";
import {
  buildEventsUrl,
  connectRunEvents,
  eventsAfterSeq,
} from "./connectRunEvents";

type Listener = (event: MessageEvent) => void;

class MockEventSource {
  static instances: MockEventSource[] = [];
  url: string;
  onmessage: ((event: MessageEvent) => void) | null = null;
  onerror: (() => void) | null = null;
  private listeners = new Map<string, Listener[]>();
  closed = false;

  constructor(url: string) {
    this.url = url;
    MockEventSource.instances.push(this);
  }

  addEventListener(type: string, listener: Listener) {
    const list = this.listeners.get(type) ?? [];
    list.push(listener);
    this.listeners.set(type, list);
  }

  emitMessage(data: unknown) {
    const event = { data: JSON.stringify(data) } as MessageEvent;
    this.onmessage?.(event);
  }

  emitDone(status: string) {
    const event = { data: JSON.stringify({ status }) } as MessageEvent;
    for (const fn of this.listeners.get("done") ?? []) {
      fn(event);
    }
  }

  close() {
    this.closed = true;
  }
}

describe("buildEventsUrl", () => {
  it("includes after_seq for reconnect", () => {
    expect(buildEventsUrl("abc_0001", 3)).toBe(
      "/router/agent/runs/abc_0001/events?after_seq=3",
    );
  });
});

describe("eventsAfterSeq", () => {
  it("matches server filter semantics", () => {
    const events = [{ seq: 1 }, { seq: 3 }, { seq: 4 }, { seq: 9 }];
    expect(eventsAfterSeq(events, 3).map((e) => e.seq)).toEqual([4, 9]);
  });
});

describe("connectRunEvents reconnect", () => {
  it("tracks lastSeq and reconnects with after_seq", async () => {
    const Original = globalThis.EventSource;
    MockEventSource.instances = [];
    // @ts-expect-error test mock
    globalThis.EventSource = MockEventSource;

    const received: number[] = [];
    let doneStatus: string | undefined;

    const disconnect = connectRunEvents(
      "1781760132_0006",
      (ev) => received.push(ev.seq),
      (status) => {
        doneStatus = status;
      },
      { reconnectDelayMs: 10 },
    );

    const first = MockEventSource.instances[0];
    expect(first.url).toContain("after_seq=0");

    first.emitMessage({ type: "status", seq: 3, status: "running" });
    first.emitMessage({ type: "task", seq: 4, status: "evidence.collected" });
    first.emitDone("finished");

    expect(received).toEqual([3, 4]);
    expect(doneStatus).toBe("finished");
    expect(first.closed).toBe(true);

    first.onerror?.();
    await new Promise((r) => setTimeout(r, 20));

    const second = MockEventSource.instances[1];
    expect(second).toBeDefined();
    expect(second.url).toContain("after_seq=4");

    disconnect();
    globalThis.EventSource = Original;
  });

  it("normalizes events before callback", () => {
    const Original = globalThis.EventSource;
    MockEventSource.instances = [];
    // @ts-expect-error test mock
    globalThis.EventSource = MockEventSource;

    let label = "";
    const disconnect = connectRunEvents("run", (ev) => {
      label = ev.label;
    });

    MockEventSource.instances[0].emitMessage({
      type: "task",
      seq: 5,
      status: "final.ready",
      text: "required evidence satisfied",
    });

    expect(label).toBe("Ready to answer");

    disconnect();
    globalThis.EventSource = Original;
  });
});
