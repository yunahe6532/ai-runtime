"""LLM-first read-only exploration — Cursor-like tool choice, evidence-based final gate."""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from typing import Any

from adapters.memory import SessionState
from .planner import AgentPlan

LOG = logging.getLogger("router.read_only_explorer")

READ_ONLY_EXPLORER_ENABLED = os.getenv("READ_ONLY_EXPLORER_ENABLED", "1") == "1"
READ_ONLY_EXPLORER_OVERRIDE = os.getenv("READ_ONLY_EXPLORER_OVERRIDE", "1") == "1"
READ_ONLY_STATIC_FALLBACK = os.getenv("READ_ONLY_STATIC_FALLBACK", "1") == "1"
READ_ONLY_EXPLORER_MAX_TOKENS = int(os.getenv("READ_ONLY_EXPLORER_MAX_TOKENS", "1536"))
READ_ONLY_MIN_DIR_STAGE = os.getenv("READ_ONLY_MIN_DIR_STAGE", "content")
READ_ONLY_SYNTHESIS_DEPTH = os.getenv("READ_ONLY_SYNTHESIS_DEPTH", "rich").strip().lower()
CROSS_TIER_GREP_PATTERN = os.getenv("READ_ONLY_CROSS_TIER_GREP", "import |from ")

STAGE_ORDER = {"none": 0, "inventory": 1, "content": 2, "boundary": 3, "anchor": 4}
SOURCE_TOOLS = frozenset({"ReadSource", "GrepSource", "GlobSource"})

# Hint patterns (LLM may choose any pattern; these help stage detection only)
WIDE_CONTENT_GREP = 'class |def |"""'
BOUNDARY_GREP = "import |from "
GREP_PATTERN_PROGRESSION = (WIDE_CONTENT_GREP, BOUNDARY_GREP, ".")
TIER_DIR_NAMES = ("runtime_core", "adapters", "legacy", "integrations")
MAX_EXPLORATION_ACTIONS = int(os.getenv("READ_ONLY_MAX_EXPLORATION_ACTIONS", "48"))

EXPLORATION_STRATEGY = """\
You are a read-only codebase analyst (like Cursor Agent). Plan the single best NEXT tool call.

Principles (guidelines — choose freely, do not follow a fixed script):
- Prefer official docs (MODULE_MAP, ARCHITECTURE, INTEGRATIONS) before guessing from filenames.
- Mix tools: ReadSource for files, GlobSource to inventory dirs, GrepSource for roles/imports/cross-refs.
- After inventory, deepen with Grep (class/def/docstring/import) or ReadSource on anchor files surfaced by Grep.
- Map tier boundaries (runtime_core vs adapters vs legacy vs integrations) using docs + import wiring.
- Set allow_final=true only when exploration_depth_sufficient is true AND you can answer with cited evidence.
- Never repeat a failed or shallow tool result; pick a different source or tool type.
- Read source_digests first — they are chunked grep/read summaries already collected.
- Do not repeat any action listed in exploration_actions_tried; advance pattern or source_id.
- Read exploration_checklist — each key is pending|done; never re-run done items.
- Check synthesis_evidence_dimensions — allow_final only when required dimensions are ok.
- For rich answers: read official docs first, then per-tier inventory→content→boundary grep, then anchor ReadSource on key files from grep.
- Use source_id only — do not invent paths.
"""


def _canonical_grep_pattern(pattern: str) -> str:
    """Map LLM pattern variants to stable sig keys (avoid import|from.* vs import |from repeats)."""
    p = re.sub(r"\s+", " ", (pattern or ".").strip())
    compact = re.sub(r"\s+", "", p.lower())
    if compact in (".", ".*", "^.*$", "^.*"):
        return "."
    if "class" in compact or "def" in compact or '"""' in p:
        return WIDE_CONTENT_GREP
    if "import" in compact or "from" in compact:
        return BOUNDARY_GREP
    return p or "."


def exploration_action_sig(
    tool: str,
    source_id: str,
    *,
    pattern: str = "",
    glob_pattern: str = "",
) -> str:
    t = str(tool or "").strip()
    sid = str(source_id or "").strip()
    if t in ("Grep", "GrepSource"):
        return f"grep:{sid}:{_canonical_grep_pattern(pattern)}"
    if t in ("Glob", "GlobSource"):
        return f"glob:{sid}:{glob_pattern or '*.py'}"
    if t in ("Read", "ReadSource"):
        return f"read:{sid}"
    return f"{t.lower()}:{sid}"


def record_exploration_action(
    plan_dict: dict[str, Any],
    tool: str,
    source_id: str,
    *,
    pattern: str = "",
    glob_pattern: str = "",
) -> str:
    sig = exploration_action_sig(tool, source_id, pattern=pattern, glob_pattern=glob_pattern)
    tried = list(dict.fromkeys(list(plan_dict.get("exploration_actions_tried") or []) + [sig]))
    plan_dict["exploration_actions_tried"] = tried[-MAX_EXPLORATION_ACTIONS:]
    return sig


def actions_tried_set(plan_dict: dict[str, Any]) -> set[str]:
    return set(plan_dict.get("exploration_actions_tried") or [])


