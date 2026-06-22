"""Structured agent planner: rule-based default, optional LLM JSON planner."""

from __future__ import annotations

import copy
import hashlib
import json
import logging
import os
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .evidence_extractors import (
  collect_evidence_from_tool_result,
  evidence_types_satisfied,
  exploration_evidence_done,
  is_exploration_intent,
  looks_like_project_inspection,
)
from adapters.memory import SessionState

LOG = logging.getLogger("router.planner")

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PLANNER_MODE = os.getenv("PLANNER_MODE", "rule")  # rule | llm | hybrid
PLANNER_MIN_CONFIDENCE = float(os.getenv("PLANNER_MIN_CONFIDENCE", "0.6"))
LONG_URL = os.getenv("LONG_URL", "http://llama-long:8082").rstrip("/")

DEFAULT_EVIDENCE: dict[str, list[str]] = {
  "project_inspection": ["target_coverage"],
  "benchmark_analysis": ["runtime_score_seen", "agent_benchmark_seen"],
  "log_analysis": ["flow_phase_seen"],
  "runtime_diagnosis": [
    "phase_distribution_seen",
    "loop_pattern_seen",
    "bottleneck_seen",
  ],
  "flow_analysis": ["flow_phase_seen"],
  "code_analysis": ["code_location_seen"],
}

PATH_RE = re.compile(
  r"(/[\w./\-~]+(?:\.(?:json|py|md|yml|yaml|html|log|sh|env)|\.flow\.json))"
)


@dataclass
class AgentPlan:
  task_intent: str = "general"
  confidence: float = 0.7
  goal: str = ""
  known_files: list[str] = field(default_factory=list)
  next_action: dict[str, Any] = field(default_factory=dict)
  avoid_actions: list[str] = field(default_factory=list)
  evidence_needed: list[str] = field(default_factory=list)
  evidence_collected: list[str] = field(default_factory=list)
  done_when: list[str] = field(default_factory=list)
  banned_tools: list[str] = field(default_factory=list)
  step_count: int = 0
  failed_actions: dict[str, int] = field(default_factory=dict)
  stale: bool = False
  keyword_hints: list[str] = field(default_factory=list)
  previous_plan_failures: list[str] = field(default_factory=list)
  final_ready: bool = False
  final_ready_step: int = 0
  context_need: dict[str, Any] = field(default_factory=dict)
  allowed_tools: list[str] = field(default_factory=list)
  disallowed_tools: list[str] = field(default_factory=list)
  preferred_sources: list[str] = field(default_factory=list)
  max_tool_rounds: int = 0
  coverage_hits: list[str] = field(default_factory=list)
  source_hits: list[str] = field(default_factory=list)
  source_registry: dict[str, Any] = field(default_factory=dict)
  source_candidates: list[str] = field(default_factory=list)
  required_source_ids: list[str] = field(default_factory=list)
  summary_source_ids: list[str] = field(default_factory=list)
  source_grep_depth: dict[str, int] = field(default_factory=dict)
  source_inventory_failures: dict[str, int] = field(default_factory=dict)
  source_exploration_stage: dict[str, str] = field(default_factory=dict)
  exploration_milestones: list[str] = field(default_factory=list)
  exploration_actions_tried: list[str] = field(default_factory=list)
  exploration_checklist: dict[str, str] = field(default_factory=dict)
  source_digests: dict[str, str] = field(default_factory=dict)
  router_intent: str = ""

  def to_dict(self) -> dict[str, Any]:
    return asdict(self)

  @classmethod
  def from_dict(cls, data: dict[str, Any] | None) -> AgentPlan:
    if not data:
      return cls()
    fields = {k: data[k] for k in cls.__dataclass_fields__ if k in data}
    return cls(**fields)


def detect_keyword_hints(query: str) -> list[str]:
  q = (query or "").lower()
  hints: list[str] = []
  if any(k in q for k in ("html validation", "validator", "w3c", "markup error", "html 검증", "마크업 검증")):
    hints.append("html_validation")
  if any(k in q for k in ("벤치", "benchmark", "runtime score", "벤치마크")):
    hints.append("benchmark_analysis")
  if "docker-compose" in q and any(k in q for k in ("포트", "port")):
    hints.append("compose_port")
  if any(k in q for k in ("flow.json", "flow trace", "flow 로그", "cursor 로그", "llm에 입력")):
    hints.append("flow_analysis")
  if any(k in q for k in ("로그", "log", "trace", "캡처", "flow")):
    if "log_analysis" not in hints:
      hints.append("log_analysis")
  if any(k in q for k in ("느린", "루프", "병목", "튜닝", "ping-pong", "pingpong", "속도", "48턴", "runtime error")):
    hints.append("runtime_diagnosis")
  if any(k in q for k in ("개선", "벤치", "benchmark", "runtime score")) and "benchmark_analysis" not in hints:
    if any(k in q for k in ("벤치", "benchmark", "runtime score", "score.json")):
      hints.append("benchmark_analysis")
  if any(k in q for k in ("plan_state", "agent_exec", "router")):
    hints.append("code_analysis")
  if looks_like_project_inspection(query):
    hints.append("project_inspection")
  return hints


def infer_workspace_scope(query: str, known_files: list[str], workspace: str) -> str:
  from .project_root import effective_workspace

  return effective_workspace(workspace, known_files)


EXCLUDE_DIR_NAMES = frozenset(
  {"node_modules", ".git", ".codex", "__pycache__", ".venv", "dist", "build", ".npm"}
)


def extract_paths_from_text(text: str) -> list[str]:
  out: list[str] = []
  for m in PATH_RE.finditer(text or ""):
    p = m.group(1)
    if any(part in EXCLUDE_DIR_NAMES for part in Path(p).parts):
      continue
    out.append(p)
  return out


def _default_benchmark_files() -> list[str]:
  tmp = PROJECT_ROOT / "tmp"
  names = [
    "benchmark-cursor-agent.json",
    "benchmark-runtime-score.json",
    "benchmark-runtime.json",
  ]
  return [str(tmp / n) for n in names if (tmp / n).exists()]


def _pick_read_target(known_files: list[str]) -> str | None:
  for p in known_files:
    if p.startswith("/") and Path(p).exists():
      return p
  for p in known_files:
    if p.startswith("/"):
      return p
  return None


