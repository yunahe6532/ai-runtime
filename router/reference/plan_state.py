"""Per-request plan state: done/blocked/next/evidence for ReAct loop steering."""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Literal

from artifact_analyzer import extract_compose_port_evidence, format_analysis_compact, html_validation_command
from adapters.memory import Artifact, SessionState, normalize_file_path
from adapters.retrieval import load_artifact_meta

LOG = logging.getLogger("router.plan_state")

AgentPhase = Literal["tool_planning", "final_answer", "partial_final_answer", "recovery_final"]

VALIDATION_QUERY_KW = (
  "html validation",
  "validator",
  "w3c",
  "dom validation",
  "invalid html",
  "markup error",
  "html 검증",
  "마크업 검증",
  "dom 검증",
  "접근성 검사",
)

READ_BLOCK_AFTER = int(os.getenv("READ_BLOCK_AFTER", "1"))
MIN_TOOL_CALLS_FOR_FINAL_ANSWER = int(os.getenv("MIN_TOOL_CALLS_FOR_FINAL_ANSWER", "3"))

COMPOSE_PORT_QUERY_KW = ("포트", "port")
COMPOSE_PORT_CONTEXT_KW = ("docker-compose", "compose.yml", "compose", "router")


@dataclass
class PhaseState:
  """Event-sourced phase tracking — updated from new messages only."""

  current_phase: str = "tool_planning"
  last_user_query_key: str = ""
  final_ready: bool = False
  final_ready_query_key: str = ""
  tool_call_turns_since_user: int = 0
  last_tool_result_key: str = ""
  last_role: str = ""
  final_ready_cancelled_count: int = 0
  noise_count: int = 0
  awaiting_tool_result: bool = False

  def to_dict(self) -> dict[str, Any]:
    return {
      "current_phase": self.current_phase,
      "last_user_query_key": self.last_user_query_key,
      "final_ready": self.final_ready,
      "final_ready_query_key": self.final_ready_query_key,
      "tool_call_turns_since_user": self.tool_call_turns_since_user,
      "last_tool_result_key": self.last_tool_result_key,
      "last_role": self.last_role,
      "final_ready_cancelled_count": self.final_ready_cancelled_count,
      "noise_count": self.noise_count,
      "awaiting_tool_result": self.awaiting_tool_result,
    }

  @classmethod
  def from_dict(cls, data: dict[str, Any] | None) -> PhaseState:
    if not data:
      return cls()
    return cls(
      current_phase=str(data.get("current_phase") or "tool_planning"),
      last_user_query_key=str(data.get("last_user_query_key") or ""),
      final_ready=bool(data.get("final_ready")),
      final_ready_query_key=str(data.get("final_ready_query_key") or ""),
      tool_call_turns_since_user=int(data.get("tool_call_turns_since_user") or 0),
      last_tool_result_key=str(data.get("last_tool_result_key") or ""),
      last_role=str(data.get("last_role") or ""),
      final_ready_cancelled_count=int(data.get("final_ready_cancelled_count") or 0),
      noise_count=int(data.get("noise_count") or 0),
      awaiting_tool_result=bool(data.get("awaiting_tool_result")),
    )


def load_phase_state(state: SessionState | None) -> PhaseState:
  if not state:
    return PhaseState()
  raw = getattr(state, "phase_state", None) or {}
  if isinstance(raw, dict) and raw:
    return PhaseState.from_dict(raw)
  return PhaseState()


def save_phase_state(state: SessionState, ps: PhaseState) -> None:
  state.phase_state = ps.to_dict()


def apply_phase_events(
  state: SessionState,
  indexed_messages: list[Any],
) -> PhaseState:
  """Update phase state from newly indexed messages only."""
  from message_index import IndexedMessage, is_noise_kind

  ps = load_phase_state(state)
  ap = state.agent_plan or {}
  router_intent = str(ap.get("router_intent") or "")
  min_tc = MIN_TOOL_CALLS_FOR_FINAL_ANSWER
  if router_intent == "read_only_analysis" or ap.get("evidence_needed") == ["target_coverage"]:
    min_tc = 1

  for item in indexed_messages:
    if isinstance(item, IndexedMessage):
      im = item
    elif isinstance(item, dict):
      from message_index import index_message

      im = index_message(item, int(item.get("index", 0)))
    else:
      continue

    if is_noise_kind(im.kind):
      ps.noise_count += 1
      ps.last_role = im.role
      continue

    if im.kind == "user_task":
      ps.last_user_query_key = im.key
      ps.tool_call_turns_since_user = 0
      ps.final_ready = bool(ap.get("final_ready"))
      if ps.final_ready and ps.final_ready_query_key != im.key:
        ps.final_ready = False
      ps.current_phase = "tool_planning"
      ps.awaiting_tool_result = False
      ps.last_role = "user"
      continue

    if im.kind == "assistant_tool_call":
      ps.tool_call_turns_since_user += 1
      ps.current_phase = "tool_planning"
      ps.awaiting_tool_result = True
      ps.last_role = "assistant"
      if ps.final_ready:
        ps.final_ready = False
        ps.final_ready_cancelled_count += 1
        try:
          from .planner import AgentPlan, revoke_final_ready

          plan = AgentPlan.from_dict(ap) if ap else None
          if plan:
            revoke_final_ready(plan, state, "post_final_tool_call", emit_run_events=False)
            state.agent_plan = plan.to_dict()
            ap = state.agent_plan
        except ImportError:
          pass
      continue

    if im.kind == "tool_result":
      ps.last_tool_result_key = im.key
      ps.awaiting_tool_result = False
      ps.last_role = "tool"
      if ps.final_ready:
        ps.final_ready = False
        ps.final_ready_cancelled_count += 1
      if bool(ap.get("final_ready")):
        ps.current_phase = "final_answer"
      else:
        ps.current_phase = "tool_planning"
      continue

    if im.kind == "assistant_final":
      ps.last_role = "assistant"
      ps.awaiting_tool_result = False
      continue

    ps.last_role = im.role

  if bool(ap.get("final_ready")):
    ps.final_ready = True
    ps.final_ready_query_key = ps.last_user_query_key
    if ps.last_role == "tool" and ps.tool_call_turns_since_user >= min_tc:
      ps.current_phase = "final_answer"

  save_phase_state(state, ps)
  return ps


