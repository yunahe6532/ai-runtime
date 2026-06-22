# AI Runtime — Refactor Plan (Local LLM Runtime)

> **상태**: Phase 2.1 — LLM Planner Shadow (2026-06-22)  
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
| `runtime_kernel/task_journal.py` | Journal · Handoff render · normalized events |
| `runtime_kernel/evidence_anchor.py` | path/symbol/line/hash anchor |
| `runtime_kernel/evidence_ingest.py` | Read/Grep/Shell/Edit → anchor + journal wire |
| `runtime_kernel/final_report.py` | deterministic final markdown renderer |
| `dynamic_context_scheduler.py` | WS → single retrieve → pre-pack → prompt |
| `SessionState` | project_index · task_journal · handoff · evidence_anchors |

env: `PROJECT_INDEX_BOOTSTRAP=1`

ingest wire: `memory_store.update_state_from_delta` → `ingest_artifacts_evidence`  
final report: `response_guard.build_partial_final_prose` → `render_final_report`

---

## Phase 1.75 — Evidence path complete ✅ (2026-06-22)

1. EvidenceAnchor ingest wire (Read/Grep/Shell/Edit/tool)
2. Task Journal event normalization (`JournalKind`, `record_*`)
3. Final Report Renderer (deterministic markdown, LLM polish optional)

**AI Planner 승격은 위 완료 후** — 입력: RuntimeState · ProjectIndex · WorkingSet · TaskJournal · EvidenceAnchors · SelfModel · CurrentUserRequest

---

## Phase 1.8 — Evidence/Journal/Report 검증 고정 ✅ (2026-06-22)

| 항목 | 내용 |
|------|------|
| E2E | `scripts/test-evidence-journal-report-e2e.py` |
| Size caps | `memory_limits.py` — journal/anchor/handoff prune |
| Final report flag | `response_guard` → `final_report_used=true` log |
| Inspector | journal/anchor/handoff/final_report 상태 표시 |

env caps (optional override):

| 변수 | default |
|------|---------|
| `MAX_JOURNAL_EVENTS` | 200 |
| `MAX_ANCHORS_TOTAL` | 300 |
| `MAX_ANCHORS_PER_FILE` | 12 |
| `MAX_ANCHOR_SUMMARY_CHARS` | 800 |
| `MAX_HANDOFF_CHARS` | 16000 |

Phase 2 착수 조건:

- [x] Evidence ingest E2E 통과
- [x] Final report deterministic output 확인
- [x] recovery / unit 벤치 회귀 없음 (`verify-router.sh`)
- [x] Journal/Anchor size cap
- [x] Inspector journal/anchor/report 상태

---

## Phase 2 — AI Planner (next)

### Phase 2.0 — RuntimeState Contract ✅ (2026-06-22)

| 모듈 | 역할 |
|------|------|
| `agent_brain/runtime_state.py` | Planner `RuntimeState` + `RuntimeStateBuilder` |
| `agent_brain/planner_contract.py` | `PlannerDecision` schema (read/grep/…/final) |
| `agent_brain/planner_shadow.py` | Shadow mode — compare rule vs candidate, no hot path |

Shadow hook: `dynamic_context_scheduler.build_context_for_turn`  
env: `PLANNER_SHADOW_MODE=1` (default on), `MAX_RUNTIME_STATE_PROMPT_CHARS=8000`

Phase 2.1 ✅: LLM planner shadow — `agent_brain/llm_planner.py`, 3-way compare

Phase 2.2 (next): read/grep/glob 부분 승격 (edit/shell/final은 hard guard 유지)

---

### Phase 2.1 — LLM Planner Shadow ✅ (2026-06-22)

| 모듈 | 역할 |
|------|------|
| `agent_brain/llm_planner.py` | `propose_llm_shadow_decision()` — RuntimeState JSON → PlannerDecision |
| `planner_shadow.py` | rule / heuristic / LLM `compare_triple_decisions()` |

env (`LLM_PLANNER_SHADOW_ENABLED=0` default — shadow only):

| 변수 | default |
|------|---------|
| `LLM_PLANNER_SHADOW_ENABLED` | `0` |
| `LLM_PLANNER_TIMEOUT_SEC` | `15` |
| `LLM_PLANNER_MAX_TOKENS` | `512` |

trace: `planner.llm.proposed`, `planner.triple_compared`

```bash
python3 scripts/test-llm-planner-shadow-e2e.py
```

---

### Phase 2.05 — Planner/Explorer Trace Observability ✅ (2026-06-22)

| 항목 | 내용 |
|------|------|
| SSOT | `write_explorer_trace()` — common schema + flush |
| Host path | `tmp/cursor-captures/explorer-trace.ndjson` (docker `/captures/`) |
| CLI | `tail-explorer-flow.py --from-start` (replay+exit), `--diagnose` |
| Events | `planner.*`, `memory.*`, `working_set.*`, `coverage.*`, `final_report.*` |
| Shadow | `would_change_hot_path`, `target_overlap`, `confidence_delta` |

```bash
python3 scripts/test-explorer-trace-e2e.py
python3 scripts/tail-explorer-flow.py tmp/cursor-captures/explorer-trace.ndjson --from-start
```

---

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
python3 scripts/test-planner-runtime-state-e2e.py
python3 scripts/test-llm-planner-shadow-e2e.py
python3 scripts/test-explorer-trace-e2e.py
python3 scripts/test-evidence-journal-report-e2e.py
python3 scripts/benchmark-recovery-e2e.py
./scripts/verify-router.sh
# live stack 필요:
python3 scripts/benchmark-runtime-score.py --tasks 30
```

GitHub: https://github.com/yunahe6532/ai-runtime