def apply_policy_constraints(
  plan: AgentPlan,
  query: str,
  *,
  router_intent: str = "",
  workspace: str = "",
) -> AgentPlan:
  """Attach action-space constraints (not forced Shell next_action)."""
  from .project_root import resolve_project_root
  from .source_registry import (
    build_source_registry,
    discover_read_only_relpaths,
    resolve_root_mapping,
    summary_source_ids_for_registry,
  )
  from .target_coverage import (
    looks_like_read_only_query,
    read_only_tool_policy,
  )

  plan.router_intent = router_intent or plan.router_intent
  known = list(plan.known_files or [])
  from .project_root import effective_workspace, is_container_router_path

  ws = workspace
  if is_container_router_path(ws):
    ws = ""
  root = effective_workspace(ws or workspace, known)
  mapping = resolve_root_mapping(root, known_paths=known)
  read_only = router_intent == "read_only_analysis" or looks_like_read_only_query(query)

  if read_only:
    plan.router_intent = "read_only_analysis"

  if read_only or plan.task_intent == "project_inspection":
    allowed, disallowed, max_rounds = read_only_tool_policy(query, use_source_tools=True)
    plan.allowed_tools = allowed
    plan.disallowed_tools = disallowed
    plan.max_tool_rounds = max_rounds
    relpaths, summary_rels = discover_read_only_relpaths(query, mapping)
    if read_only and any(str(r).replace("\\", "/").startswith("router/") for r in relpaths):
        if "router" not in [str(r).replace("\\", "/").strip("/") for r in relpaths]:
            relpaths.insert(0, "router")
    plan.preferred_sources = relpaths
    registry = build_source_registry(mapping, plan.preferred_sources)
    plan.source_registry = registry.to_dict()
    plan.source_candidates = registry.source_ids()
    plan.summary_source_ids = summary_source_ids_for_registry(registry, summary_rels)
    query_tokens = {
      t.lower()
      for t in re.findall(r"[a-zA-Z_][a-zA-Z0-9_]*", query or "")
      if len(t) >= 3
    }
    query_dir_ids = [
      s.id
      for s in registry.sources
      if s.exists
      and s.kind == "dir"
      and Path(s.relpath).name.lower() in query_tokens
    ]
    plan.required_source_ids = list(
      dict.fromkeys(plan.summary_source_ids + query_dir_ids)
    )
    if read_only:
      plan.evidence_needed = ["target_coverage"]
      plan.done_when = [
        "summary docs and query-mentioned dirs read via source_id",
      ]
      if "Shell" not in plan.banned_tools and "Shell" in plan.disallowed_tools:
        plan.banned_tools.append("Shell")
    plan.avoid_actions = list(dict.fromkeys(plan.avoid_actions + [
      "inventing absolute paths",
      "Read/Grep/Glob with raw path when source_id available",
      "Shell ls/find on home directory",
      "Write/Edit/StrReplace on read-only tasks",
    ]))
    plan.next_action = {
      "tool": "",
      "target": "",
      "reason": "Pick source_id from available_sources — runtime resolves path.",
    }
    plan.known_files = [
      s.host_path for s in registry.available() if s.kind == "file"
    ][:12]

  ctx = plan.context_need if isinstance(plan.context_need, dict) else {}
  if plan.preferred_sources:
    ctx = dict(ctx)
    ctx["coverage_targets"] = list(
      dict.fromkeys((ctx.get("coverage_targets") or []) + plan.preferred_sources)
    )[:10]
    if plan.source_registry:
      ctx["available_sources"] = plan.source_candidates
    if plan.required_source_ids:
      ctx = dict(ctx)
      ctx["coverage_targets"] = list(plan.required_source_ids)[:10]
    plan.context_need = ctx
  return plan


def normalize_plan(plan: AgentPlan, query: str, *, router_intent: str = "", workspace: str = "") -> AgentPlan:
  """Attach default evidence for classified intents; never leave general without evidence."""
  q = (query or "").lower()
  if looks_like_project_inspection(query):
    if plan.task_intent in ("general", "exploration", ""):
      plan.task_intent = "project_inspection"
  if any(k in q for k in ("벤치", "benchmark", "runtime score", "score.json")):
    if plan.task_intent in ("general", "exploration", ""):
      plan.task_intent = "benchmark_analysis"
  if any(k in q for k in ("로그", "flow", "trace", "llm에 입력", "cursor 로그")):
    if plan.task_intent in ("general", "exploration", ""):
      plan.task_intent = "log_analysis"
  if any(k in q for k in ("느린", "루프", "병목", "튜닝", "ping", "속도")):
    if plan.task_intent in ("general", "exploration", ""):
      plan.task_intent = "runtime_diagnosis"

  defaults = DEFAULT_EVIDENCE.get(plan.task_intent)
  if defaults and not plan.evidence_needed:
    plan.evidence_needed = list(defaults)

  if plan.task_intent == "project_inspection":
    if not plan.done_when:
      plan.done_when = [
        "preferred docs or module dirs read",
        "coverage targets satisfied",
      ]
  elif plan.task_intent == "benchmark_analysis" and not plan.next_action.get("tool"):
    bench_md = PROJECT_ROOT / "docs" / "BENCHMARK.md"
    if bench_md.exists():
      plan.next_action = {
        "tool": "Read",
        "target": str(bench_md),
        "reason": "Read curated BENCHMARK.md summary (prefer over raw JSON).",
      }
    else:
      plan.next_action = {
        "tool": "Grep",
        "target": str(PROJECT_ROOT / "tmp"),
        "query": "pass_rate|runtime_score|agent_success",
        "reason": "Grep benchmark fields without full JSON read.",
      }
  elif plan.task_intent in ("log_analysis", "flow_analysis") and not plan.next_action.get("tool"):
    flows = sorted((PROJECT_ROOT / "tmp" / "cursor-captures").glob("*.flow.json"))
    if flows:
      plan.next_action = {
        "tool": "Grep",
        "target": str(flows[-1]),
        "query": "phase|intent|pack_tokens|saved_pct",
        "reason": "Grep flow trace fields without full JSON read.",
      }
  elif plan.task_intent == "runtime_diagnosis" and not plan.next_action.get("tool"):
    plan.next_action = {
      "tool": "Read",
      "target": str(PROJECT_ROOT / "router" / "plan_state.py"),
      "reason": "Inspect phase resolution / loop guard logic.",
    }

  if plan.task_intent == "general" and not plan.evidence_needed:
    plan.evidence_needed = ["artifact_seen"]
    plan.done_when = ["at least one relevant file or log read"]

  from context_need import build_context_need, merge_context_need

  rule = build_context_need(plan, query, router_intent)
  if plan.context_need:
    plan.context_need = merge_context_need(plan.context_need, rule).to_dict()
  else:
    plan.context_need = rule.to_dict()
  plan = apply_policy_constraints(plan, query, router_intent=router_intent, workspace=workspace)
  return plan


