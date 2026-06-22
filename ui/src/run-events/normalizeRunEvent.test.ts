import { describe, expect, it } from "vitest";
import { normalizeRunEvent } from "./normalizeRunEvent";
import { collectEvidenceBadges } from "./evidenceBadges";

const SAMPLE = {
  thinking: {
    type: "thinking",
    seq: 5,
    at: "2026-06-18T05:19:47Z",
    text: "Avoid: Glob when known_files has readable paths",
  },
  planCreated: {
    type: "task",
    seq: 4,
    status: "plan.created",
    text: "intent=benchmark_analysis next=Read known_files=1",
  },
  evidence: {
    type: "task",
    seq: 4,
    status: "evidence.collected",
    text: "runtime_score:success=100.0",
    data: { evidence: ["runtime_score:success=100.0"], source: "Read" },
  },
  toolRunning: {
    type: "tool_call",
    seq: 6,
    name: "Read",
    status: "running",
    args: { path: "/tmp/benchmark-runtime-score.json" },
  },
  toolCompleted: {
    type: "tool_call",
    seq: 6,
    name: "Read",
    status: "completed",
    args: { path: "/tmp/benchmark-cursor-agent.json" },
  },
  finalReady: {
    type: "task",
    seq: 5,
    status: "final.ready",
    text: "required evidence satisfied",
  },
  finished: {
    type: "status",
    seq: 9,
    status: "finished",
    message: "run completed",
  },
};

describe("normalizeRunEvent", () => {
  it("maps thinking to collapsible summary", () => {
    const n = normalizeRunEvent(SAMPLE.thinking);
    expect(n.type).toBe("thinking");
    expect(n.label).toBe("Thinking");
    expect(n.detail).toContain("Glob");
  });

  it("maps plan.created task", () => {
    const n = normalizeRunEvent(SAMPLE.planCreated);
    expect(n.label).toBe("Plan created");
    expect(n.status).toBe("plan.created");
  });

  it("maps evidence.collected with data tags", () => {
    const n = normalizeRunEvent(SAMPLE.evidence);
    expect(n.label).toBe("Evidence collected");
    expect(n.detail).toContain("runtime_score");
  });

  it("maps tool_call running with basename target", () => {
    const n = normalizeRunEvent(SAMPLE.toolRunning);
    expect(n.type).toBe("tool_call");
    expect(n.label).toContain("Read");
    expect(n.label).toContain("benchmark-runtime-score.json");
  });

  it("maps tool_call completed", () => {
    const n = normalizeRunEvent(SAMPLE.toolCompleted);
    expect(n.label).toContain("completed");
  });

  it("maps final.ready", () => {
    const n = normalizeRunEvent(SAMPLE.finalReady);
    expect(n.label).toBe("Ready to answer");
  });

  it("maps status finished", () => {
    const n = normalizeRunEvent(SAMPLE.finished);
    expect(n.status).toBe("finished");
    expect(n.detail).toBe("run completed");
  });
});

describe("collectEvidenceBadges", () => {
  it("lights badges from evidence.collected events", () => {
    const events = [
      normalizeRunEvent(SAMPLE.evidence),
      normalizeRunEvent({
        type: "task",
        seq: 5,
        status: "evidence.collected",
        data: { evidence: ["agent_benchmark:pass_rate=85.7"] },
      }),
      normalizeRunEvent({
        type: "task",
        seq: 6,
        status: "evidence.collected",
        data: { evidence: ["flow_phase:tool_planning"] },
      }),
    ];
    const badges = collectEvidenceBadges(events);
    expect(badges.every((b) => b.collected)).toBe(true);
    expect(badges.map((b) => b.key)).toEqual([
      "runtime_score",
      "agent_benchmark",
      "flow_phase",
    ]);
  });
});
