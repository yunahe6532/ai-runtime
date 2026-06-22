"""Evidence extractors registry — tool results → evidence_collected tags."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Callable

from artifact_analyzer import extract_compose_port_evidence

EvidenceFn = Callable[[str, str, str], list[str]]

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _extract_compose_port(text: str, _path: str = "", _tool: str = "") -> list[str]:
  ev = extract_compose_port_evidence(text)
  if ev.get("router_port") or ev.get("port_evidence"):
    port = ev.get("router_port") or ev.get("host_port", "?")
    return [f"compose_port:{port}"]
  return []


def _extract_benchmark_agent(text: str, path: str = "", _tool: str = "") -> list[str]:
  if "benchmark-cursor-agent" not in path and "benchmark-cursor-agent" not in text[:500]:
    return []
  try:
    data = json.loads(text) if text.strip().startswith("{") else None
  except json.JSONDecodeError:
    data = None
  if data and "runs" in data:
    last = data["runs"][-1].get("summary", {})
    return [f"agent_benchmark:pass_rate={last.get('pass_rate')}"]
  if "pass_rate" in text or "tool_match_rate" in text:
    return ["agent_benchmark:seen"]
  return []


def _extract_runtime_score(text: str, path: str = "", _tool: str = "") -> list[str]:
  if "benchmark-runtime-score" not in path and "runtime_score" not in text[:800]:
    return []
  try:
    data = json.loads(text) if text.strip().startswith("{") else None
  except json.JSONDecodeError:
    data = None
  if data and "runs" in data:
    rs = data["runs"][-1].get("runtime_score", {}).get("ai_runtime", {})
    return [f"runtime_score:success={rs.get('agent_success_rate_pct')}"]
  if "agent_success_rate" in text or "Tasks passed" in text:
    return ["runtime_score:seen"]
  return []


def _extract_flow_phase(text: str, path: str = "", _tool: str = "") -> list[str]:
  tags: list[str] = []
  if ".flow.json" in path or '"stage"' in text[:400]:
    try:
      data = json.loads(text) if text.strip().startswith("{") else None
    except json.JSONDecodeError:
      data = None
    if data:
      stages = data.get("stages") or []
      proxy = next((s for s in stages if s.get("stage") == "2_router_proxy"), {})
      tags.extend([
        f"flow_phase:{proxy.get('phase', '?')}",
        f"flow_task_kind:{proxy.get('intent', '?')}",
        "flow_phase_seen",
      ])
      phases = [s.get("phase") for s in stages if s.get("phase")]
      if phases:
        tags.append(f"phase_distribution_seen:{','.join(str(p) for p in phases[:6])}")
      if data.get("elapsed_sec", 0) and float(data.get("elapsed_sec", 0)) > 30:
        tags.append("bottleneck_seen:slow_llm_turn")
  if "final_answer" in text and "tool_planning" in text:
    tags.append("loop_pattern_seen:final_tool_mix")
  if "phase_result=final_answer" in text or "agent_plan_evidence_ready" in text:
    tags.append("loop_pattern_seen:premature_final")
  if "xml_tool_leak" in text.lower() or "<tool_call>" in text[:500]:
    tags.append("xml_leak_seen")
  if "tools_stripped" in text:
    tags.append("tools_stripped_seen")
  return list(dict.fromkeys(tags))


def _extract_runtime_score_seen(text: str, path: str = "", _tool: str = "") -> list[str]:
  base = _extract_runtime_score(text, path)
  if base:
    return base + ["runtime_score_seen"]
  return []


def _extract_agent_benchmark_seen(text: str, path: str = "", _tool: str = "") -> list[str]:
  base = _extract_benchmark_agent(text, path)
  if base:
    return base + ["agent_benchmark_seen"]
  return []


def _extract_fix_strategy(text: str, path: str = "", _tool: str = "") -> list[str]:
  text_l = (text or "").lower()
  if any(k in text_l for k in ("validate_decision", "evidence_judge", "loop_guard", "xml leak", "final_answer_count")):
    return ["fix_strategy_seen"]
  if "plan_state" in path or "agent_exec" in path or "evidence_judge" in path:
    if len(text) > 200:
      return ["fix_strategy_seen", "code_location_seen"]
  return []


def _extract_code_location(text: str, path: str = "", tool: str = "") -> list[str]:
  if not path and not text:
    return []
  if any(p in (path or "") for p in ("plan_state.py", "agent_exec.py", "planner.py", "main.py")):
    return ["code_location_seen"]
  if tool == "Read" and path.endswith(".py") and len(text) > 80:
    return [f"code_location_seen:{Path(path).name}"]
  return []


def _extract_artifact_seen(text: str, path: str = "", tool: str = "") -> list[str]:
  if text.strip() and len(text) > 40 and tool in ("Read", "Grep", "Shell"):
    return ["artifact_seen"]
  return []


def _extract_bottleneck(text: str, path: str = "", _tool: str = "") -> list[str]:
  text_l = text.lower()
  tags: list[str] = []
  if "elapsed_sec" in text_l or "saved_pct" in text_l or "pack_tokens" in text_l:
    tags.append("bottleneck_seen:flow_metrics")
  if "48" in text and "turn" in text_l:
    tags.append("loop_pattern_seen:high_turn_count")
  if "ping-pong" in text_l or "pingpong" in text_l:
    tags.append("loop_pattern_seen:ping_pong")
  return tags


def _extract_plan_state_logic(text: str, path: str = "", _tool: str = "") -> list[str]:
  if "plan_state" not in path and "VALIDATION_QUERY_KW" not in text:
    return []
  if "VALIDATION_QUERY_KW" in text or "AgentPlan" in text or "resolve_agent_phase" in text:
    return ["plan_state_logic:seen"]
  return []


PROJECT_TREE_DIRS = ("router", "ui", "scripts", "docs", "configs", "docker-compose.yml")
CORE_FILE_NAMES = (
  "main.py",
  "memory_store.py",
  "planner.py",
  "agent_runs.py",
  "ARCHITECTURE.md",
  "README.md",
  "handoff.md",
)

EXPLORATION_INTENTS = frozenset({
  "exploration",
  "project_inspection",
  "repo_summary",
  "code_read",
})

PROJECT_INSPECTION_KW = (
  "프로젝트 구조",
  "구조 파악",
  "구현사항",
  "구현 사항",
  "architecture",
  "repo structure",
  "project structure",
  "어떤식으로",
  "디렉터리",
  "directory layout",
  "코드베이스",
)


def looks_like_project_inspection(query: str) -> bool:
  q = (query or "").lower()
  return any(k.lower() in q for k in PROJECT_INSPECTION_KW)


def is_exploration_intent(task_intent: str) -> bool:
  return (task_intent or "general") in EXPLORATION_INTENTS


def _extract_project_tree(text: str, path: str = "", tool: str = "") -> list[str]:
  if not text.strip():
    return []
  text_l = text.lower()
  if "path does not exist" in text_l or "0 files" in text_l:
    return []
  hits = [d for d in PROJECT_TREE_DIRS if d in text_l]
  if len(hits) >= 2:
    return [f"project_tree_seen:{','.join(hits[:8])}"]
  if tool == "Shell" and ("exit code: 0" in text_l or "total " in text_l):
    if any(d in text_l for d in ("router", "scripts", "docs", "ui", "configs")):
      return [f"project_tree_seen:shell_ls"]
    if "ai-runtime" in text_l or "cursor-local-llm" in text_l:
      return ["project_tree_seen:runtime_root"]
  if tool == "Read" and path.endswith((".md", ".json")) and len(text) > 80:
    parent = Path(path).parent.name
    if parent in PROJECT_TREE_DIRS:
      return [f"project_tree_seen:read_{parent}"]
  return []


def _extract_core_files(text: str, path: str = "", tool: str = "") -> list[str]:
  if not text.strip() or len(text) < 40:
    return []
  base = Path(path).name if path else ""
  if base in CORE_FILE_NAMES or base.upper().startswith("README"):
    if base.upper().startswith("README"):
      return ["readme_seen", "core_files_seen"]
    if "ARCHITECTURE" in base.upper():
      return ["architecture_doc_seen", "core_files_seen"]
    return [f"core_files_seen:{base}"]
  text_l = text.lower()
  found = [name for name in CORE_FILE_NAMES if name.lower() in text_l]
  if len(found) >= 2:
    return [f"core_files_seen:{','.join(found[:6])}"]
  if tool == "Read" and any(name.lower() in text_l for name in ("agentplan", "memory_store", "agent_runs")):
    return ["core_files_seen:router_modules"]
  return []


EVIDENCE_EXTRACTORS: dict[str, EvidenceFn] = {
  "compose_port": _extract_compose_port,
  "benchmark_agent_result": _extract_agent_benchmark_seen,
  "runtime_score_result": _extract_runtime_score_seen,
  "flow_phase": _extract_flow_phase,
  "plan_state_logic": _extract_plan_state_logic,
  "project_tree": _extract_project_tree,
  "core_files": _extract_core_files,
  "code_location": _extract_code_location,
  "fix_strategy": _extract_fix_strategy,
  "artifact_seen": _extract_artifact_seen,
  "bottleneck": _extract_bottleneck,
}


def collect_evidence_from_tool_result(
  text: str,
  *,
  path: str = "",
  tool_name: str = "",
) -> list[str]:
  found: list[str] = []
  for _name, fn in EVIDENCE_EXTRACTORS.items():
    try:
      tags = fn(text, path, tool_name)
      found.extend(tags)
    except Exception:
      continue
  return list(dict.fromkeys(found))


def exploration_evidence_done(collected: list[str]) -> bool:
  coll = " ".join(collected).lower()
  has_tree = "project_tree_seen" in coll
  has_core = any(
    key in coll
    for key in ("core_files_seen", "readme_seen", "architecture_doc_seen")
  )
  return has_tree and has_core


def evidence_types_satisfied(
  needed: list[str],
  collected: list[str],
  *,
  task_intent: str = "",
) -> bool:
  if not needed:
    if is_exploration_intent(task_intent):
      return exploration_evidence_done(collected)
    if task_intent in {"compose_port"}:
      return True
    if task_intent in {"general", ""}:
      return any("artifact_seen" in c for c in collected)
    return False
  coll = " ".join(collected).lower()
  for item in needed:
    key = item.split(":", 1)[0].replace(" ", "_").lower()
    label = item.lower()
    if key == "target_coverage":
      if any(str(c).startswith("source_hit:") for c in collected):
        continue
      return False
    if key in coll or label in coll or any(label in c for c in collected):
      continue
    if not any(label.split()[0] in c for c in collected):
      return False
  return True