def build_rule_plan(
  query: str,
  state: SessionState,
  *,
  hints: list[str] | None = None,
  replan_failures: list[str] | None = None,
  router_intent: str = "",
) -> AgentPlan:
  hints = hints or detect_keyword_hints(query)
  known = list(dict.fromkeys(extract_paths_from_text(query) + list(state.files_read[-12:])))
  if "benchmark_analysis" in hints:
    known = list(dict.fromkeys(known + _default_benchmark_files()))

  intent = hints[0] if hints else "general"
  if "compose_port" in hints:
    intent = "compose_port"
  elif "benchmark_analysis" in hints:
    intent = "benchmark_analysis"
  elif "html_validation" in hints:
    intent = "html_validation"
  elif "flow_analysis" in hints or intent == "log_analysis":
    intent = "log_analysis" if "log_analysis" in hints else intent
  elif "runtime_diagnosis" in hints:
    intent = "runtime_diagnosis"
  elif "project_inspection" in hints or looks_like_project_inspection(query):
    intent = "project_inspection"

  avoid = [
    "Glob unless known file Read fails",
    "HTML validation Shell on non-HTML files",
    "repeating failed Shell command",
  ]
  banned: list[str] = list(state.agent_plan.get("banned_tools", []) if state.agent_plan else [])
  failed = dict(state.failed_actions or {})

  if known and _pick_read_target(known):
    avoid.insert(0, "Glob when known_files has readable paths")
    if state.glob_unproductive >= 2:
      banned.append("Glob")

  for sig, count in failed.items():
    if count >= 2:
      tool = sig.split(":", 1)[0]
      if tool and tool not in banned:
        banned.append(tool)

  evidence_needed: list[str] = list(DEFAULT_EVIDENCE.get(intent, []))
  done_when: list[str] = []
  if intent == "benchmark_analysis":
    if not evidence_needed:
      evidence_needed = [
        "runtime_score_seen",
        "agent_benchmark_seen",
      ]
    done_when = [
      "benchmark JSON read",
      "loop cause identified",
    ]
  elif intent == "log_analysis" or intent == "flow_analysis":
    if not evidence_needed:
      evidence_needed = ["flow_phase_seen"]
    done_when = ["flow trace analyzed"]
  elif intent == "runtime_diagnosis":
    done_when = ["bottleneck identified", "loop pattern understood"]
  elif intent == "compose_port":
    evidence_needed = ["router port mapping"]
    done_when = ["port answer available"]
  elif intent == "html_validation":
    evidence_needed = ["html structure validated"]
    done_when = ["validation_ok true"]
  elif intent == "project_inspection":
    evidence_needed = ["target_coverage"]
    done_when = [
      "preferred docs or module dirs read",
      "coverage targets satisfied",
    ]

  target = _pick_read_target(known)
  next_action: dict[str, Any] = {}
  if target:
    next_action = {
      "tool": "Read",
      "target": target,
      "reason": "Known file path — prefer Read over Glob.",
    }
  elif intent == "benchmark_analysis":
    bench_md = PROJECT_ROOT / "docs" / "BENCHMARK.md"
    if bench_md.exists() and str(bench_md) not in known:
      known.insert(0, str(bench_md))
    next_action = {
      "tool": "Read",
      "target": str(bench_md) if bench_md.exists() else str(PROJECT_ROOT / "docs" / "ARCHITECTURE.md"),
      "reason": "Benchmark analysis — read BENCHMARK.md summary first.",
    }
  elif "flow_analysis" in hints or intent == "log_analysis":
    flows = sorted((PROJECT_ROOT / "tmp" / "cursor-captures").glob("*.flow.json"))
    if flows:
      next_action = {
        "tool": "Grep",
        "target": str(flows[-1]),
        "query": "phase|pack_tokens|saved_pct|intent",
        "reason": "Grep latest flow trace fields.",
      }
  elif intent == "runtime_diagnosis":
    next_action = {
      "tool": "Read",
      "target": str(PROJECT_ROOT / "router" / "plan_state.py"),
      "reason": "Inspect phase / loop guard code.",
    }

  collected = list(state.agent_plan.get("evidence_collected", []) if state.agent_plan else [])

  plan = AgentPlan(
    task_intent=intent,
    confidence=0.75 if hints else 0.55,
    goal=query[:400],
    known_files=known[:12],
    next_action=next_action,
    avoid_actions=avoid,
    evidence_needed=evidence_needed,
    evidence_collected=collected,
    done_when=done_when,
    banned_tools=list(dict.fromkeys(banned)),
    step_count=int(state.agent_plan.get("step_count", 0) if state.agent_plan else 0),
    failed_actions=failed,
    stale=False,
    keyword_hints=hints,
    previous_plan_failures=replan_failures or list(state.previous_plan_failures or []),
  )
  return normalize_plan(
    plan, query, router_intent=router_intent, workspace=state.workspace_path,
  )


def filter_tools_by_plan(out: dict[str, Any], plan: AgentPlan | None) -> None:
  """Restrict Cursor tool list to plan allowed/disallowed sets."""
  if not plan or not out.get("tools"):
    return
  allowed = list(plan.allowed_tools or [])
  disallowed = set(plan.disallowed_tools or [])
  if not allowed and not disallowed:
    return
  filtered: list[dict[str, Any]] = []
  for t in out.get("tools") or []:
    if not isinstance(t, dict):
      continue
    fn = t.get("function") if isinstance(t.get("function"), dict) else t
    name = str((fn or {}).get("name") or t.get("name") or "")
    if disallowed and name in disallowed:
      continue
    if allowed and name not in allowed:
      continue
    filtered.append(t)
  out["tools"] = filtered


