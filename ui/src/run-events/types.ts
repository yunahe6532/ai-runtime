/** Cursor SDK-aligned stream event types (public beta surface). */
export type RunEventType =
  | "system"
  | "user"
  | "assistant"
  | "thinking"
  | "task"
  | "tool_call"
  | "status"
  | "request";

export type RawRunEvent = {
  type?: string;
  seq?: number;
  at?: string;
  status?: string;
  text?: string;
  summary?: string;
  message?: string | { role?: string; content?: unknown };
  name?: string;
  tool?: string;
  target?: string;
  guard_reason?: string;
  args?: Record<string, unknown>;
  data?: {
    evidence?: string[];
    source?: string;
    target?: string;
  };
  content?: string;
  [key: string]: unknown;
};

export type NormalizedRunEvent = {
  seq: number;
  type: RunEventType;
  label: string;
  detail?: string;
  status?: string;
  ts?: string;
  raw: RawRunEvent;
};

export type EvidenceBadgeKey = "runtime_score" | "agent_benchmark" | "flow_phase";

export type EvidenceBadge = {
  key: EvidenceBadgeKey;
  label: string;
  collected: boolean;
  detail?: string;
};

export type RunMeta = {
  run_id: string;
  status: string;
  turn_index?: number;
  parent_run_id?: string;
  query?: string;
  intent?: string;
  phase?: string;
};