@dataclass
class ExplorationDecision:
    thinking: str = ""
    allow_final: bool = False
    tool: str = ""
    source_id: str = ""
    pattern: str = ""
    glob_pattern: str = ""
    reason: str = ""
    source: str = "static"

    def to_next_action(self) -> dict[str, Any]:
        if self.allow_final or self.tool in ("answer", "final"):
            return {
                "tool": "answer",
                "target": "",
                "reason": self.reason or self.thinking[:200] or "explorer: sufficient evidence",
            }
        return {
            "tool": self.tool,
            "source_id": self.source_id,
            "pattern": self.pattern,
            "glob_pattern": self.glob_pattern,
            "target": self.source_id,
            "reason": self.reason or self.thinking[:200],
        }

    def to_tool_call(self) -> tuple[str, dict[str, Any]] | None:
        if self.allow_final or self.tool in ("answer", "final"):
            return None
        tool = str(self.tool or "").strip()
        sid = str(self.source_id or "").strip()
        if tool not in SOURCE_TOOLS or not sid:
            return None
        args: dict[str, Any] = {"source_id": sid}
        if tool == "GlobSource":
            args["glob_pattern"] = str(self.glob_pattern or "*.py").strip() or "*.py"
        elif tool == "GrepSource":
            pat = str(self.pattern or "").strip()
            if pat:
                args["pattern"] = pat
        return tool, args


def record_exploration_milestone(plan_dict: dict[str, Any], milestone: str) -> None:
    ms = list(dict.fromkeys(list(plan_dict.get("exploration_milestones") or []) + [milestone]))
    plan_dict["exploration_milestones"] = ms


def _milestones(plan_dict: dict[str, Any]) -> set[str]:
    return set(plan_dict.get("exploration_milestones") or [])


def record_source_exploration_stage(plan_dict: dict[str, Any], source_id: str, stage: str) -> None:
    stages = dict(plan_dict.get("source_exploration_stage") or {})
    cur = str(stages.get(source_id) or "none")
    if STAGE_ORDER.get(stage, 0) > STAGE_ORDER.get(cur, 0):
        stages[source_id] = stage
        plan_dict["source_exploration_stage"] = stages


def get_source_exploration_stage(plan_dict: dict[str, Any], source_id: str) -> str:
    return str((plan_dict.get("source_exploration_stage") or {}).get(source_id) or "none")


def dir_exploration_sufficient(plan_dict: dict[str, Any], source_id: str) -> bool:
    stage = get_source_exploration_stage(plan_dict, source_id)
    if STAGE_ORDER.get(stage, 0) < STAGE_ORDER.get(READ_ONLY_MIN_DIR_STAGE, 2):
        return False
    fc = int((plan_dict.get("source_grep_depth") or {}).get(source_id, 0) or 0)
    if fc > 0:
        return True
    if f"dir_content:{source_id}" in _milestones(plan_dict):
        return True
    digests = plan_dict.get("source_digests") or {}
    digest = str(digests.get(source_id) or "").strip()
    return len(digest) >= 120


def _dir_source_ids(plan_dict: dict[str, Any], registry: Any) -> list[str]:
    from .source_registry import SourceRegistry, required_source_ids_from_plan

    reg = registry if isinstance(registry, SourceRegistry) else SourceRegistry.from_dict(registry)
    required = required_source_ids_from_plan(plan_dict, reg)
    out: list[str] = []
    for sid in required:
        entry = reg.get(sid)
        if entry and entry.kind == "dir":
            out.append(sid)
    return out


def synthesis_evidence_dimensions(plan_dict: dict[str, Any], registry: Any) -> dict[str, str]:
    """LLM-visible evidence dimensions — ok | missing | partial (not a fixed playbook)."""
    from .source_registry import SourceRegistry

    reg = registry if isinstance(registry, SourceRegistry) else SourceRegistry.from_dict(registry)
    hits = set(plan_dict.get("source_hits") or [])
    ms = _milestones(plan_dict)
    tried = actions_tried_set(plan_dict)
    dims: dict[str, str] = {}

    summary_ids = list(plan_dict.get("summary_source_ids") or [])
    if summary_ids:
        unread = [s for s in summary_ids if s not in hits]
        dims["docs_read"] = "ok" if not unread else f"missing:{','.join(unread[:4])}"
    else:
        dims["docs_read"] = "ok"

    for sid in _dir_source_ids(plan_dict, reg):
        stage = get_source_exploration_stage(plan_dict, sid)
        content_sig = exploration_action_sig("GrepSource", sid, pattern=WIDE_CONTENT_GREP)
        boundary_sig = exploration_action_sig("GrepSource", sid, pattern=BOUNDARY_GREP)
        if (
            f"dir_content:{sid}" in ms
            or STAGE_ORDER.get(stage, 0) >= STAGE_ORDER.get("content", 2)
            or content_sig in tried
        ):
            dims[f"tier_content:{sid}"] = "ok"
        else:
            dims[f"tier_content:{sid}"] = "missing"
        if (
            f"dir_boundary:{sid}" in ms
            or STAGE_ORDER.get(stage, 0) >= STAGE_ORDER.get("boundary", 3)
            or boundary_sig in tried
        ):
            dims[f"tier_boundary:{sid}"] = "ok"
        else:
            dims[f"tier_boundary:{sid}"] = "partial"

    if "cross_tier:imports" in ms or any(
        exploration_action_sig("GrepSource", s, pattern=CROSS_TIER_GREP_PATTERN) in actions_tried_set(plan_dict)
        for s in _dir_source_ids(plan_dict, reg)
    ):
        dims["cross_tier_imports"] = "ok"
    else:
        dims["cross_tier_imports"] = "missing"

    anchor_reads = [c for c in (plan_dict.get("exploration_actions_tried") or []) if c.startswith("read:")]
    dims["anchor_file_reads"] = "ok" if len(anchor_reads) >= 2 else "partial"

    return dims