def action_signature(tool: str, args: dict[str, Any]) -> str:
  raw = tool + ":" + json.dumps(args, sort_keys=True, ensure_ascii=False)
  h = hashlib.sha256(raw.encode()).hexdigest()[:16]
  target = str(args.get("path") or args.get("command") or args.get("glob_pattern") or "")[:120]
  return f"{tool}:{h}:{target}"


FILE_TASK_INTENTS = frozenset({
  "benchmark_analysis",
  "flow_analysis",
  "code_analysis",
  "html_validation",
  "compose_port",
  "file_read",
  "debug_analysis",
  "project_inspection",
  "repo_summary",
  "code_read",
  "exploration",
})

FILE_QUERY_KW = (
  "읽고", "수정", "확인", "벤치", "로그", "read", "file", ".json", ".py", ".md", "분석",
)


def should_strip_tools(
  plan: AgentPlan | None,
  intent_name: str,
  query: str,
  body: dict[str, Any],
  phase: str,
) -> bool:
  """Return True when tools should be removed from proxy body."""
  if plan and getattr(plan, "final_ready", False) and plan.next_action.get("tool") == "answer":
    if phase in ("final_answer", "partial_final_answer", "recovery_final"):
      return True
    if plan.source_registry:
      from .source_registry import source_coverage_passes

      if source_coverage_passes(plan.to_dict(), plan.source_registry):
        return True
      return False
    return True
  if plan and is_exploration_intent(plan.task_intent) and not can_final_answer(plan):
    return False
  if phase in ("final_answer", "partial_final_answer", "recovery_final"):
    if phase == "partial_final_answer":
      return True
    if plan and is_exploration_intent(plan.task_intent) and not can_final_answer(plan):
      return False
    return True
  if intent_name in ("casual", "explain"):
    return True
  if plan and plan.source_registry and phase == "tool_planning":
    from .source_registry import source_coverage_passes

    if not source_coverage_passes(plan.to_dict(), plan.source_registry):
      return False
  if plan and evidence_types_satisfied(
    plan.evidence_needed,
    plan.evidence_collected,
    task_intent=plan.task_intent,
  ):
    if plan.next_action.get("tool") == "answer":
      if plan.source_registry:
        from .source_registry import source_coverage_passes

        return source_coverage_passes(plan.to_dict(), plan.source_registry)
      return True
  return False


def should_keep_tools(
  plan: AgentPlan | None,
  intent_name: str,
  query: str,
  body: dict[str, Any],
  phase: str,
) -> bool:
  """Keep/inject tools for file/benchmark agent tasks."""
  if should_strip_tools(plan, intent_name, query, body, phase):
    return False
  cursor_tools = body.get("tools")
  has_cursor_tools = isinstance(cursor_tools, list) and len(cursor_tools) > 0
  q = (query or "").lower()
  file_query = any(kw in q for kw in FILE_QUERY_KW)
  task_intent = (plan.task_intent if plan else "") or ""
  known_files = bool(plan and plan.known_files)
  if intent_name in ("read_only_analysis", "project_inspection") and phase == "tool_planning":
    if plan and plan.source_registry:
      from .source_registry import source_coverage_passes

      return not source_coverage_passes(plan.to_dict(), plan.source_registry)
    return True
  if task_intent in FILE_TASK_INTENTS or is_exploration_intent(task_intent):
    return True
  if intent_name in ("code_edit", "benchmark", "log_analysis", "debug", "agent", "shell_task"):
    if known_files or file_query:
      return True
  if has_cursor_tools and (known_files or task_intent != "general" or file_query):
    return True
  return False


def ensure_proxy_tools(
  out: dict[str, Any],
  body: dict[str, Any],
  plan: AgentPlan | None,
) -> None:
  """Preserve Cursor tools or inject source_id-based tools for read-only tasks."""
  from .source_tools import inject_source_tools

  if plan and inject_source_tools(out, plan):
    return
  if out.get("tools"):
    return
  src = body.get("tools")
  if isinstance(src, list) and src:
    out["tools"] = copy.deepcopy(src)
    return
  if not plan or not should_keep_tools(plan, "agent", "", body, "tool_planning"):
    return
  out["tools"] = [
    {
      "type": "function",
      "function": {
        "name": "Read",
        "parameters": {
          "type": "object",
          "properties": {"path": {"type": "string"}},
          "required": ["path"],
        },
      },
    },
    {
      "type": "function",
      "function": {
        "name": "Glob",
        "parameters": {
          "type": "object",
          "properties": {"glob_pattern": {"type": "string"}},
          "required": ["glob_pattern"],
        },
      },
    },
    {
      "type": "function",
      "function": {
        "name": "Grep",
        "parameters": {
          "type": "object",
          "properties": {
            "pattern": {"type": "string"},
            "path": {"type": "string"},
          },
          "required": ["pattern"],
        },
      },
    },
  ]


def should_replan(state: SessionState, plan: AgentPlan, query: str) -> bool:
  if plan.stale:
    return True
  if plan.router_intent == "read_only_analysis" or plan.evidence_needed == ["target_coverage"]:
    try:
      from .source_registry import source_coverage_passes

      if plan.source_registry and source_coverage_passes(plan.to_dict(), plan.source_registry):
        return False
      hits = len(plan.source_hits or [])
      candidates = len(plan.source_candidates or [])
      if candidates and hits > 0:
        return False
    except ImportError:
      pass
  if plan.confidence < PLANNER_MIN_CONFIDENCE:
    return True
  if plan.router_intent == "read_only_analysis" and plan.source_registry:
    hits = len(plan.source_hits or [])
    candidates = len(plan.source_candidates or [])
    if candidates and hits < candidates and hits > 0:
      return False
  if state.steps_since_evidence >= 3:
    return True
  if state.glob_unproductive >= 2 and plan.next_action.get("tool") == "Glob":
    return True
  if plan.previous_plan_failures and plan.step_count == 0:
    return False
  prev_query = (state.agent_plan or {}).get("goal", "")
  if prev_query and query and prev_query[:80] != query[:80] and state.steps_since_evidence == 0:
    return True
  return False