def resolve_phase_from_state(
  state: SessionState | None,
  intent_name: str,
  needs_tools: bool,
) -> AgentPhase | None:
  """Fast path: derive phase from event-sourced PhaseState without full message scan."""
  if not state or not needs_tools:
    return None
  ps = load_phase_state(state)
  if not ps.last_role:
    return None

  ap = state.agent_plan or {}
  router_intent = str(ap.get("router_intent") or "")
  min_tc = MIN_TOOL_CALLS_FOR_FINAL_ANSWER
  if router_intent == "read_only_analysis" or ap.get("evidence_needed") == ["target_coverage"]:
    min_tc = 1

  if ps.final_ready and bool(ap.get("final_ready")) and ps.last_role == "tool":
    if ps.tool_call_turns_since_user >= min_tc:
      LOG.info("phase_result=final_answer reason=phase_state_event")
      return "final_answer"

  if ps.current_phase == "final_answer" and ps.last_role == "tool":
    LOG.info("phase_result=final_answer reason=phase_state_current")
    return "final_answer"

  if ps.last_role in ("tool", "assistant") and ps.awaiting_tool_result:
    LOG.info("phase_result=tool_planning reason=phase_state_awaiting_tool")
    return "tool_planning"

  if ps.last_role == "user":
    LOG.info("phase_result=tool_planning reason=phase_state_new_user")
    return "tool_planning"

  return None


def _count_tool_calls_after_last_user(messages: list[dict[str, Any]], last_user: int) -> int:
  count = 0
  for msg in messages[last_user + 1 :]:
    if isinstance(msg, dict) and msg.get("role") == "assistant" and msg.get("tool_calls"):
      count += 1
  return count


@dataclass
class BlockedTool:
  tool: str
  path: str
  reason: str


@dataclass
class PlanState:
  task: str
  task_kind: str  # validation | compose_port | general
  phase: str  # tool_planning | validation_required | final_answer_ready
  done: list[str] = field(default_factory=list)
  blocked: list[BlockedTool] = field(default_factory=list)
  next_allowed: list[str] = field(default_factory=list)
  needed_evidence: list[str] = field(default_factory=list)
  satisfied_evidence: list[str] = field(default_factory=list)
  file_analyses: dict[str, dict[str, Any]] = field(default_factory=dict)
  repeated_read_paths: dict[str, int] = field(default_factory=dict)


def is_validation_task(query: str) -> bool:
  q = (query or "").lower()
  if any(kw in q for kw in VALIDATION_QUERY_KW):
    return True
  try:
    from .planner import detect_keyword_hints

    return "html_validation" in detect_keyword_hints(query)
  except ImportError:
    return False


def is_compose_port_task(query: str) -> bool:
  q = (query or "").lower()
  has_port = any(kw in q for kw in COMPOSE_PORT_QUERY_KW)
  has_context = any(kw in q for kw in COMPOSE_PORT_CONTEXT_KW)
  return has_port and has_context


def _merge_port_meta(meta: dict[str, Any], text: str) -> dict[str, Any]:
  merged = dict(meta)
  port_ev = extract_compose_port_evidence(text)
  if port_ev:
    merged.update(port_ev)
  return merged


def _port_from_meta(meta: dict[str, Any]) -> str | None:
  for key in ("router_port", "host_port"):
    val = meta.get(key)
    if val:
      return str(val)
  return None


def evidence_satisfied(plan: PlanState) -> bool:
  if not plan.needed_evidence:
    return True
  for needed in plan.needed_evidence:
    key = needed.split(":", 1)[-1]
    if not any(key in sat or sat.endswith(f":{key}") for sat in plan.satisfied_evidence):
      return False
  return True


def _path_variants(path: str, workspace: str) -> set[str]:
  norm = normalize_file_path(path, workspace)
  variants = {path, norm}
  if norm:
    variants.add(norm)
    base = norm.rsplit("/", 1)[-1]
    variants.add(base)
  return {v for v in variants if v}


def _collect_file_analyses(state: SessionState) -> dict[str, dict[str, Any]]:
  out: dict[str, dict[str, Any]] = dict(state.file_meta or {})
  ws = state.workspace_path or ""
  for path in state.files_read[-20:]:
    norm = normalize_file_path(path, ws) or path
    if norm in out:
      continue
    for aid in reversed(state.artifacts):
      art = load_artifact_meta(aid, state.project_key)
      if not art or art.type != "file_read":
        continue
      art_norm = normalize_file_path(art.path, ws) or art.path
      if art_norm == norm or art.path == path:
        if art.analysis:
          out[norm] = art.analysis
        break
  return out