def build_exploration_checklist(plan_dict: dict[str, Any], registry: Any) -> dict[str, str]:
    """Stable pending/done map — explorer advances checklist keys like todos."""
    from .source_registry import SourceRegistry

    reg = registry if isinstance(registry, SourceRegistry) else SourceRegistry.from_dict(registry)
    tried = actions_tried_set(plan_dict)
    dims = synthesis_evidence_dimensions(plan_dict, reg)
    hits = set(plan_dict.get("source_hits") or [])
    checklist: dict[str, str] = {}

    summary_ids = list(plan_dict.get("summary_source_ids") or [])
    if summary_ids:
        unread = [s for s in summary_ids if s not in hits]
        checklist["docs_read"] = "done" if not unread else "pending"

    rich = READ_ONLY_SYNTHESIS_DEPTH not in ("standard", "minimal")
    for sid in _dir_source_ids(plan_dict, reg):
        glob_sig = exploration_action_sig("GlobSource", sid, glob_pattern="*.py")
        if glob_sig in tried or STAGE_ORDER.get(get_source_exploration_stage(plan_dict, sid), 0) >= STAGE_ORDER["inventory"]:
            checklist[f"glob:{sid}"] = "done"
        else:
            checklist[f"glob:{sid}"] = "pending"

        content_sig = exploration_action_sig("GrepSource", sid, pattern=WIDE_CONTENT_GREP)
        stage = get_source_exploration_stage(plan_dict, sid)
        if (
            content_sig in tried
            or dims.get(f"tier_content:{sid}") == "ok"
            or STAGE_ORDER.get(stage, 0) >= STAGE_ORDER["content"]
            or f"dir_content:{sid}" in _milestones(plan_dict)
        ):
            checklist[f"grep_content:{sid}"] = "done"
        else:
            checklist[f"grep_content:{sid}"] = "pending"

        if rich:
            boundary_sig = exploration_action_sig("GrepSource", sid, pattern=BOUNDARY_GREP)
            if (
                boundary_sig in tried
                or dims.get(f"tier_boundary:{sid}") == "ok"
                or STAGE_ORDER.get(stage, 0) >= STAGE_ORDER["boundary"]
                or f"dir_boundary:{sid}" in _milestones(plan_dict)
            ):
                checklist[f"grep_boundary:{sid}"] = "done"
            else:
                checklist[f"grep_boundary:{sid}"] = "pending"

    if rich:
        if dims.get("cross_tier_imports") == "ok":
            checklist["cross_tier_imports"] = "done"
        else:
            checklist["cross_tier_imports"] = "pending"

    return checklist


def _decision_for_checklist_key(
    key: str,
    plan_dict: dict[str, Any],
    reg: Any,
    *,
    tried: set[str],
) -> ExplorationDecision | None:
    from .source_registry import SourceRegistry

    reg_obj = reg if isinstance(reg, SourceRegistry) else SourceRegistry.from_dict(reg)
    if key == "docs_read":
        for sid in list(plan_dict.get("summary_source_ids") or []):
            sig = exploration_action_sig("ReadSource", sid)
            if sid not in set(plan_dict.get("source_hits") or []) and sig not in tried:
                return ExplorationDecision(
                    tool="ReadSource",
                    source_id=sid,
                    thinking=f"Checklist: read doc {sid}.",
                    reason="checklist: docs_read",
                    source="checklist",
                )
        return None
    if key.startswith("glob:"):
        sid = key.split(":", 1)[1]
        sig = exploration_action_sig("GlobSource", sid, glob_pattern="*.py")
        if sig not in tried:
            return ExplorationDecision(
                tool="GlobSource",
                source_id=sid,
                glob_pattern="*.py",
                thinking=f"Checklist: inventory {sid}.",
                reason="checklist: glob",
                source="checklist",
            )
        return None
    if key.startswith("grep_content:"):
        sid = key.split(":", 1)[1]
        sig = exploration_action_sig("GrepSource", sid, pattern=WIDE_CONTENT_GREP)
        if sig not in tried:
            return ExplorationDecision(
                tool="GrepSource",
                source_id=sid,
                pattern=WIDE_CONTENT_GREP,
                thinking=f"Checklist: content grep {sid}.",
                reason="checklist: grep_content",
                source="checklist",
            )
        return None
    if key.startswith("grep_boundary:"):
        sid = key.split(":", 1)[1]
        sig = exploration_action_sig("GrepSource", sid, pattern=BOUNDARY_GREP)
        if sig not in tried:
            return ExplorationDecision(
                tool="GrepSource",
                source_id=sid,
                pattern=BOUNDARY_GREP,
                thinking=f"Checklist: boundary grep {sid}.",
                reason="checklist: grep_boundary",
                source="checklist",
            )
        return None
    if key == "cross_tier_imports":
        for sid in _dir_source_ids(plan_dict, reg_obj):
            sig = exploration_action_sig("GrepSource", sid, pattern=CROSS_TIER_GREP_PATTERN)
            if sig not in tried:
                return ExplorationDecision(
                    tool="GrepSource",
                    source_id=sid,
                    pattern=CROSS_TIER_GREP_PATTERN,
                    thinking="Checklist: cross-tier import map.",
                    reason="checklist: cross_tier",
                    source="checklist",
                )
    return None