def mark_final_ready(plan: AgentPlan, *, query: str = "", project_root: str = "") -> None:
  from .source_registry import source_coverage_passes
  from .target_coverage import target_coverage_passes

  targets = list(plan.preferred_sources or [])
  ctx = plan.context_need if isinstance(plan.context_need, dict) else {}
  if ctx.get("coverage_targets"):
    targets = list(dict.fromkeys(targets + list(ctx.get("coverage_targets") or [])))
  plan_dict = plan.to_dict()
  if plan.router_intent == "read_only_analysis" or plan.evidence_needed == ["target_coverage"]:
    if plan.source_registry:
      if not source_coverage_passes(plan_dict, plan.source_registry):
        return
      try:
        from .read_only_explorer import source_exploration_depth_passes

        if not source_exploration_depth_passes(plan_dict, plan.source_registry):
          return
      except ImportError:
        pass
    elif targets and not target_coverage_passes(plan_dict, targets):
      return
    plan.coverage_hits = list(plan_dict.get("coverage_hits") or [])
    plan.source_hits = list(plan_dict.get("source_hits") or [])
  plan.final_ready = True
  plan.final_ready_step = plan.step_count
  plan.next_action = {"tool": "answer", "target": "", "reason": "Target coverage satisfied — prose final."}


def revoke_final_ready(
  plan: AgentPlan,
  state: SessionState,
  reason: str = "post_final_tool_call",
  *,
  emit_run_events: bool = True,
  run_id: str = "",
) -> None:
  if not plan.final_ready:
    return
  plan.final_ready = False
  plan.final_ready_step = 0
  plan.next_action = {"tool": "", "target": "", "reason": f"Re-explore after {reason} — pick source_id."}
  state.phase_hint = "explore"
  if emit_run_events:
    try:
      from adapters.observe import current_run_id, emit_task

      rid = run_id or current_run_id()
      if rid:
        emit_task(rid, "final.ready_cancelled", reason[:240])
    except ImportError:
      pass
  LOG.info("final_ready revoked reason=%s step=%d", reason, plan.step_count)


def update_plan_after_tool(
  plan: AgentPlan,
  state: SessionState,
  *,
  tool_name: str,
  args: dict[str, Any],
  result_text: str,
  success: bool,
  emit_run_events: bool = True,
  run_id: str = "",
) -> AgentPlan:
  if plan.final_ready:
    revoke_final_ready(plan, state, "post_final_tool_call", emit_run_events=emit_run_events, run_id=run_id)

  sig = action_signature(tool_name, args)
  if not success:
    plan.failed_actions[sig] = plan.failed_actions.get(sig, 0) + 1
    state.failed_actions[sig] = plan.failed_actions[sig]
    if plan.failed_actions[sig] >= 2:
      tool_ban = tool_name
      if tool_ban not in plan.banned_tools:
        plan.banned_tools.append(tool_ban)
      state.previous_plan_failures.append(f"{tool_name} failed twice: {sig[:40]}")
  else:
    plan.failed_actions.pop(sig, None)

  path = str(args.get("path") or args.get("target_directory") or "")
  query_arg = str(args.get("pattern") or args.get("query") or "")
  if not query_arg and plan.next_action:
    query_arg = str(plan.next_action.get("pattern") or plan.next_action.get("glob_pattern") or "")
  source_id = str(args.get("_source_id") or args.get("source_id") or "")
  target = source_id or path or query_arg or str(args.get("command") or "")[:120]

  from .project_root import resolve_project_root
  from .source_tools import extract_source_id_from_args
  from .target_coverage import is_home_shell_command, register_hits_from_tool

  source_id = extract_source_id_from_args(args) or source_id
  root = resolve_project_root(state.workspace_path)
  registry = plan.source_registry if plan.source_registry else None

  if plan.router_intent == "read_only_analysis" and source_id:
    try:
      from .read_only_explorer import record_exploration_action

      plan_dict_ro = plan.to_dict()
      record_exploration_action(
        plan_dict_ro,
        tool_name,
        source_id,
        pattern=query_arg,
        glob_pattern=str(args.get("glob_pattern") or ""),
      )
      plan.exploration_actions_tried = list(plan_dict_ro.get("exploration_actions_tried") or [])
      try:
        from explorer_trace import trace_explorer_action
        from .read_only_explorer import exploration_action_sig

        trace_explorer_action(
            "action_done" if success else "action_failed",
            tool=tool_name,
            source_id=source_id,
            pattern=query_arg,
            glob_pattern=str(args.get("glob_pattern") or ""),
            success=success,
            result_chars=len(result_text or ""),
            result_preview=(result_text or "")[:2400],
            action_sig=exploration_action_sig(
              tool_name,
              source_id,
              pattern=query_arg,
              glob_pattern=str(args.get("glob_pattern") or ""),
            ),
        )
      except ImportError:
        pass
    except ImportError:
      pass

  if tool_name == "Shell" and is_home_shell_command(str(args.get("command") or "")):
    state.steps_since_evidence += 1
  elif success:
    targets = list(plan.preferred_sources or [])
    ctx = plan.context_need if isinstance(plan.context_need, dict) else {}
    if ctx.get("coverage_targets"):
      targets = list(dict.fromkeys(targets + list(ctx.get("coverage_targets") or [])))
    if targets or source_id:
      plan_dict = plan.to_dict()
      added = register_hits_from_tool(
        plan_dict,
        path=path,
        content=result_text[:8000],
        tool_name=tool_name,
        project_root=root,
        targets=targets,
        success=success,
        source_id=source_id,
        pattern=query_arg,
        registry=registry,
      )
      plan.coverage_hits = list(plan_dict.get("coverage_hits") or [])
      plan.source_hits = list(plan_dict.get("source_hits") or [])
      plan.source_grep_depth = dict(plan_dict.get("source_grep_depth") or {})
      plan.source_inventory_failures = dict(plan_dict.get("source_inventory_failures") or {})
      plan.source_exploration_stage = dict(plan_dict.get("source_exploration_stage") or {})
      plan.exploration_milestones = list(plan_dict.get("exploration_milestones") or [])
      try:
        from .read_only_explorer import build_exploration_checklist

        if plan.router_intent == "read_only_analysis" and registry:
          plan.exploration_checklist = build_exploration_checklist(plan_dict, registry)
      except ImportError:
        pass
      if success:
        try:
          from .read_only_explorer import _source_digests, _sync_source_digests_to_plan

          digests = _source_digests(state, plan)
          _sync_source_digests_to_plan(plan_dict, digests)
          plan.source_digests = dict(plan_dict.get("source_digests") or {})
        except ImportError:
          pass
      try:
        from .read_only_explorer import source_exploration_depth_passes

        if plan.router_intent == "read_only_analysis" and registry and source_exploration_depth_passes(plan_dict, registry):
          if "target_coverage" not in plan.evidence_collected:
            plan.evidence_collected = list(dict.fromkeys(plan.evidence_collected + ["target_coverage"]))
      except ImportError:
        pass
      if added:
        hit_label = source_id or added[0]
        plan.evidence_collected = list(
          dict.fromkeys(plan.evidence_collected + [f"source_hit:{hit_label}"])
        )
        state.steps_since_evidence = 0
  else:
    state.steps_since_evidence += 1

  new_ev = collect_evidence_from_tool_result(result_text, path=path, tool_name=tool_name)
  if new_ev:
    before = set(plan.evidence_collected)
    added = list(set(new_ev) - before)
    plan.evidence_collected = list(dict.fromkeys(plan.evidence_collected + new_ev))
    if added:
      state.steps_since_evidence = 0
      if emit_run_events:
        try:
          from adapters.observe import current_run_id, emit_evidence_collected

          rid = run_id or current_run_id()
          if rid:
            emit_evidence_collected(
              rid,
              added,
              source=tool_name,
              target=target,
            )
        except ImportError:
          pass
    else:
      state.steps_since_evidence += 1
  else:
    state.steps_since_evidence += 1

  try:
    from .evidence_store import append_evidence_item, build_evidence_item

    append_evidence_item(
      state,
      build_evidence_item(
        tool=tool_name,
        path=path,
        query=query_arg,
        result_text=result_text[:8000],
        tags=new_ev,
        raw_ref=target,
      ),
    )
    state.tools_since_judge = int(getattr(state, "tools_since_judge", 0) or 0) + 1
    state.explore_round = int(getattr(state, "explore_round", 0) or 0) + 1
  except ImportError:
    pass

  if tool_name == "Glob" and (not result_text.strip() or "0 files" in result_text.lower()):
    state.glob_unproductive += 1
  elif tool_name == "Read" and success:
    state.glob_unproductive = 0

  plan.step_count += 1
  state.steps_since_evidence = state.steps_since_evidence

  try:
    from .loop_guard import record_action_sig

    record_action_sig(state, sig)
  except ImportError:
    pass

  if evidence_types_satisfied(
    plan.evidence_needed,
    plan.evidence_collected,
    task_intent=plan.task_intent,
  ):
    from .target_coverage import target_coverage_passes
    from .source_registry import source_coverage_passes

    targets = list(plan.preferred_sources or [])
    ctx = plan.context_need if isinstance(plan.context_need, dict) else {}
    if ctx.get("coverage_targets"):
      targets = list(dict.fromkeys(targets + list(ctx.get("coverage_targets") or [])))
    coverage_ok = False
    if plan.router_intent == "read_only_analysis" or plan.evidence_needed == ["target_coverage"]:
      if plan.source_registry:
        coverage_ok = source_coverage_passes(plan.to_dict(), plan.source_registry)
        try:
          from .read_only_explorer import source_exploration_depth_passes

          coverage_ok = coverage_ok and source_exploration_depth_passes(plan.to_dict(), plan.source_registry)
        except ImportError:
          pass
      elif targets:
        coverage_ok = target_coverage_passes(plan.to_dict(), targets)
    elif not targets or target_coverage_passes(plan.to_dict(), targets):
      coverage_ok = True
    if coverage_ok:
      mark_final_ready(plan, query=state.current_query or "", project_root=root)
    if emit_run_events:
      try:
        from adapters.observe import current_run_id, emit_task

        rid = run_id or current_run_id()
        if rid:
          emit_task(rid, "final.ready", "required evidence satisfied")
      except ImportError:
        pass
  elif plan.step_count >= 3 and state.steps_since_evidence >= 3:
    plan.stale = True

  return plan


