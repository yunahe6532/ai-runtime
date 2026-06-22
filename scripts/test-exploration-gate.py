#!/usr/bin/env python3
"""P0 exploration / premature-final gate tests."""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "router"))

from reference.agent_exec import detect_agent_phase, guard_final_answer_content, parse_function_xml, postprocess_agent_response
from reference.evidence_extractors import (
  collect_evidence_from_tool_result,
  evidence_types_satisfied,
  exploration_evidence_done,
  looks_like_project_inspection,
)
from adapters.memory import SessionState
from reference.planner import build_rule_plan, can_final_answer, normalize_plan, AgentPlan


def test_project_inspection_plan():
  state = SessionState()
  q = "지금 프로젝트 구조 어떤식으로 되어있는지 파악하고 구현사항 알려줘봐"
  assert looks_like_project_inspection(q)
  plan = build_rule_plan(q, state)
  assert plan.task_intent == "project_inspection"
  assert "project_tree_seen" in plan.evidence_needed[0] or "project_tree_seen" in str(plan.evidence_needed)
  assert not can_final_answer(plan)
  plan.evidence_collected = ["project_tree_seen:router,ui", "core_files_seen:main.py"]
  assert can_final_answer(plan)
  print("project_inspection_plan: OK")


def test_empty_evidence_not_final_for_general():
  assert evidence_types_satisfied([], [], task_intent="general") is False
  assert evidence_types_satisfied([], ["artifact_seen"], task_intent="general") is True
  assert evidence_types_satisfied([], [], task_intent="project_inspection") is False
  assert exploration_evidence_done(["project_tree_seen:x", "readme_seen"]) is True
  print("empty_evidence_gate: OK")


def test_project_tree_extractor():
  ls_out = "Exit code: 0\n\ndrwx router\ndrwx ui\ndrwx scripts\ndrwx docs"
  tags = collect_evidence_from_tool_result(ls_out, tool_name="Shell")
  assert any("project_tree_seen" in t for t in tags)
  print("project_tree_extractor: OK")


def test_three_tool_calls_stays_planning_without_evidence():
  os.environ["MIN_TOOL_CALLS_FOR_FINAL_ANSWER"] = "3"
  q = "프로젝트 구조 파악하고 구현사항 알려줘"
  state = SessionState()
  plan = build_rule_plan(q, state)
  plan = normalize_plan(plan, q)
  state.agent_plan = plan.to_dict()
  msgs = [{"role": "user", "content": q}]
  for _ in range(3):
    msgs.append({"role": "assistant", "content": "", "tool_calls": [{"id": "x", "type": "function", "function": {"name": "Shell", "arguments": "{}"}}]})
    msgs.append({"role": "tool", "content": "Exit code: 0\n\n```\nrouter\nui\n```"})
  phase = detect_agent_phase({"messages": msgs, "tools": [{}]}, "code_edit", True, state, q)
  assert phase == "tool_planning", f"expected tool_planning got {phase}"
  print("three_tc_stays_planning: OK")


def test_final_xml_leak_promotes_tool_planning():
  xml = (
    '<tool_call>\n<function=Shell>\n<parameter=command>\n'
    "ls -la /home/yunahe/ai-runtime/cursor-local-llm\n"
    "</parameter>\n</function>\n</tool_call>"
  )
  resp = {
    "choices": [{
      "message": {"role": "assistant", "content": xml},
      "finish_reason": "stop",
    }]
  }
  out, log = postprocess_agent_response(resp, "code_edit", "프로젝트 구조", phase="final_answer")
  assert out["choices"][0].get("finish_reason") == "tool_calls" or out["choices"][0]["message"].get("tool_calls")
  assert log.synthetic_tool_call
  print("final_xml_promote: OK")


if __name__ == "__main__":
  test_project_inspection_plan()
  test_empty_evidence_not_final_for_general()
  test_project_tree_extractor()
  test_three_tool_calls_stays_planning_without_evidence()
  test_final_xml_leak_promotes_tool_planning()
  print("all exploration gate tests passed")
