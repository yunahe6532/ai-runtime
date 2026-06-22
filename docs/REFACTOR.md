# AI Runtime — 3-Tier Refactor Plan

> **상태**: Phase 1 진행 중 (2026-06-22)  
> **목표**: rule/FSM 누적 프록시 → Runtime Kernel + AI Planner + Observability

---

## 3계층

```text
1. runtime_kernel/     Context · Memory · Budget · Retrieval · Coverage · RuntimeState
2. agent_brain/        AI가 RuntimeState를 보고 다음 action 결정 (Phase 2)
3. observability/      Trace SSOT — turn_log | langfuse | otel_only
```

레거시 `router/reference/`, `intent_router.py`, `dynamic_context_scheduler.py`는 **점진 이전** — 한 번에 삭제하지 않음.

---

## Phase 0 — 멈추고 정리 ✅

- 기능 추가 중단, SSOT 우선
- [ARCHITECTURE.md §12](./ARCHITECTURE.md) 감사 완료
- [VISION.md §12.15 gap](./ARCHITECTURE.md) 정리

---

## Phase 1 — SSOT 통일 (진행 중)

| 항목 | 상태 | 모듈 |
|------|:----:|------|
| Intent 통일 | ✅ | `runtime_kernel/intent.py` → `resolve_runtime_intent` |
| Phase 통일 | ✅ 스키마 | `runtime_kernel/phase.py` |
| Budget 상수 통일 | ✅ | `runtime_kernel/constants.py` |
| RuntimeState JSON | ✅ | `runtime_kernel/runtime_state.py` |
| Self Model YAML | ✅ | `config/runtime_self_model.yaml` |
| Trace SSOT | ✅ config | `observability/trace_ssot.py` (`TRACE_SSOT=turn_log`) |
| Planner action contract | ✅ 스키마 | `agent_brain/planner_contract.py` |
| `classify_intent` → SSOT | ✅ | `intent_router.py` |
| `context_need` → budget_profile | ✅ | `context_need.py` |
| `TOOL_PLANNING_MAX_TOKENS` 단일화 | ✅ | 800 default everywhere |
| `EXEC_INTENTS` 단일화 | ✅ | `runtime_kernel/constants.py` |
| Phase FSM 단일화 | ⬜ | `plan_state` + `planner` 이전 |
| Trace emitter 단일화 | ⬜ | adapters/observe 분기 |
| Legacy `_classify_intent_legacy` 제거 | ⬜ | regression 후 |

---

## Phase 2 — AI Planner 중심

```text
RuntimeState → AI Planner → PlannerDecision
  → Kernel executes (retrieve / tool / summarize / final)
  → Coverage check
  → repeat or exit
```

Rule은 **hard guard만**: loop_guard, response_guard, read_guard.

`PLANNER_MODE=llm`을 기본 경로로 승격 — `RuntimeState` + `self_model` block을 planner prompt에 주입.

---

## Phase 3 — Memory Summarization Loop

```text
if context_tokens > budget:
    summarize_old_turns()
    store_summary(session_summary / artifact_summary)
    replace_history_with_summary_pointer()
```

`memory_store/` tier: session_summary, artifact_summary, tool_result_cache, project_map.

---

## 하지 않을 것 (지금)

- retrieval 2-pass 제거 (Phase 1 SSOT 후)
- `main.py` split (Phase 2 후)
- reference/ 16-file merge (Phase 2 후)
- Langfuse full stack without TRACE_SSOT=langfuse decision

---

## GitHub

- Repo: `ai-runtime` (product name)
- Path: `cursor-local-llm/` = Cursor reference implementation

---

## 검증

```bash
cd router && python3 -c "from runtime_kernel import resolve_runtime_intent; print(resolve_runtime_intent('구조 분석').to_dict())"
./scripts/verify-router.sh   # when stack up
```