def call_llm_planner(
  query: str,
  state: SessionState,
  hints: list[str],
  memory_summary: str,
) -> AgentPlan | None:
  if PLANNER_MODE not in ("llm", "hybrid"):
    return None
  try:
    import httpx
  except ImportError:
    return None

  schema_hint = (
    "Return ONLY JSON with keys: task_intent, confidence, goal, known_files, "
    "next_action{tool,target,reason}, avoid_actions, evidence_needed, "
    "evidence_collected, done_when, context_need{intent,required_sources,priority,"
    "must_include,coverage_targets}"
  )
  payload = {
    "model": "model.gguf",
    "stream": False,
    "temperature": 0,
    "max_tokens": 600,
    "messages": [
      {
        "role": "system",
        "content": (
          "You are a planning module. Output valid JSON only, no markdown. "
          + schema_hint
        ),
      },
      {
        "role": "user",
        "content": json.dumps(
          {
            "query": query,
            "keyword_hints": hints,
            "files_read": state.files_read[-8:],
            "memory_summary": memory_summary[:1500],
            "previous_failures": state.previous_plan_failures[-5:],
          },
          ensure_ascii=False,
        ),
      },
    ],
  }
  try:
    r = httpx.post(f"{LONG_URL}/v1/chat/completions", json=payload, timeout=60.0)
    r.raise_for_status()
    content = str(r.json()["choices"][0]["message"].get("content") or "")
    start = content.find("{")
    end = content.rfind("}")
    if start < 0 or end <= start:
      return None
    data = json.loads(content[start : end + 1])
    ctx_raw = data.pop("context_need", None)
    plan = AgentPlan.from_dict(data)
    if isinstance(ctx_raw, dict):
        plan.context_need = ctx_raw
    plan.keyword_hints = hints
    if plan.confidence < 0.3:
      return None
    return plan
  except Exception as exc:
    LOG.warning("llm_planner failed: %s", exc)
    return None


