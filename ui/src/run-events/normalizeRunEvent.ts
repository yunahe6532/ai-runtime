import type { NormalizedRunEvent, RawRunEvent, RunEventType } from "./types";

const SDK_TYPES = new Set<RunEventType>([
  "system",
  "user",
  "assistant",
  "thinking",
  "task",
  "tool_call",
  "status",
  "request",
]);

function basename(path: string): string {
  const parts = path.replace(/\\/g, "/").split("/");
  return parts[parts.length - 1] || path;
}

function toolTarget(ev: RawRunEvent): string {
  const args = ev.args;
  if (args && typeof args.path === "string") {
    return basename(args.path);
  }
  if (typeof ev.target === "string") {
    return basename(ev.target);
  }
  const dataTarget = ev.data?.target;
  if (typeof dataTarget === "string") {
    return basename(dataTarget);
  }
  return "";
}

function taskLabel(ev: RawRunEvent): string {
  const status = ev.status ?? "";
  if (status === "plan.created") return "Plan created";
  if (status === "evidence.collected") return "Evidence collected";
  if (status === "final.ready") return "Ready to answer";
  if (status === "proxy.built") return "Proxy built";
  return status || "Task";
}

function messageText(ev: RawRunEvent): string {
  const msg = ev.message;
  if (typeof msg === "string") return msg;
  if (msg && typeof msg === "object" && "content" in msg) {
    const content = msg.content;
    if (typeof content === "string") return content;
    if (Array.isArray(content)) {
      return content
        .map((part) => {
          if (part && typeof part === "object" && "text" in part) {
            return String((part as { text?: string }).text ?? "");
          }
          return "";
        })
        .filter(Boolean)
        .join("\n");
    }
  }
  return "";
}

function coerceType(rawType: string | undefined): RunEventType {
  if (rawType && SDK_TYPES.has(rawType as RunEventType)) {
    return rawType as RunEventType;
  }
  return "status";
}

export function normalizeRunEvent(ev: RawRunEvent): NormalizedRunEvent {
  const seq = typeof ev.seq === "number" ? ev.seq : 0;
  const ts = typeof ev.at === "string" ? ev.at : undefined;
  const type = coerceType(ev.type);

  if (type === "thinking") {
    return {
      seq,
      type,
      label: "Thinking",
      detail: ev.text ?? ev.summary ?? "",
      ts,
      raw: ev,
    };
  }

  if (type === "task") {
    const status = ev.status ?? "";
    let detail = ev.text ?? "";
    if (status === "evidence.collected" && ev.data?.evidence?.length) {
      detail = ev.data.evidence.join(", ");
    }
    if (!detail && ev.data) {
      detail = JSON.stringify(ev.data);
    }
    return {
      seq,
      type,
      label: taskLabel(ev),
      detail,
      status,
      ts,
      raw: ev,
    };
  }

  if (type === "tool_call") {
    const name = ev.name ?? ev.tool ?? "Tool";
    const target = toolTarget(ev);
    const status = ev.status ?? "";
    const label =
      status === "running"
        ? `${name} ${target}`.trim()
        : `${name} ${status}`.trim();
    return {
      seq,
      type,
      label,
      detail: ev.guard_reason ?? target,
      status,
      ts,
      raw: ev,
    };
  }

  if (type === "status") {
    return {
      seq,
      type,
      label: ev.status ?? "Status",
      detail: typeof ev.message === "string" ? ev.message : "",
      status: ev.status,
      ts,
      raw: ev,
    };
  }

  if (type === "user") {
    return {
      seq,
      type,
      label: "User",
      detail: messageText(ev) || ev.text || ev.content || "",
      ts,
      raw: ev,
    };
  }

  if (type === "assistant") {
    return {
      seq,
      type,
      label: "Assistant",
      detail: ev.text ?? ev.content ?? messageText(ev),
      ts,
      raw: ev,
    };
  }

  if (type === "request") {
    return {
      seq,
      type,
      label: "Request",
      detail: ev.text ?? JSON.stringify(ev.data ?? {}),
      status: ev.status,
      ts,
      raw: ev,
    };
  }

  return {
    seq,
    type: "system",
    label: type === "system" ? "System" : String(ev.type ?? "event"),
    detail: ev.text ?? ev.content ?? messageText(ev),
    status: ev.status,
    ts,
    raw: ev,
  };
}