def log_exploration_plan(
    plan: AgentPlan,
    decision: ExplorationDecision,
    ctx: dict[str, Any],
    *,
    run_id: str = "",
) -> None:
    from explorer_trace import trace_explorer_plan

    plan_dict = plan.to_dict()
    checklist = plan_dict.get("exploration_checklist") or {}
    trace_explorer_plan(
        step=int(plan.step_count or 0),
        final_ready=bool(plan.final_ready),
        depth_ok=bool(ctx.get("exploration_depth_sufficient")),
        thinking=decision.thinking or "",
        decision_source=decision.source,
        next_tool=decision.tool or ("answer" if decision.allow_final else ""),
        next_sid=decision.source_id,
        next_pattern=decision.pattern or "",
        next_glob=decision.glob_pattern or "",
        reason=decision.reason or "",
        checklist=checklist,
        checklist_pending=[k for k, v in checklist.items() if v == "pending"],
        dims=ctx.get("synthesis_evidence_dimensions") or {},
        tried_count=len(plan_dict.get("exploration_actions_tried") or []),
        tried_tail=list(plan_dict.get("exploration_actions_tried") or []),
    )

def _required_dimensions_for_depth() -> list[str]:
    """Human-readable dimension keys for logging (explorer uses synthesis_evidence_dimensions)."""
    if READ_ONLY_SYNTHESIS_DEPTH in ("standard", "minimal"):
        return ["docs_read", "tier_content:*"]
    return ["docs_read", "tier_content:*", "tier_boundary:*", "cross_tier_imports"]


def exploration_checklist_pending(plan_dict: dict[str, Any], registry: Any) -> list[str]:
    checklist = plan_dict.get("exploration_checklist")
    if not isinstance(checklist, dict) or not checklist:
        checklist = build_exploration_checklist(plan_dict, registry)
    return [k for k, v in checklist.items() if v == "pending"]