def build_plan_state(
  state: SessionState,
  query: str,
  artifacts: list[Artifact] | None = None,
  messages: list[dict[str, Any]] | None = None,
) -> PlanState:
  ws = state.effective_workspace or state.workspace_path or ""
  try:
    from .planner import detect_keyword_hints
    from .evidence_extractors import looks_like_project_inspection

    hints = detect_keyword_hints(query)
    if "benchmark_analysis" in hints:
      task_kind = "benchmark_analysis"
    elif looks_like_project_inspection(query):
      task_kind = "project_inspection"
    elif (
      state.agent_plan
      and state.agent_plan.get("task_intent") in {"project_inspection", "repo_summary", "code_read"}
    ):
      task_kind = str(state.agent_plan.get("task_intent"))
    elif is_validation_task(query):
      task_kind = "validation"
    elif is_compose_port_task(query):
      task_kind = "compose_port"
    else:
      task_kind = "general"
  except ImportError:
    if is_validation_task(query):
      task_kind = "validation"
    elif is_compose_port_task(query):
      task_kind = "compose_port"
    else:
      task_kind = "general"
  analyses = _collect_file_analyses(state)
  read_counts = dict(state.read_counts or {})

  done: list[str] = []
  blocked: list[BlockedTool] = []
  next_allowed: list[str] = ["Shell", "Grep", "Glob"]
  needed: list[str] = []
  satisfied: list[str] = []

  for path, meta in analyses.items():
    done.append(f"file_read cached: {path} ({meta.get('chars', '?')} chars)")
    if meta.get("kind") == "html":
      satisfied.append(f"html_structure:{path}")
    if meta.get("kind") == "html_validation" and meta.get("validation_ok"):
      satisfied.append(f"html_validated:{path}")
    port = _port_from_meta(meta)
    if port:
      satisfied.append(f"router_port:{port}")

  for path in state.files_read[-20:]:
    norm = normalize_file_path(path, ws) or path
    if norm in analyses and _port_from_meta(analyses.get(norm, {})):
      continue
    for aid in reversed(state.artifacts):
      art = load_artifact_meta(aid, state.project_key)
      if not art or art.type != "file_read":
        continue
      art_norm = normalize_file_path(art.path, ws) or art.path
      if art_norm != norm and art.path != path:
        continue
      text = art.summary or ""
      port_ev = extract_compose_port_evidence(text)
      if port_ev.get("router_port"):
        satisfied.append(f"router_port:{port_ev['router_port']}")
        analyses[norm] = _merge_port_meta(analyses.get(norm, {"path": norm, "kind": "file"}), text)
      break

  if messages:
    for msg in messages:
      if not isinstance(msg, dict) or msg.get("role") != "tool":
        continue
      text = str(msg.get("content") or "")
      port_ev = extract_compose_port_evidence(text)
      if port_ev.get("router_port"):
        if not any(s.startswith("router_port:") for s in satisfied):
          satisfied.append(f"router_port:{port_ev['router_port']}")
        analyses["__session_tool__"] = _merge_port_meta(
          {"path": "", "kind": "docker_compose"},
          text,
        )

  for path, count in read_counts.items():
    meta = analyses.get(path) or (state.file_meta.get(path) if state.file_meta else {})
    if isinstance(meta, dict) and meta.get("kind") == "grep":
      continue
    if count >= READ_BLOCK_AFTER:
      blocked.append(
        BlockedTool(
          tool="Read",
          path=path,
          reason=f"already read {count}x; use cached artifact summary",
        )
      )

  html_paths = [p for p, m in analyses.items() if m.get("kind") == "html"]

  if task_kind == "validation":
    for hp in html_paths:
      meta = analyses.get(hp, {})
      if meta.get("validation_ok"):
        satisfied.append(f"html_validated:{hp}")
      else:
        needed.append(f"html_validated:{hp}")
        next_allowed.insert(0, f"Shell html validation for {hp}")
    for hp in html_paths:
      if hp in read_counts and read_counts[hp] >= READ_BLOCK_AFTER:
        blocked.append(
          BlockedTool(tool="Read", path=hp, reason="HTML cached — use artifact summary or Shell validation")
        )
    phase = "validation_required"
    if html_paths and all(analyses.get(p, {}).get("validation_ok") for p in html_paths):
      phase = "final_answer_ready"
    elif not needed:
      phase = "final_answer_ready"
  elif task_kind == "compose_port":
    needed = ["router_port_mapping"]
    if not any(s.startswith("router_port:") for s in satisfied):
      for _path, meta in list(analyses.items()):
        port = _port_from_meta(meta)
        if port:
          satisfied.append(f"router_port:{port}")
    if any(s.startswith("router_port:") for s in satisfied):
      needed = []
      phase = "final_answer_ready"
      next_allowed = ["prose_answer_only"]
    else:
      phase = "validation_required"
      next_allowed = ["Grep docker-compose ports", "Read docker-compose.yml (once)"]
  elif task_kind == "benchmark_analysis":
    needed = ["agent benchmark result", "runtime score result"]
    for ev in list(state.agent_plan.get("evidence_collected", []) if state.agent_plan else []):
      satisfied.append(ev)
    if state.agent_plan:
      try:
        from .evidence_extractors import evidence_types_satisfied

        if evidence_types_satisfied(
          needed,
          list(state.agent_plan.get("evidence_collected", [])),
          task_intent="benchmark_analysis",
        ):
          needed = []
          phase = "final_answer_ready"
      except ImportError:
        pass
    if needed:
      phase = "validation_required"
      next_allowed = ["Read benchmark JSON files", "Read flow trace"]
  elif task_kind == "project_inspection":
    needed = ["target_coverage"]
    for ev in list(state.agent_plan.get("evidence_collected", []) if state.agent_plan else []):
      satisfied.append(ev)
    try:
      from .target_coverage import target_coverage_passes

      ap = state.agent_plan or {}
      targets = list(ap.get("preferred_sources") or [])
      ctx = ap.get("context_need") if isinstance(ap.get("context_need"), dict) else {}
      if ctx.get("coverage_targets"):
        targets = list(dict.fromkeys(targets + list(ctx.get("coverage_targets") or [])))
      if targets and target_coverage_passes(ap, targets):
        needed = []
        phase = "final_answer_ready"
      else:
        phase = "tool_planning"
        missing_ids: list[str] = []
        try:
          from .source_registry import SourceRegistry, required_source_ids_from_plan

          reg_raw = ap.get("source_registry") or {}
          if reg_raw:
            reg = SourceRegistry.from_dict(reg_raw)
            hits = set(ap.get("source_hits") or [])
            for sid in required_source_ids_from_plan(ap, reg):
              if sid not in hits:
                missing_ids.append(sid)
        except ImportError:
          pass
        if missing_ids:
          next_allowed = [f"ReadSource/GlobSource {sid}" for sid in missing_ids[:6]]
        else:
          next_allowed = ["GlobSource on undiscovered dirs", "ReadSource on summary docs"]
    except ImportError:
      phase = "tool_planning"
  else:
    phase = "tool_planning"

  if artifacts:
    for art in artifacts:
      if art.type == "file_read" and art.path:
        norm = normalize_file_path(art.path, ws) or art.path
        read_counts[norm] = read_counts.get(norm, 0) + 1

  return PlanState(
    task=query[:300],
    task_kind=task_kind,
    phase=phase,
    done=done[-8:],
    blocked=blocked,
    next_allowed=next_allowed[:6],
    needed_evidence=needed,
    satisfied_evidence=satisfied,
    file_analyses=analyses,
    repeated_read_paths=read_counts,
  )


