# AI Runtime — Refactor Plan (Local LLM Runtime)

> **상태**: Phase 1.5 — Memory Scheduler (2026-06-22)  
> **철학**: [VISION.md](./VISION.md) — Context 압축기 ❌ · Local LLM Runtime ✅

---

## 3계층

```text
1. runtime_kernel/     Project Index · Working Set · Journal · Budget · Coverage
2. agent_brain/        RuntimeState → AI PlannerDecision (Phase 2)
3. observability/      TRACE_SSOT=turn_log
```

---

## Phase 1 — SSOT ✅

Intent · Phase · Constants · RuntimeState · Self Model · Trace config

---

## Phase 1.5 — Memory Scheduler ✅ (2026-06-22)

| 모듈 | 역할 |
|------|------|
| `runtime_kernel/project_index.py` | Bootstrap · fingerprint · git invalidation |
| `runtime_kernel/working_set.py` | Hot path planner · pre-pack constraints |
| `runtime_kernel/task_journal.py` | Journal · Handoff render |
| `runtime_kernel/evidence_anchor.py` | path/symbol/line/hash anchor |
| `dynamic_context_scheduler.py` | WS → single retrieve → pre-pack → prompt |
| `SessionState` | project_index · task_journal · handoff · evidence_anchors |

env: `PROJECT_INDEX_BOOTSTRAP=1`

---

## Phase 2 — AI Planner (next)

```text
RuntimeState + Self Model + Journal tail
    → LLM PlannerDecision (retrieve_more | call_tool | summarize | final)
    → Kernel executes
    → hard guard only (loop_guard)
```

`reference/` → hard guard + tool exec만 hot path 유지

---

## Phase 3 — Memory Summarization

```text
if raw_tokens > budget:
    summarize_old_turns → session_summary artifact
    replace history with summary pointer
```

---

## Phase 4 — Final Report Renderer

```text
Task Journal + Patch Log + Handoff → markdown report
LLM optional polish only
```

---

## 검증

```bash
cd router && python3 -c "
from runtime_kernel.project_index import bootstrap_project_index
from runtime_kernel.working_set import plan_working_set
from context_need import ContextNeed
idx = bootstrap_project_index('.', 'test')
print('index files', idx.file_count)
"
```

GitHub: https://github.com/yunahe6532/ai-runtime