def exploration_depth_sufficient(plan_dict: dict[str, Any], registry: Any) -> bool:
    """Evidence depth gate — dimension-based; explorer LLM fills gaps until ok."""
    from .source_registry import SourceRegistry

    reg = registry if isinstance(registry, SourceRegistry) else SourceRegistry.from_dict(registry)
    pending = exploration_checklist_pending(plan_dict, reg)
    if not pending:
        # #region agent log
        try:
            import json
            import time

            with open("/home/yunahe/.cursor/debug-694f50.log", "a", encoding="utf-8") as fh:
                fh.write(
                    json.dumps(
                        {
                            "sessionId": "694f50",
                            "location": "read_only_explorer.py:exploration_depth_sufficient",
                            "message": "depth_ok_via_empty_checklist",
                            "data": {"pending": pending},
                            "hypothesisId": "B",
                            "runId": "pre-fix",
                            "timestamp": int(time.time() * 1000),
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )
        except OSError:
            pass
        # #endregion
        return True
    dims = synthesis_evidence_dimensions(plan_dict, reg)
    summary_ids = list(plan_dict.get("summary_source_ids") or [])
    if summary_ids and dims.get("docs_read") != "ok":
        return False
    for sid in _dir_source_ids(plan_dict, reg):
        if dims.get(f"tier_content:{sid}") != "ok":
            return False
        if READ_ONLY_SYNTHESIS_DEPTH not in ("standard", "minimal"):
            if dims.get(f"tier_boundary:{sid}") != "ok":
                return False
    if READ_ONLY_SYNTHESIS_DEPTH not in ("standard", "minimal"):
        if dims.get("cross_tier_imports") != "ok":
            return False
    return True


def source_exploration_depth_passes(plan_dict: dict[str, Any], registry: Any) -> bool:
    return exploration_depth_sufficient(plan_dict, registry)


# Backward-compatible alias (tests / older call sites)
exploration_checklist_passes = exploration_depth_sufficient


def note_exploration_from_hit(
    plan_dict: dict[str, Any],
    source_id: str,
    *,
    tool: str = "",
    content: str = "",
    pattern: str = "",
    registry: Any = None,
) -> None:
    """Observational stage/milestone updates from successful tool hits."""
    from .source_registry import SourceRegistry

    reg = None
    if registry is not None:
        reg = registry if isinstance(registry, SourceRegistry) else SourceRegistry.from_dict(registry)
    entry = reg.get(source_id) if reg else None
    tool_l = str(tool or "").lower()
    pat = str(pattern or "").lower()
    low = (content or "")[:2000].lower()

    if tool_l in ("read", "readsource") or (entry and entry.kind == "file"):
        record_exploration_milestone(plan_dict, f"doc_read:{source_id}")
        record_source_exploration_stage(plan_dict, source_id, "anchor")
        return

    if tool_l in ("glob", "globsource"):
        record_source_exploration_stage(plan_dict, source_id, "inventory")
        return

    if tool_l in ("grep", "grepsource"):
        pat_norm = _canonical_grep_pattern(pat)
        if pat_norm == BOUNDARY_GREP or "import" in pat.replace("|", " ") or "from" in pat:
            record_source_exploration_stage(plan_dict, source_id, "boundary")
            record_exploration_milestone(plan_dict, f"dir_boundary:{source_id}")
            if "runtime_core" in low and any(t in low for t in ("legacy", "adapters", "integrations")):
                record_exploration_milestone(plan_dict, "cross_tier:imports")
            return
        if "__init__" in pat:
            record_exploration_milestone(plan_dict, f"dir_init:{source_id}")
            record_source_exploration_stage(plan_dict, source_id, "anchor")
            return
        if '"""' in pat or "class" in pat or "def" in pat or pat.strip() in (".", ""):
            record_source_exploration_stage(plan_dict, source_id, "content")
            record_exploration_milestone(plan_dict, f"dir_content:{source_id}")
            return
        if "<workspace_result" in low:
            record_source_exploration_stage(plan_dict, source_id, "content")


def _resolve_artifact_source_id(art: Any, reg: Any) -> str:
    from .source_registry import SourceRegistry, lookup_source_id_by_relpath

    reg_obj = reg if isinstance(reg, SourceRegistry) else SourceRegistry.from_dict(reg)
    path = str(getattr(art, "path", "") or "").strip()
    if not path:
        return ""
    sid = lookup_source_id_by_relpath(reg_obj, path)
    if sid:
        return sid
    for term in list(getattr(art, "index_terms", None) or [])[:6]:
        sid2 = lookup_source_id_by_relpath(reg_obj, str(term))
        if sid2:
            return sid2
    return ""


def _source_digests(
    state: SessionState | None,
    plan: AgentPlan,
    *,
    limit: int = 16,
) -> list[dict[str, Any]]:
    if state is None:
        return []
    from .source_registry import SourceRegistry

    reg = SourceRegistry.from_dict(plan.source_registry or {})
    by_sid: dict[str, dict[str, Any]] = {}
    try:
        from legacy.retriever import load_artifact_meta

        for aid in list(reversed(getattr(state, "artifacts", None) or []))[: limit * 3]:
            art = load_artifact_meta(aid, getattr(state, "project_key", "") or "")
            if not art or art.is_error:
                continue
            sid = _resolve_artifact_source_id(art, reg)
            if not sid:
                continue
            digest = (art.prompt_excerpt or "").strip()
            if not digest and art.excerpt_chunks:
                digest = "\n\n".join(str(c) for c in art.excerpt_chunks[:4] if str(c).strip())
            if not digest:
                digest = (art.summary or "").strip()
            if len(digest) < 40:
                continue
            prev = by_sid.get(sid)
            if prev and len(str(prev.get("digest") or "")) >= len(digest):
                continue
            by_sid[sid] = {
                "source_id": sid,
                "tool": art.name or art.type,
                "digest": digest[:1400],
                "chunk_count": len(art.excerpt_chunks or []),
                "chars": int(art.chars or 0),
            }
    except ImportError:
        return []
    return list(by_sid.values())[:limit]


def _sync_source_digests_to_plan(plan_dict: dict[str, Any], digests: list[dict[str, Any]]) -> None:
    merged = dict(plan_dict.get("source_digests") or {})
    for row in digests:
        sid = str(row.get("source_id") or "").strip()
        body = str(row.get("digest") or "").strip()
        if sid and body:
            merged[sid] = body[:2000]
    plan_dict["source_digests"] = merged


def _artifact_summaries(state: SessionState | None, plan: AgentPlan | None = None, limit: int = 12) -> list[dict[str, str]]:
    if state is None:
        return []
    if plan is not None:
        digests = _source_digests(state, plan, limit=limit)
        if digests:
            return [
                {
                    "tool": str(row.get("tool") or "digest"),
                    "path": str(row.get("source_id") or ""),
                    "summary": str(row.get("digest") or "")[:900],
                }
                for row in digests
            ]
    try:
        from .evidence_store import evidence_items_for_judge

        items = evidence_items_for_judge(state, limit=limit)
        if items:
            return items
    except ImportError:
        pass
    try:
        from legacy.retriever import load_artifact_meta

        out: list[dict[str, str]] = []
        for aid in list(reversed(getattr(state, "artifacts", None) or []))[:limit]:
            art = load_artifact_meta(aid, getattr(state, "project_key", "") or "")
            if not art:
                continue
            excerpt = (art.prompt_excerpt or art.summary or "")[:500]
            out.append({"tool": art.name or art.type, "path": art.path or "", "summary": excerpt})
        return out
    except ImportError:
        return []


def _evidence_gaps(plan_dict: dict[str, Any], registry: Any) -> list[str]:
    from .source_registry import SourceRegistry, pending_source_ids_for_plan

    reg = registry if isinstance(registry, SourceRegistry) else SourceRegistry.from_dict(registry)
    gaps: list[str] = []
    hits = set(plan_dict.get("source_hits") or [])
    for sid in list(plan_dict.get("summary_source_ids") or []):
        if sid not in hits:
            gaps.append(f"unread_doc:{sid}")
    for sid in pending_source_ids_for_plan(plan_dict, reg):
        stage = get_source_exploration_stage(plan_dict, sid)
        entry = reg.get(sid)
        kind = entry.kind if entry else "?"
        gaps.append(f"pending:{sid}({kind},stage={stage})")
    return gaps[:20]


def _registry_snapshot(plan: AgentPlan) -> list[dict[str, str]]:
    from .source_registry import SourceRegistry

    reg = SourceRegistry.from_dict(plan.source_registry or {})
    plan_dict = plan.to_dict()
    rows: list[dict[str, str]] = []
    for s in reg.available()[:28]:
        rows.append(
            {
                "source_id": s.id,
                "kind": s.kind,
                "label": s.label,
                "stage": get_source_exploration_stage(plan_dict, s.id),
                "file_hits": str((plan_dict.get("source_grep_depth") or {}).get(s.id, 0) or 0),
                "in_source_hits": str(s.id in set(plan_dict.get("source_hits") or [])),
            }
        )
    return rows


def build_exploration_context(state: SessionState | None, plan: AgentPlan, query: str) -> dict[str, Any]:
    plan_dict = plan.to_dict()
    from .source_registry import SourceRegistry, pending_source_ids_for_plan, required_source_ids_from_plan

    reg = SourceRegistry.from_dict(plan.source_registry or {})
    digests = _source_digests(state, plan)
    _sync_source_digests_to_plan(plan_dict, digests)
    plan.source_digests = dict(plan_dict.get("source_digests") or {})
    dims = synthesis_evidence_dimensions(plan_dict, reg)
    depth_ok = exploration_depth_sufficient(plan_dict, reg)
    return {
        "user_goal": (query or plan.goal or "")[:800],
        "exploration_strategy": EXPLORATION_STRATEGY,
        "synthesis_evidence_dimensions": dims,
        "synthesis_depth_mode": READ_ONLY_SYNTHESIS_DEPTH,
        "required_source_ids": required_source_ids_from_plan(plan_dict, reg),
        "pending_source_ids": pending_source_ids_for_plan(plan_dict, reg),
        "summary_source_ids": list(plan_dict.get("summary_source_ids") or []),
        "exploration_depth_sufficient": depth_ok,
        "exploration_stage_by_source": dict(plan_dict.get("source_exploration_stage") or {}),
        "evidence_gaps": _evidence_gaps(plan_dict, reg),
        "grep_file_counts": dict(plan_dict.get("source_grep_depth") or {}),
        "available_sources": _registry_snapshot(plan),
        "source_digests": digests,
        "exploration_actions_tried": list(plan_dict.get("exploration_actions_tried") or [])[-24:],
        "exploration_checklist": build_exploration_checklist(plan_dict, reg),
        "collected_evidence": _artifact_summaries(state, plan),
        "source_hits": list(plan_dict.get("source_hits") or [])[:20],
        "step_count": plan.step_count,
        "explore_round": int(getattr(state, "explore_round", 0) or 0) if state else 0,
    }


def _pick_next_untried_exploration(plan: AgentPlan, ctx: dict[str, Any]) -> ExplorationDecision:
    """Advance to next source/pattern — never repeat exploration_actions_tried."""
    from .source_registry import SourceRegistry

    reg = SourceRegistry.from_dict(plan.source_registry or {})
    plan_dict = plan.to_dict()
    tried = actions_tried_set(plan_dict)
    hits = set(plan_dict.get("source_hits") or [])

    checklist = build_exploration_checklist(plan_dict, reg)
    for key, status in checklist.items():
        if status != "pending":
            continue
        picked = _decision_for_checklist_key(key, plan_dict, reg, tried=tried)
        if picked is not None:
            return picked

    for sid in list(plan_dict.get("summary_source_ids") or []):
        sig = exploration_action_sig("ReadSource", sid)
        if sid not in hits and sig not in tried:
            return ExplorationDecision(
                tool="ReadSource",
                source_id=sid,
                thinking=f"Unread summary doc {sid}.",
                reason="untried: read doc",
                source="fallback",
            )

    pending = list(ctx.get("pending_source_ids") or [])
    for sid in pending:
        entry = reg.get(sid)
        if not entry:
            continue
        if entry.kind == "file":
            sig = exploration_action_sig("ReadSource", sid)
            if sig not in tried:
                return ExplorationDecision(
                    tool="ReadSource",
                    source_id=sid,
                    thinking=f"Read file source {sid}.",
                    reason="untried: read file",
                    source="fallback",
                )
            continue

        stage = get_source_exploration_stage(plan_dict, sid)
        if stage in ("none",):
            sig = exploration_action_sig("GlobSource", sid, glob_pattern="*.py")
            if sig not in tried:
                return ExplorationDecision(
                    tool="GlobSource",
                    source_id=sid,
                    glob_pattern="*.py",
                    thinking=f"Inventory dir {sid}.",
                    reason="untried: glob inventory",
                    source="fallback",
                )

        for pat in GREP_PATTERN_PROGRESSION:
            sig = exploration_action_sig("GrepSource", sid, pattern=pat)
            if sig not in tried:
                return ExplorationDecision(
                    tool="GrepSource",
                    source_id=sid,
                    pattern=pat,
                    thinking=f"Deepen dir {sid} with grep pattern={pat!r}.",
                    reason=f"untried: grep {pat[:24]}",
                    source="fallback",
                )

    dims = synthesis_evidence_dimensions(plan_dict, reg)
    if dims.get("cross_tier_imports") != "ok":
        for sid in _dir_source_ids(plan_dict, reg):
            sig = exploration_action_sig("GrepSource", sid, pattern=CROSS_TIER_GREP_PATTERN)
            if sig not in tried:
                return ExplorationDecision(
                    tool="GrepSource",
                    source_id=sid,
                    pattern=CROSS_TIER_GREP_PATTERN,
                    thinking="Map cross-tier import boundaries.",
                    reason="untried: cross-tier imports",
                    source="fallback",
                )

    for s in reg.available():
        if s.kind != "file" or s.id.startswith("doc."):
            continue
        sig = exploration_action_sig("ReadSource", s.id)
        if sig in tried:
            continue
        rel = s.relpath.replace("\\", "/").lower()
        dir_sid = ""
        for tier in TIER_DIR_NAMES:
            if f"/{tier}/" in rel or rel.endswith(f"/{tier}"):
                dir_sid = f"dir.{tier}"
                break
        if dir_sid and dims.get(f"tier_boundary:{dir_sid}") == "ok":
            return ExplorationDecision(
                tool="ReadSource",
                source_id=s.id,
                thinking=f"Read anchor file {s.id} for cited evidence.",
                reason="untried: anchor read",
                source="fallback",
            )

    if ctx.get("exploration_depth_sufficient"):
        return ExplorationDecision(
            allow_final=True,
            thinking="All pending sources explored; depth sufficient.",
            reason="untried: depth sufficient",
            source="fallback",
        )

    avail = [s.id for s in reg.available() if s.id not in hits]
    for sid in avail:
        entry = reg.get(sid)
        if not entry or entry.kind != "dir":
            continue
        sig = exploration_action_sig("GlobSource", sid, glob_pattern="*.py")
        if sig not in tried:
            return ExplorationDecision(
                tool="GlobSource",
                source_id=sid,
                glob_pattern="*.py",
                thinking=f"Fallback inventory for {sid}.",
                reason="untried: extra glob",
                source="fallback",
            )

    return ExplorationDecision(reason="untried: exhausted", source="fallback")


def _minimal_fallback_decision(plan: AgentPlan, ctx: dict[str, Any]) -> ExplorationDecision:
    """Emergency fallback when explorer LLM is unavailable — pick first untried gap."""
    if ctx.get("exploration_depth_sufficient"):
        return ExplorationDecision(
            allow_final=True,
            thinking="Fallback: exploration depth sufficient.",
            reason="fallback: depth sufficient",
            source="fallback",
        )
    return _pick_next_untried_exploration(plan, ctx)


def _extract_json_object(text: str) -> dict[str, Any] | None:
    if not text:
        return None
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(text[start : end + 1])
        return data if isinstance(data, dict) else None
    except json.JSONDecodeError:
        return None


def _normalize_decision(data: dict[str, Any], plan: AgentPlan, ctx: dict[str, Any]) -> ExplorationDecision:
    from .source_registry import SourceRegistry

    reg = SourceRegistry.from_dict(plan.source_registry or {})
    valid_ids = {s.id for s in reg.available()}
    tool = str(data.get("next_tool") or data.get("tool") or "").strip()
    sid = str(data.get("source_id") or "").strip()
    allow_final = bool(data.get("allow_final"))
    thinking = str(data.get("thinking") or data.get("reasoning") or "")[:1600]
    reason = str(data.get("reason") or thinking[:280])
    pattern = str(data.get("pattern") or "").strip()
    glob_pattern = str(data.get("glob_pattern") or "").strip()

    if allow_final:
        if ctx.get("exploration_depth_sufficient"):
            return ExplorationDecision(
                allow_final=True,
                thinking=thinking,
                reason=reason or "LLM: sufficient evidence",
                source="llm",
            )
        LOG.debug("read_only_explorer llm allow_final rejected depth_insufficient")
        return _minimal_fallback_decision(plan, ctx)

    if tool in ("answer", "final"):
        return _minimal_fallback_decision(plan, ctx)

    if tool not in SOURCE_TOOLS or sid not in valid_ids:
        LOG.debug("read_only_explorer llm invalid tool=%s sid=%s", tool, sid)
        return _pick_next_untried_exploration(plan, ctx)

    entry = reg.get(sid)
    if entry and entry.kind == "dir" and tool == "ReadSource":
        tool = "GrepSource"
        if not pattern:
            pattern = WIDE_CONTENT_GREP

    sig = exploration_action_sig(tool, sid, pattern=pattern, glob_pattern=glob_pattern or "*.py")
    if sig in actions_tried_set(plan.to_dict()):
        LOG.debug("read_only_explorer llm repeat blocked sig=%s", sig)
        return _pick_next_untried_exploration(plan, ctx)

    return ExplorationDecision(
        tool=tool,
        source_id=sid,
        pattern=pattern,
        glob_pattern=glob_pattern or "*.py",
        thinking=thinking,
        reason=reason,
        allow_final=False,
        source="llm",
    )


def llm_plan_read_only_exploration(ctx: dict[str, Any]) -> dict[str, Any] | None:
    schema = (
        "Return ONLY JSON: "
        '{"thinking":"why this step","allow_final":bool,'
        '"next_tool":"ReadSource|GrepSource|GlobSource|answer",'
        '"source_id":"dir.xxx or doc.yyy","pattern":"optional grep pattern",'
        '"glob_pattern":"optional e.g. *.py","reason":"short"}'
    )
    body = {
        "model": os.getenv("READ_ONLY_EXPLORER_MODEL", "fast"),
        "stream": False,
        "temperature": 0.2,
        "max_tokens": READ_ONLY_EXPLORER_MAX_TOKENS,
        "messages": [
            {
                "role": "system",
                "content": EXPLORATION_STRATEGY + "\n" + schema,
            },
            {"role": "user", "content": json.dumps(ctx, ensure_ascii=False)},
        ],
    }
    try:
        from adapters.gateway import chat_completion

        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
        result = chat_completion(
            method="POST",
            path="/v1/chat/completions",
            headers={"Content-Type": "application/json"},
            body_bytes=payload,
            body_json=body,
            backend_hint="fast",
            stream=False,
        )
        if getattr(result, "status_code", 500) != 200:
            LOG.warning("read_only_explorer llm status=%s", getattr(result, "status_code", "?"))
            return None
        choices = (getattr(result, "json_data", None) or {}).get("choices") or []
        content = str((choices[0].get("message") or {}).get("content") or "")
        return _extract_json_object(content)
    except Exception as exc:
        LOG.warning("read_only_explorer llm failed: %s", exc)
        return None


def plan_read_only_exploration(state: SessionState | None, plan: AgentPlan, query: str) -> ExplorationDecision:
    ctx = build_exploration_context(state, plan, query)
    if READ_ONLY_EXPLORER_ENABLED:
        raw = llm_plan_read_only_exploration(ctx)
        if isinstance(raw, dict):
            decision = _normalize_decision(raw, plan, ctx)
            LOG.debug(
                "read_only_explorer llm tool=%s sid=%s allow_final=%s source=%s pattern=%s glob=%s",
                decision.tool or "answer",
                decision.source_id,
                decision.allow_final,
                decision.source,
                (decision.pattern or "")[:40],
                (decision.glob_pattern or "")[:24],
            )
            return decision
    if READ_ONLY_STATIC_FALLBACK:
        decision = _minimal_fallback_decision(plan, ctx)
        LOG.debug(
            "read_only_explorer fallback tool=%s sid=%s allow_final=%s",
            decision.tool or "answer",
            decision.source_id,
            decision.allow_final,
        )
        return decision
    return ExplorationDecision(reason="explorer disabled", source="static")


def refresh_read_only_exploration_plan(
    state: SessionState | None,
    plan: AgentPlan,
    query: str,
) -> ExplorationDecision:
    from .source_registry import SourceRegistry

    decision = plan_read_only_exploration(state, plan, query)
    plan_dict = plan.to_dict()
    reg = SourceRegistry.from_dict(plan.source_registry or {})
    tried = actions_tried_set(plan_dict)
    if not decision.allow_final and decision.tool and decision.source_id:
        sig = exploration_action_sig(
            decision.tool,
            decision.source_id,
            pattern=decision.pattern,
            glob_pattern=decision.glob_pattern,
        )
        if sig in tried:
            ctx_skip = build_exploration_context(state, plan, query)
            decision = _pick_next_untried_exploration(plan, ctx_skip)
            if decision.source == "fallback":
                decision.source = "checklist_skip_tried"
    na = decision.to_next_action()
    plan.next_action = na
    checklist = build_exploration_checklist(plan_dict, reg)
    plan_dict["exploration_checklist"] = checklist
    pending = [k for k, v in checklist.items() if v == "pending"]

    if not decision.allow_final and not decision.tool and not pending:
        prior_thinking = str(plan_dict.get("exploration_thinking") or decision.thinking or "").strip()
        decision = ExplorationDecision(
            allow_final=True,
            thinking=prior_thinking or "Exploration checklist complete — synthesize from collected digests.",
            reason="checklist_complete",
            source="checklist",
        )
        na = decision.to_next_action()
        plan.next_action = na
        # #region agent log
        try:
            import json
            import time

            with open("/home/yunahe/.cursor/debug-694f50.log", "a", encoding="utf-8") as fh:
                fh.write(
                    json.dumps(
                        {
                            "sessionId": "694f50",
                            "location": "read_only_explorer.py:refresh_read_only_exploration_plan",
                            "message": "checklist_complete_allow_final",
                            "data": {"thinking_len": len(prior_thinking), "pending": pending},
                            "hypothesisId": "A",
                            "runId": "pre-fix",
                            "timestamp": int(time.time() * 1000),
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )
        except OSError:
            pass
        # #endregion

    if not decision.allow_final and decision.tool and decision.source_id:
        if plan.final_ready and state is not None:
            from .planner import revoke_final_ready

            revoke_final_ready(plan, state, "explorer_needs_tools", emit_run_events=False)

    plan.exploration_actions_tried = list(plan_dict.get("exploration_actions_tried") or plan.exploration_actions_tried or [])
    plan.exploration_milestones = list(plan_dict.get("exploration_milestones") or plan.exploration_milestones or [])
    plan.exploration_checklist = dict(checklist)
    plan.source_exploration_stage = dict(plan_dict.get("source_exploration_stage") or plan.source_exploration_stage or {})
    plan.source_digests = dict(plan_dict.get("source_digests") or plan.source_digests or {})
    ctx = build_exploration_context(state, plan, query)
    log_exploration_plan(plan, decision, ctx)
    if state is not None:
        ap = plan.to_dict()
        ap["next_action"] = na
        ap["exploration_thinking"] = decision.thinking[:2000]
        ap["exploration_decision_source"] = decision.source
        ap["exploration_actions_tried"] = list(plan.exploration_actions_tried or [])
        ap["exploration_checklist"] = checklist
        ap["source_digests"] = dict(plan.source_digests or {})
        state.agent_plan = ap
    return decision


def get_exploration_tool_call(
    plan: AgentPlan,
    state: SessionState | None,
    query: str,
) -> tuple[str, dict[str, Any]] | None:
    decision = refresh_read_only_exploration_plan(state, plan, query)
    return decision.to_tool_call()


def format_exploration_plan_block(plan: AgentPlan, budget_tokens: int = 800) -> str:
    from context_budget import truncate_to_token_budget

    plan_dict = plan.to_dict() if hasattr(plan, "to_dict") else dict(plan or {})
    thinking = str(plan_dict.get("exploration_thinking") or "").strip()
    na = plan.next_action or {}
    src = str(plan_dict.get("exploration_decision_source") or "")
    lines = ["[Exploration — LLM planner + evidence depth gate]"]
    if src:
        lines.append(f"decision_source: {src}")
    if thinking:
        lines.append(f"thinking: {thinking[:700]}")
    if na.get("tool"):
        lines.append(
            f"next: {na.get('tool')} source_id={na.get('source_id') or na.get('target')} "
            f"pattern={na.get('pattern', '')} glob={na.get('glob_pattern', '')} ({na.get('reason', '')})"
        )
    stages = plan_dict.get("source_exploration_stage") or {}
    if stages:
        lines.append("exploration_stage:")
        for sid, st in list(stages.items())[:12]:
            lines.append(f"- {sid}: {st}")
    checklist = plan_dict.get("exploration_checklist") or {}
    if checklist:
        pending = [k for k, v in checklist.items() if v == "pending"]
        done = [k for k, v in checklist.items() if v == "done"]
        lines.append(f"exploration_checklist: done={len(done)} pending={len(pending)}")
        if pending[:8]:
            lines.append("pending: " + ", ".join(pending[:8]))
    lines.append("[/Exploration]")
    return truncate_to_token_budget("\n".join(lines), budget_tokens)