def ensure_agent_plan(
  state: SessionState,
  query: str,
  *,
  force_replan: bool = False,
  router_intent: str = "",
) -> AgentPlan:
  hints = detect_keyword_hints(query)
  existing = AgentPlan.from_dict(state.agent_plan) if state.agent_plan else AgentPlan()

  if existing.goal and not force_replan and not should_replan(state, existing, query):
    existing = normalize_plan(
      existing, query, router_intent=router_intent, workspace=state.workspace_path,
    )
    existing.keyword_hints = hints
    if evidence_types_satisfied(
      existing.evidence_needed,
      existing.evidence_collected,
      task_intent=existing.task_intent,
    ):
      try:
        from .source_registry import source_coverage_passes

        if existing.source_registry and source_coverage_passes(
          existing.to_dict(), existing.source_registry
        ):
          mark_final_ready(existing, query=query, project_root=state.workspace_path or "")
        else:
          from .target_coverage import target_coverage_passes

          targets = list(existing.preferred_sources or [])
          if not targets or target_coverage_passes(existing.to_dict(), targets):
            existing.next_action = {
              "tool": "answer",
              "target": "",
              "reason": "Target coverage satisfied — prose final.",
            }
      except ImportError:
        pass
    state.agent_plan = existing.to_dict()
    state.effective_workspace = infer_workspace_scope(query, existing.known_files, state.workspace_path)
    return existing

  failures = list(state.previous_plan_failures[-8:])
  rule_plan = build_rule_plan(
    query, state, hints=hints, replan_failures=failures, router_intent=router_intent,
  )

  memory_summary = f"files_read={state.files_read[-5:]} commands={state.commands_run[-3:]}"
  llm_plan = call_llm_planner(query, state, hints, memory_summary)
  if llm_plan and PLANNER_MODE == "llm":
    plan = llm_plan
  elif llm_plan and PLANNER_MODE == "hybrid" and llm_plan.confidence >= rule_plan.confidence:
    plan = llm_plan
  else:
    plan = rule_plan

  plan.evidence_collected = list(
    dict.fromkeys(plan.evidence_collected + existing.evidence_collected)
  )
  plan.coverage_hits = list(
    dict.fromkeys((plan.coverage_hits or []) + (existing.coverage_hits or []))
  )
  plan.source_hits = list(
    dict.fromkeys((plan.source_hits or []) + (existing.source_hits or []))
  )
  plan = normalize_plan(
    plan, query, router_intent=router_intent, workspace=state.workspace_path,
  )
  try:
    from .source_registry import source_coverage_passes

    if (
      plan.router_intent == "read_only_analysis"
      or plan.evidence_needed == ["target_coverage"]
    ) and plan.source_registry:
      if source_coverage_passes(plan.to_dict(), plan.source_registry):
        mark_final_ready(plan, query=query, project_root=state.workspace_path or "")
  except ImportError:
    pass
  state.agent_plan = plan.to_dict()
  state.effective_workspace = infer_workspace_scope(query, plan.known_files, state.workspace_path)
  try:
    from adapters.observe import current_run_id, emit_plan_created

    rid = current_run_id()
    if rid:
      emit_plan_created(rid, plan.to_dict())
  except ImportError:
    pass
  LOG.info(
    "planner intent=%s confidence=%.2f known_files=%d next=%s replan=%s",
    plan.task_intent,
    plan.confidence,
    len(plan.known_files),
    plan.next_action.get("tool", "?"),
    str(force_replan or should_replan(state, existing, query)).lower(),
  )
  return plan


def can_final_answer(plan: AgentPlan) -> bool:
  from .source_registry import source_coverage_passes
  from .target_coverage import target_coverage_passes

  targets = list(plan.preferred_sources or [])
  ctx = plan.context_need if isinstance(plan.context_need, dict) else {}
  if ctx.get("coverage_targets"):
    targets = list(dict.fromkeys(targets + list(ctx.get("coverage_targets") or [])))
  if plan.router_intent == "read_only_analysis" or plan.evidence_needed == ["target_coverage"]:
    if plan.source_registry:
      from .read_only_explorer import source_exploration_depth_passes

      if not source_exploration_depth_passes(plan.to_dict(), plan.source_registry):
        return False
      return source_coverage_passes(plan.to_dict(), plan.source_registry)
    if not targets:
      return bool(plan.source_hits or plan.coverage_hits)
    return target_coverage_passes(plan.to_dict(), targets)
  if plan.next_action.get("tool") == "answer":
    return evidence_types_satisfied(
      plan.evidence_needed,
      plan.evidence_collected,
      task_intent=plan.task_intent,
    )
  if not plan.evidence_needed:
    if is_exploration_intent(plan.task_intent):
      return exploration_evidence_done(plan.evidence_collected)
    return False
  return evidence_types_satisfied(
    plan.evidence_needed,
    plan.evidence_collected,
    task_intent=plan.task_intent,
  )


EXECUTOR_PLAN_INSTRUCTIONS = """\
Follow the saved agent plan unless new evidence contradicts it.
Do not choose tools listed in avoid_actions, banned_tools, or disallowed_tools.
When available_sources are listed, use ReadSource/GrepSource/GlobSource with source_id only.
For source_id with kind=dir, use GrepSource or GlobSource — never ReadSource on directories.
Do NOT invent absolute paths. Do NOT use Read/Grep/Glob with raw path when source registry is active.
If source_hits satisfy targets, answer in prose (no tools).
If the same action failed twice, do not repeat it.
Do not run Shell ls/find on home directory unless explicitly requested.
"""