def format_plan_state_block(plan: PlanState, budget_tokens: int) -> str:
  from context_budget import truncate_to_token_budget

  lines = [
    "[Current Plan State]",
    f"task: {plan.task[:200]}",
    f"task_kind: {plan.task_kind}",
    f"phase: {plan.phase}",
  ]
  if plan.done:
    lines.append("done:")
    for item in plan.done:
      lines.append(f"- {item}")
  if plan.blocked:
    lines.append("blocked:")
    for b in plan.blocked[:8]:
      lines.append(f"- DO NOT {b.tool} path={b.path} ({b.reason})")
  if plan.next_allowed:
    lines.append("next_allowed:")
    for item in plan.next_allowed:
      lines.append(f"- {item}")
  if plan.needed_evidence:
    lines.append("needed_evidence:")
    for item in plan.needed_evidence:
      lines.append(f"- {item}")
  if plan.satisfied_evidence:
    lines.append("satisfied_evidence:")
    for item in plan.satisfied_evidence:
      lines.append(f"- {item}")
  if plan.file_analyses:
    lines.append("artifact_summaries:")
    for path, meta in list(plan.file_analyses.items())[:4]:
      lines.append(format_analysis_compact(meta, max_chars=500))
  lines.append("[/Current Plan State]")
  return truncate_to_token_budget("\n".join(lines), budget_tokens)


def is_read_blocked(plan: PlanState, path: str, workspace: str = "") -> bool:
  variants = _path_variants(path, workspace)
  for b in plan.blocked:
    if b.tool != "Read":
      continue
    bvars = _path_variants(b.path, workspace)
    if variants & bvars:
      return True
  return False


def validation_shell_for_path(path: str) -> str:
  return html_validation_command(path)


def build_compose_port_answer(plan: PlanState, query: str) -> str:
  """Answer port questions from cached compose / env evidence."""
  for _path, meta in plan.file_analyses.items():
    port = _port_from_meta(meta)
    if port:
      container = meta.get("container_port") or port
      return (
        f"router 서비스 포트는 ${'{'}PORT:-{port}{'}'} "
        f"(호스트 {port} → 컨테이너 {container})입니다."
      )
  for ev in plan.satisfied_evidence:
    if ev.startswith("router_port:"):
      port = ev.split(":", 1)[1]
      return f"router 서비스 포트는 ${'{'}PORT:-{port}{'}'} ({port})입니다."
  return ""


def _plan_task_kind(plan: Any) -> str:
  kind = str(getattr(plan, "task_kind", "") or "").strip()
  if kind:
    return kind
  return str(getattr(plan, "task_intent", "") or getattr(plan, "router_intent", "") or "")


def _build_read_only_structure_answer(plan: Any) -> str:
  """read_only final prose comes from LLM + PromptPack evidence — no runtime stub."""
  _ = plan
  return ""


def build_evidence_answer(plan: Any, query: str) -> str:
  """Compose prose from structured evidence (validation, compose_port). read_only uses LLM final pass."""
  if _is_read_only_coverage_plan(plan):
    return ""
  task_kind = _plan_task_kind(plan)
  file_analyses = getattr(plan, "file_analyses", None) or {}
  needed_evidence = list(getattr(plan, "needed_evidence", None) or [])
  satisfied_evidence = list(getattr(plan, "satisfied_evidence", None) or [])

  if task_kind == "compose_port":
    ans = build_compose_port_answer(plan, query)
    if ans:
      return ans
  if task_kind == "validation":
    ans = build_final_answer_from_plan(plan, query)
    if ans and "없습니다" not in ans[:40]:
      return ans
  ans = build_compose_port_answer(plan, query)
  if ans:
    return ans
  if needed_evidence and not all(
    any(ev.split(":", 1)[-1] in s for s in satisfied_evidence) for ev in needed_evidence
  ):
    return (
      "required evidence가 아직 충족되지 않았습니다. "
      "docker-compose.yml의 ports/PORT 설정을 확인한 뒤 답변하세요."
    )
  return ""


