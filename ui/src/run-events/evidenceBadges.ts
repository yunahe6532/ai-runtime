import type { EvidenceBadge, EvidenceBadgeKey, NormalizedRunEvent } from "./types";

const BADGE_DEFS: { key: EvidenceBadgeKey; label: string; match: (tag: string) => boolean }[] = [
  {
    key: "runtime_score",
    label: "runtime_score",
    match: (tag) => tag.toLowerCase().includes("runtime_score"),
  },
  {
    key: "agent_benchmark",
    label: "agent_benchmark",
    match: (tag) => tag.toLowerCase().includes("agent_benchmark"),
  },
  {
    key: "flow_phase",
    label: "flow_phase",
    match: (tag) => tag.toLowerCase().includes("flow_phase"),
  },
];

function evidenceTagsFromEvent(ev: NormalizedRunEvent): string[] {
  if (ev.type !== "task" || ev.status !== "evidence.collected") {
    return [];
  }
  const fromData = ev.raw.data?.evidence;
  if (Array.isArray(fromData)) {
    return fromData.map(String);
  }
  if (ev.detail) {
    return ev.detail.split(",").map((s) => s.trim()).filter(Boolean);
  }
  return [];
}

export function collectEvidenceBadges(events: NormalizedRunEvent[]): EvidenceBadge[] {
  const collected = new Map<EvidenceBadgeKey, string>();

  for (const ev of events) {
    for (const tag of evidenceTagsFromEvent(ev)) {
      for (const def of BADGE_DEFS) {
        if (def.match(tag) && !collected.has(def.key)) {
          collected.set(def.key, tag);
        }
      }
    }
  }

  return BADGE_DEFS.map((def) => ({
    key: def.key,
    label: def.label,
    collected: collected.has(def.key),
    detail: collected.get(def.key),
  }));
}