def format_saved_agent_plan_block(
  plan: AgentPlan,
  budget_tokens: int,
  state: SessionState | None = None,
) -> str:
  from context_budget import truncate_to_token_budget
  from failed_action import format_failed_tools_for_planner
  from .source_registry import format_source_registry_block

  lines = [
    "[Saved Agent Plan]",
    f"intent: {plan.task_intent}",
    f"confidence: {plan.confidence:.2f}",
    f"goal: {plan.goal[:300]}",
  ]
  if plan.known_files:
    lines.append("known_files:")
    for p in plan.known_files[:10]:
      lines.append(f"- {p}")
  if plan.allowed_tools:
    lines.append("allowed_tools:")
    lines.append("- " + ", ".join(plan.allowed_tools))
  if plan.disallowed_tools:
    lines.append("disallowed_tools:")
    lines.append("- " + ", ".join(plan.disallowed_tools[:12]))
  if plan.preferred_sources:
    lines.append("preferred_sources:")
    for s in plan.preferred_sources[:10]:
      lines.append(f"- {s}")
  if plan.max_tool_rounds:
    lines.append(f"max_tool_rounds: {plan.max_tool_rounds}")
  if plan.source_candidates:
    lines.append("source_candidates:")
    for sid in plan.source_candidates[:12]:
      lines.append(f"- {sid}")
  if plan.source_hits:
    lines.append("source_hits:")
    for h in plan.source_hits[:10]:
      lines.append(f"- {h}")
  stages = plan.source_exploration_stage or {}
  if stages:
    lines.append("exploration_stage (none→inventory→content→anchor):")
    for sid, st in list(stages.items())[:12]:
      lines.append(f"- {sid}: {st}")
  if plan.router_intent == "read_only_analysis":
    try:
      from .read_only_explorer import format_exploration_plan_block

      block = format_exploration_plan_block(plan, max(256, budget_tokens // 4))
      if block.strip():
        lines.append(block)
    except ImportError:
      pass
  if plan.source_registry:
    from .source_registry import SourceRegistry, format_source_registry_block

    reg = SourceRegistry.from_dict(plan.source_registry)
    candidates = set(plan.source_candidates or [])
    hits = set(plan.source_hits or [])
    missing_ids = [
      s.id
      for s in reg.sources
      if s.exists and s.id in candidates and s.id not in hits
    ]
    if missing_ids:
      lines.append("missing_source_ids (read next — do NOT repeat source_hits):")
      for sid in missing_ids[:8]:
        lines.append(f"- {sid}")
      lines.append(
        "tool_planning: follow [Exploration Plan] — Glob inventory → Grep content/imports → Read docs; "
        "use ReadSource/GrepSource/GlobSource with source_id only."
      )
    lines.append(format_source_registry_block(plan.source_registry))
  if plan.coverage_hits:
    lines.append("coverage_hits:")
    for h in plan.coverage_hits[:10]:
      lines.append(f"- {h}")
  if plan.next_action and plan.next_action.get("tool"):
    na = plan.next_action
    lines.append("next_action:")
    lines.append(f"- {na.get('tool', '?')} {na.get('target', '')} ({na.get('reason', '')})")
  if plan.avoid_actions:
    lines.append("avoid:")
    for a in plan.avoid_actions[:8]:
      lines.append(f"- {a}")
  if plan.banned_tools:
    lines.append("banned_tools:")
    for t in plan.banned_tools:
      lines.append(f"- {t}")
  if plan.evidence_needed:
    lines.append("evidence_needed:")
    for e in plan.evidence_needed:
      lines.append(f"- {e}")
  if plan.evidence_collected:
    lines.append("evidence_collected:")
    for e in plan.evidence_collected:
      lines.append(f"- {e}")
  if plan.done_when:
    lines.append("done_when:")
    for d in plan.done_when:
      lines.append(f"- {d}")
  if plan.previous_plan_failures:
    lines.append("previous_plan_failures:")
    for f in plan.previous_plan_failures[-5:]:
      lines.append(f"- {f}")
  if state:
    failed_block = format_failed_tools_for_planner(state)
    if failed_block:
      lines.append(failed_block)
  lines.append(EXECUTOR_PLAN_INSTRUCTIONS.strip())
  lines.append("[/Saved Agent Plan]")
  return truncate_to_token_budget("\n".join(lines), budget_tokens)


def violates_avoid_actions(tool_name: str, args: dict[str, Any], avoid: list[str]) -> bool:
  blob = f"{tool_name} {json.dumps(args, ensure_ascii=False)}".lower()
  for rule in avoid:
    r = rule.lower()
    if "glob" in r and tool_name == "Glob":
      if "unless" in r and "fail" in r:
        return False
      return True
    if "html validation shell" in r and tool_name == "Shell":
      cmd = str(args.get("command") or "").lower()
      if "html.parser" in cmd or "beautifulsoup" in cmd:
        return True
    if "repeating failed shell" in r and tool_name == "Shell":
      continue
  if tool_name == "Glob" and any("glob when known_files" in a.lower() for a in avoid):
    return True
  return False


def validate_tool_call(
  tool_name: str,
  args: dict[str, Any],
  plan: AgentPlan,
) -> tuple[bool, str]:
  if plan.final_ready and tool_name != "answer":
    return False, "final_ready active — no tools after final gate"

  from .source_registry import SOURCE_TOOL_NAMES, SourceRegistry

  if plan.source_registry and plan.source_candidates:
    sid = str(args.get("_source_id") or args.get("source_id") or "")
    if sid:
      reg = SourceRegistry.from_dict(plan.source_registry)
      if sid in reg.source_ids():
        return True, ""

  if plan.disallowed_tools and tool_name in plan.disallowed_tools:
    return False, f"{tool_name} disallowed for this plan"

  if plan.source_registry and plan.source_candidates:
    sid = str(args.get("_source_id") or args.get("source_id") or "")
    if tool_name in {"Read", "Grep", "Glob"} and not sid:
      return False, "path-based tool blocked — use ReadSource/GrepSource/GlobSource with source_id"
  if tool_name in SOURCE_TOOL_NAMES:
    reg = SourceRegistry.from_dict(plan.source_registry or {})
    sid = str(args.get("source_id") or "")
    if sid not in reg.source_ids():
      return False, f"unknown or unavailable source_id: {sid}"
    return True, ""
  if plan.allowed_tools and tool_name not in plan.allowed_tools:
    return False, f"{tool_name} not in allowed_tools"
  if tool_name == "Shell":
    from .target_coverage import is_home_shell_command, shell_explicitly_requested

    cmd = str(args.get("command") or "")
    if is_home_shell_command(cmd) and not shell_explicitly_requested(plan.goal):
      return False, "Shell on home directory blocked — use Read on preferred_sources"
  if tool_name in plan.banned_tools:
    return False, "tool is banned by plan"
  if violates_avoid_actions(tool_name, args, plan.avoid_actions):
    return False, "tool violates avoid_actions"
  sig = action_signature(tool_name, args)
  if plan.failed_actions.get(sig, 0) >= 2:
    return False, "same failed action repeated"
  if plan.known_files and tool_name == "Glob":
    if _pick_read_target(plan.known_files):
      return False, "known files exist; use Read first"
  if tool_name == "Read":
    path = str(args.get("path") or "")
    try:
      from .read_guard import check_read_allowed, format_read_guard_message

      allowed, reason, info = check_read_allowed(path, args)
      if not allowed and info:
        alts = "; ".join((info.get("next_allowed") or [])[:3])
        return False, f"{reason}: {format_read_guard_message(info)[:200]} | try: {alts}"
    except ImportError:
      pass
  return True, ""