def build_final_answer_from_plan(plan: Any, query: str) -> str:
  """HTML validation tasks only — never used for read_only / project structure."""
  _ = query
  if _is_read_only_coverage_plan(plan):
    return ""
  if _plan_task_kind(plan) != "validation":
    return ""
  lines = ["요청하신 검증 결과를 artifact 분석 기준으로 정리합니다.", ""]
  file_analyses = getattr(plan, "file_analyses", None) or {}
  if not file_analyses:
    return (
      "파일 분석 결과가 plan state에 없습니다. "
      "Shell로 HTML 구조 검증을 먼저 실행해 주세요."
    )
  for path, meta in file_analyses.items():
    kind = meta.get("kind", "")
    if kind in ("html", "html_validation"):
      lines.append(f"**{path}**")
      lines.append(f"- 크기: {meta.get('chars', '?')} chars / {meta.get('lines', '?')} lines")
      lines.append(f"- Mermaid 블록: {meta.get('mermaid_blocks', '?')}개")
      lines.append(f"- HTMLParser 오류: {meta.get('html_parse_errors', '?')}건")
      lines.append(f"- DOCTYPE: {'✅' if meta.get('has_doctype') else '❌'}")
      lines.append(f"- `</html>` 닫힘: {'✅' if meta.get('closes_html') else '❌'}")
      lines.append(f"- charset: {'✅' if meta.get('has_charset') else '❌'}")
      lines.append(f"- lang=ko: {'✅' if meta.get('lang_ko') else '△'}")
      ok = meta.get("validation_ok")
      lines.append(f"- 구조 검증: {'✅ 통과' if ok else '△ 추가 Shell 검증 권장'}")
      lines.append("")
  needed_evidence = list(getattr(plan, "needed_evidence", None) or [])
  satisfied_evidence = list(getattr(plan, "satisfied_evidence", None) or [])
  if needed_evidence and not all(
    any(ev.split(":", 1)[-1] in s for s in satisfied_evidence) for ev in needed_evidence
  ):
    lines.append("※ 일부 required evidence가 아직 충족되지 않았습니다.")
  return "\n".join(lines).strip()


def _next_action_suggests_answer(ap: Any | None) -> bool:
  if ap is None:
    return False
  na = getattr(ap, "next_action", None) or {}
  if isinstance(na, dict):
    tool = str(na.get("tool") or "").strip().lower()
  else:
    tool = str(getattr(na, "tool", "") or "").strip().lower()
  return tool in ("answer", "final", "final_answer")


def _min_tool_calls_for_final(ap: Any | None, intent_name: str = "") -> int:
  if ap is None:
    return MIN_TOOL_CALLS_FOR_FINAL_ANSWER
  router_intent = str(getattr(ap, "router_intent", "") or "")
  evidence_needed = list(getattr(ap, "evidence_needed", None) or [])
  if router_intent == "read_only_analysis" or evidence_needed == ["target_coverage"]:
    return 1
  return MIN_TOOL_CALLS_FOR_FINAL_ANSWER


def _answer_action_may_finalize(
  ap: Any,
  state: SessionState | None,
  *,
  intent_name: str,
  tool_call_turns: int,
  last_role: str,
) -> bool:
  """next_action=answer is necessary but not sufficient for final_answer."""
  if not _next_action_suggests_answer(ap):
    return False
  if last_role != "tool":
    return False
  from .planner import can_final_answer

  if not can_final_answer(ap):
    return False
  if not bool(getattr(ap, "final_ready", False)):
    return False
  min_tc = _min_tool_calls_for_final(ap, intent_name)
  if tool_call_turns < min_tc:
    return False
  from .loop_guard import should_block_final_answer

  blocked, _reason = should_block_final_answer(
    state,
    can_final=True,
    task_intent=str(getattr(ap, "task_intent", "") or ""),
    intent_name=intent_name,
  )
  return not blocked


def _next_action_forces_final(ap: Any | None) -> bool:
  """Deprecated alias — do not use for phase resolution without coverage gates."""
  return _next_action_suggests_answer(ap)


def _evidence_count(ap: Any | None, state: SessionState | None = None) -> int:
  collected: list[str] = []
  if ap is not None:
    collected = list(getattr(ap, "evidence_collected", None) or [])
  elif state and state.agent_plan:
    collected = list((state.agent_plan or {}).get("evidence_collected") or [])
  return len(collected)


def _read_only_coverage_incomplete(ap: Any | None) -> bool:
  if ap is None:
    return False
  if str(getattr(ap, "router_intent", "") or "") != "read_only_analysis":
    return False
  try:
    from .planner import can_final_answer

    return not can_final_answer(ap)
  except ImportError:
    return True


def _clear_ping_pong_for_retry(state: SessionState | None) -> None:
  if state is None:
    return
  state.same_action_repeated = 0
  state.turns_since_progress = 0


def _agent_plan_for_phase(state: SessionState | None) -> Any | None:
  if not state or not state.agent_plan:
    return None
  try:
    from .planner import AgentPlan

    return AgentPlan.from_dict(state.agent_plan)
  except ImportError:
    return None


def _resolve_with_agent_plan(
  state: SessionState | None,
  ap: Any,
  *,
  last_role: str,
  tool_call_turns: int,
  intent_name: str,
  query: str = "",
) -> AgentPhase | None:
  from .loop_guard import emit_plan_repair, should_block_final_answer
  from .planner import AgentPlan, can_final_answer

  if last_role != "tool":
    return None

  task_intent = str(ap.task_intent or "general")

  if task_intent == "general" and not ap.evidence_needed:
    LOG.info("phase_result=tool_planning reason=general_no_evidence_needed")
    return "tool_planning"

  if _answer_action_may_finalize(
    ap,
    state,
    intent_name=intent_name,
    tool_call_turns=tool_call_turns,
    last_role=last_role,
  ):
    LOG.info("phase_result=final_answer reason=next_action_answer_coverage_ok")
    return "final_answer"

  blocked, reason = should_block_final_answer(
    state,
    can_final=can_final_answer(ap),
    task_intent=task_intent,
    intent_name=intent_name,
  )
  if blocked and reason in ("bad_ping_pong", "final_already_sent_this_turn"):
    if reason == "bad_ping_pong":
      emit_plan_repair(reason)
      if can_final_answer(ap):
        LOG.info("phase_result=final_answer reason=coverage_complete_despite_ping_pong")
        return "final_answer"
      if _read_only_coverage_incomplete(ap):
        try:
          from .read_only_explorer import exploration_checklist_pending

          pending = exploration_checklist_pending(ap.to_dict(), ap.source_registry)
          if not pending and can_final_answer(ap):
            LOG.info("phase_result=final_answer reason=read_only_checklist_complete_despite_ping_pong")
            return "final_answer"
        except ImportError:
          pass
        emit_plan_repair("bad_ping_pong_escalate_glob")
        _clear_ping_pong_for_retry(state)
        LOG.info("phase_result=tool_planning reason=bad_ping_pong_coverage_incomplete")
        return "tool_planning"
      if _is_read_only_coverage_plan(ap):
        LOG.info("phase_result=tool_planning reason=read_only_bad_ping_pong")
        return "tool_planning"
      if _evidence_count(ap, state) > 0:
        LOG.info("phase_result=partial_final_answer reason=bad_ping_pong_partial")
        return "partial_final_answer"
    LOG.info("phase_result=tool_planning reason=%s", reason)
    return "tool_planning"

  try:
    from .evidence_judge import EVIDENCE_JUDGE_ENABLED, evaluate_exploration, phase_from_decision

    router_intent = str(getattr(ap, "router_intent", "") or "")
    read_only_cov = router_intent == "read_only_analysis" or list(ap.evidence_needed or []) == ["target_coverage"]
    if read_only_cov:
      from .read_only_explorer import READ_ONLY_EXPLORER_ENABLED, refresh_read_only_exploration_plan

      if READ_ONLY_EXPLORER_ENABLED and state is not None:
        decision = refresh_read_only_exploration_plan(state, ap, query or str(ap.goal or ""))
        state.agent_plan = ap.to_dict()
        if decision.allow_final and can_final_answer(ap):
          LOG.info("phase_result=final_answer reason=read_only_explorer_allow_final")
          return "final_answer"
      if can_final_answer(ap):
        LOG.info("phase_result=final_answer reason=read_only_coverage_complete")
        return "final_answer"
      LOG.info("phase_result=tool_planning reason=read_only_exploration_incomplete")
      return "tool_planning"

    if EVIDENCE_JUDGE_ENABLED and state is not None:
      plan_copy = AgentPlan.from_dict(ap.to_dict())
      _static, decision = evaluate_exploration(
        state,
        plan_copy,
        query=query or str(ap.goal or ""),
        tool_call_turns=tool_call_turns,
        intent_name=intent_name,
      )
      state.agent_plan = plan_copy.to_dict()
      phase = phase_from_decision(decision)
      if phase == "final_answer":
        min_tc = _min_tool_calls_for_final(plan_copy, intent_name)
        if tool_call_turns < min_tc:
          LOG.info(
            "phase_result=tool_planning reason=judge_final_but_tc<%d",
            min_tc,
          )
          return "tool_planning"
        blocked2, reason2 = should_block_final_answer(
          state,
          can_final=True,
          task_intent=task_intent,
          intent_name=intent_name,
        )
        if blocked2:
          LOG.info("phase_result=tool_planning reason=judge_blocked_%s", reason2)
          return "tool_planning"
        LOG.info("phase_result=final_answer reason=evidence_judge_%s", decision.decision)
        return "final_answer"
      LOG.info("phase_result=tool_planning reason=evidence_judge_%s", decision.decision)
      return "tool_planning"
  except ImportError:
    pass

  from .evidence_extractors import evidence_types_satisfied

  router_intent = str(getattr(ap, "router_intent", "") or "")
  evidence_needed = list(getattr(ap, "evidence_needed", None) or [])
  read_only_coverage = (
    router_intent == "read_only_analysis"
    or evidence_needed == ["target_coverage"]
  )
  if read_only_coverage:
    if not can_final_answer(ap):
      LOG.info("phase_result=tool_planning reason=source_coverage_incomplete")
      return "tool_planning"
  elif evidence_needed and not evidence_types_satisfied(
    evidence_needed,
    ap.evidence_collected,
    task_intent=task_intent,
  ):
    try:
      from .loop_guard import is_bad_ping_pong

      if state and is_bad_ping_pong(state) and _evidence_count(ap, state) > 0:
        if _is_read_only_coverage_plan(ap):
          LOG.info("phase_result=tool_planning reason=read_only_evidence_incomplete_ping_pong")
          return "tool_planning"
        LOG.info("phase_result=partial_final_answer reason=evidence_incomplete_ping_pong")
        return "partial_final_answer"
    except ImportError:
      pass
    LOG.info("phase_result=tool_planning reason=agent_plan_evidence_incomplete")
    return "tool_planning"

  if not can_final_answer(ap):
    LOG.info("phase_result=tool_planning reason=agent_plan_no_done_signal")
    return "tool_planning"

  if not ap.final_ready:
    LOG.info("phase_result=tool_planning reason=final_ready_not_set")
    return "tool_planning"

  if tool_call_turns < _min_tool_calls_for_final(ap, intent_name):
    LOG.info(
      "phase_result=tool_planning reason=tc_turns(%d)<%d",
      tool_call_turns,
      _min_tool_calls_for_final(ap, intent_name),
    )
    return "tool_planning"

  LOG.info("phase_result=final_answer reason=agent_plan_evidence_ready")
  return "final_answer"


def _cursor_looping_flagged(messages: list[Any]) -> bool:
  for msg in reversed(messages[-4:]):
    if not isinstance(msg, dict) or msg.get("role") != "user":
      continue
    text = str(msg.get("content") or "").lower()
    if "flagged as looping" in text or "repeating response pattern" in text:
      return True
  return False


def _is_read_only_coverage_plan(ap: Any | None) -> bool:
  if ap is None:
    return False
  router_intent = str(getattr(ap, "router_intent", "") or "")
  evidence_needed = list(getattr(ap, "evidence_needed", None) or [])
  return router_intent == "read_only_analysis" or evidence_needed == ["target_coverage"]


def _read_only_source_registry_active(state: SessionState | None) -> bool:
  if not state or not state.agent_plan:
    return False
  ap = state.agent_plan
  return str(ap.get("router_intent") or "") == "read_only_analysis" and bool(ap.get("source_registry"))


def resolve_agent_phase(
  body: dict[str, Any],
  state: SessionState | None,
  query: str,
  intent_name: str,
  needs_tools: bool,
) -> AgentPhase | None:
  if intent_name == "casual" or not needs_tools:
    return None
  if intent_name not in (
    "shell_task",
    "benchmark",
    "log_analysis",
    "code_edit",
    "read_only_analysis",
    "agent",
    "debug",
  ) and not needs_tools:
    return None

  messages = body.get("messages", [])
  if not isinstance(messages, list):
    messages = []

  if _cursor_looping_flagged(messages):
    ap_loop = _agent_plan_for_phase(state)
    if ap_loop:
      try:
        from .planner import can_final_answer

        if can_final_answer(ap_loop):
          LOG.info("phase_result=final_answer reason=cursor_looping_flag_coverage_ok")
          return "final_answer"
      except ImportError:
        pass
      LOG.info("phase_result=tool_planning reason=cursor_looping_flag_read_only_or_incomplete")
      return "tool_planning"

  ap_boot = _agent_plan_for_phase(state)
  if ap_boot and _answer_action_may_finalize(
    ap_boot,
    state,
    intent_name=intent_name,
    tool_call_turns=max(
      1,
      load_phase_state(state).tool_call_turns_since_user if state else 0,
    ),
    last_role=str((body.get("messages") or [{}])[-1].get("role") if body.get("messages") else ""),
  ):
    LOG.info("phase_result=final_answer reason=next_action_answer_coverage_ok")
    return "final_answer"

  try:
    from .loop_guard import emit_plan_repair, is_bad_ping_pong

    if state and is_bad_ping_pong(state):
      if _evidence_count(ap_boot, state) > 0:
        try:
          from .planner import can_final_answer

          if ap_boot and can_final_answer(ap_boot):
            LOG.info("phase_result=final_answer reason=coverage_complete_despite_ping_pong")
            return "final_answer"
        except ImportError:
          pass
        if _read_only_coverage_incomplete(ap_boot):
          try:
            from .read_only_explorer import exploration_checklist_pending

            pending = exploration_checklist_pending(ap_boot.to_dict(), ap_boot.source_registry)
            if not pending and can_final_answer(ap_boot):
              LOG.info("phase_result=final_answer reason=read_only_checklist_complete_despite_ping_pong")
              return "final_answer"
          except ImportError:
            pass
          emit_plan_repair("bad_ping_pong_escalate_glob")
          _clear_ping_pong_for_retry(state)
          LOG.info("phase_result=tool_planning reason=bad_ping_pong_coverage_incomplete")
          return "tool_planning"
        if _is_read_only_coverage_plan(ap_boot):
          LOG.info("phase_result=tool_planning reason=read_only_bad_ping_pong")
          return "tool_planning"
        LOG.info("phase_result=partial_final_answer reason=bad_ping_pong_partial")
        return "partial_final_answer"
  except ImportError:
    pass

  event_phase = resolve_phase_from_state(state, intent_name, needs_tools)
  if event_phase is not None and state and getattr(state, "last_ingest_metrics", None):
    metrics = state.last_ingest_metrics or {}
    if metrics.get("diff_mode") == "append_only" and int(metrics.get("messages_new", 99)) <= 5:
      return event_phase

  messages = body.get("messages", [])
  if not isinstance(messages, list) or not messages:
    return "tool_planning"

  last_role = messages[-1].get("role") if messages else ""
  last_user = -1
  for i, msg in enumerate(messages):
    if isinstance(msg, dict) and msg.get("role") == "user":
      last_user = i

  tool_call_turns = _count_tool_calls_after_last_user(messages, last_user)
  if state and load_phase_state(state).tool_call_turns_since_user > 0:
    tool_call_turns = max(tool_call_turns, load_phase_state(state).tool_call_turns_since_user)
  plan = build_plan_state(state or SessionState(), query, messages=None) if state else build_plan_state(SessionState(), query, messages=None)
  ap = _agent_plan_for_phase(state)

  if state and state.agent_plan:
    try:
      from .evidence_extractors import evidence_types_satisfied
      from .planner import AgentPlan, can_final_answer

      ap_legacy = AgentPlan.from_dict(state.agent_plan)
      if can_final_answer(ap_legacy):
        pass
      elif ap_legacy.evidence_needed and not evidence_types_satisfied(
        ap_legacy.evidence_needed,
        ap_legacy.evidence_collected,
        task_intent=ap_legacy.task_intent,
      ):
        LOG.info("phase_result=tool_planning reason=agent_plan_evidence_incomplete")
        return "tool_planning"
    except ImportError:
      pass

  if plan and plan.task_kind == "compose_port":
    if plan.phase == "final_answer_ready":
      LOG.info("phase_result=final_answer reason=compose_port_evidence_ready")
      return "final_answer"
    LOG.info("phase_result=tool_planning reason=compose_port_evidence_missing")
    return "tool_planning"

  if plan and plan.task_kind == "validation":
    if plan.phase != "final_answer_ready":
      LOG.info("phase_result=tool_planning reason=validation_evidence_incomplete")
      return "tool_planning"
    if last_role == "tool":
      LOG.info("phase_result=final_answer reason=validation_evidence_complete")
      return "final_answer"

  if plan and plan.phase == "final_answer_ready" and last_role == "tool":
    if state and int(getattr(state, "final_answer_count", 0) or 0) >= 1:
      if not _cursor_looping_flagged(messages):
        LOG.info("phase_result=tool_planning reason=final_already_sent_plan_ready")
        return "tool_planning"
    if plan.task_kind in ("general", "project_inspection"):
      try:
        from .planner import AgentPlan, can_final_answer

        if state and state.agent_plan:
          ap = AgentPlan.from_dict(state.agent_plan)
          if not can_final_answer(ap):
            LOG.info("phase_result=tool_planning reason=exploration_not_done")
            return "tool_planning"
      except ImportError:
        if plan.task_kind == "general":
          LOG.info("phase_result=tool_planning reason=general_no_auto_final")
          return "tool_planning"
    LOG.info("phase_result=final_answer reason=plan_ready")
    return "final_answer"

  if last_role == "tool":
    if ap is not None:
      try:
        from .planner import can_final_answer

        router_intent = str(getattr(ap, "router_intent", "") or "")
        evidence_needed = list(getattr(ap, "evidence_needed", None) or [])
        read_only_cov = (
          router_intent == "read_only_analysis"
          or evidence_needed == ["target_coverage"]
        )
        if (
          not read_only_cov
          and can_final_answer(ap)
          and tool_call_turns >= _min_tool_calls_for_final(ap, intent_name)
        ):
          LOG.info("phase_result=final_answer reason=source_coverage_complete")
          return "final_answer"
      except ImportError:
        pass
    if plan and any(b.tool == "Read" for b in plan.blocked):
      if _read_only_source_registry_active(state):
        LOG.info("phase_result=tool_planning reason=blocked_read_loop_ignored_read_only")
      else:
        try:
          from .planner import AgentPlan, can_final_answer

          if state and state.agent_plan and can_final_answer(AgentPlan.from_dict(state.agent_plan)):
            pass
          else:
            LOG.info("phase_result=tool_planning reason=blocked_read_loop")
            return "tool_planning"
        except ImportError:
          LOG.info("phase_result=tool_planning reason=blocked_read_loop")
          return "tool_planning"
    if plan and plan.task_kind == "validation" and plan.phase != "final_answer_ready":
      LOG.info("phase_result=tool_planning reason=validation_incomplete")
      return "tool_planning"
    if plan and plan.needed_evidence and not evidence_satisfied(plan):
      LOG.info("phase_result=tool_planning reason=required_evidence_incomplete")
      return "tool_planning"
    if ap is not None:
      resolved = _resolve_with_agent_plan(
        state,
        ap,
        last_role=last_role,
        tool_call_turns=tool_call_turns,
        intent_name=intent_name,
        query=query,
      )
      if resolved:
        return resolved
    if tool_call_turns >= MIN_TOOL_CALLS_FOR_FINAL_ANSWER:
      if plan and plan.task_kind == "validation" and plan.phase != "final_answer_ready":
        return "tool_planning"
      if state and int(getattr(state, "final_answer_count", 0) or 0) >= 1:
        if not _cursor_looping_flagged(messages):
          LOG.info("phase_result=tool_planning reason=final_already_sent")
          return "tool_planning"
      try:
        from .planner import AgentPlan, can_final_answer

        if state and state.agent_plan:
          ap_check = AgentPlan.from_dict(state.agent_plan)
          if not can_final_answer(ap_check):
            LOG.info(
              "phase_result=tool_planning reason=tc_turns>=%d_but_evidence_incomplete",
              MIN_TOOL_CALLS_FOR_FINAL_ANSWER,
            )
            return "tool_planning"
      except ImportError:
        pass
      LOG.info("phase_result=final_answer reason=tc_turns>=%d", MIN_TOOL_CALLS_FOR_FINAL_ANSWER)
      return "final_answer"
    if not body.get("tools") and plan and plan.phase == "final_answer_ready":
      if state and int(getattr(state, "final_answer_count", 0) or 0) >= 1:
        if not _cursor_looping_flagged(messages):
          LOG.info("phase_result=tool_planning reason=final_already_sent_no_tools")
          return "tool_planning"
      LOG.info("phase_result=final_answer reason=no_tools_and_plan_ready")
      return "final_answer"
    LOG.info("phase_result=tool_planning reason=tc_turns(%d)<%d", tool_call_turns, MIN_TOOL_CALLS_FOR_FINAL_ANSWER)
    return "tool_planning"

  if tool_call_turns >= MIN_TOOL_CALLS_FOR_FINAL_ANSWER:
    for msg in messages[last_user + 1 :]:
      if not isinstance(msg, dict) or msg.get("role") != "tool":
        continue
      text = str(msg.get("content") or "")
      if len(text) > 40 and not text.startswith("[tool observation hash="):
        if plan and plan.task_kind == "validation" and plan.phase != "final_answer_ready":
          return "tool_planning"
        return "final_answer"

  return "tool_planning"


def compute_plan_phase_hint(state: SessionState, query: str, artifacts: list[Artifact]) -> str:
  plan = build_plan_state(state, query, artifacts)
  return plan.phase
