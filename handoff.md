# Handoff

> **Product**: AI Runtime — Memory & Task Operating System · [VISION.md](./docs/VISION.md)

## 2026-06-22 — Runtime Reachability Audit (Phase 2.2a-deep)

### 신규
- `scripts/audit-runtime-reachability.py` — `--static`, `--profile`, `--merge`

### 산출물
- `docs/reports/runtime-reachability.md`
- `docs/reports/deprecated-branches.md`
- `docs/reports/deprecated-env.md`
- `docs/reports/archive-candidates.md`
- `tmp/runtime-reachability.json`

### 핵심 발견
- `CONTEXT_OPTIMIZER` / `RUNTIME_OPTIMIZER` → **imported_but_dead_branch** (env=1이지만 entrypoint에서 import 경로 없음)
- D-tier archive 후보: **0건** (strict filter — 삭제/이동 보류)
- `dead_candidate`: 3 (non-legacy peripheral)

### 검증
- 회귀 테스트 전부 PASS
- benchmark 30/30 PASS

### 다음
- Phase 2.2a-clean: runtime artifact move + legacy archive (reachability 기준 적용)

---

## 2026-06-22 — Repo Hygiene & Dead Code Audit (Phase 2.2a hygiene)

### 변경 파일
- `router/runtime_kernel/runtime_paths.py` (신규) — `AI_RUNTIME_DATA_DIR` SSOT
- `router/runtime_kernel/project_index.py` — `ProjectIndexConfig`, `classify_path`, ignore policy v2
- `router/explorer_trace.py`, `router/legacy/memory_store.py` — runtime paths
- `scripts/audit-repo-inventory.py`, `scripts/audit-dead-code.py`, `scripts/generate-project-structure.py` (신규)
- `scripts/test-project-index-ignore-e2e.py` (신규)
- `.gitignore`, `.dockerignore`, `router/.dockerignore`, `.env.example`, `docker-compose.yml`
- `docs/PROJECT_STRUCTURE.md`, `docs/reports/*` (audit reports)
- `docs/FILE_TREE.md` → `docs/reports/FILE_TREE.full.md` (이동)

### 완료
- Repo inventory / dead code / legacy archive plan 리포트 생성 (삭제 없음)
- Runtime storage: `~/.local/share/ai-runtime` 기본, repo `tmp/` fallback
- Project Index: vendor/tmp/cache/FILE_TREE 제외

### 검증
- `test-project-index-ignore-e2e.py` PASS
- 기존 planner/trace/recovery/ping-pong PASS
- `benchmark-runtime-score.py --tasks 30` (재실행 예정)

### 다음 작업
- Phase 2.2b hot path 승격 (read/grep/glob) — hygiene 완료 후
- 선택: 오래된 `tmp/` artifact를 `AI_RUNTIME_DATA_DIR`로 archive 이동

---

## 2026-06-22 — Phase 2.2a Planner Promotion Gate

### 변경 파일
- `router/agent_brain/promotion_gate.py` (신규)
- `router/agent_brain/planner_shadow.py` — promotion gate hook + trace
- `router/explorer_trace.py`, `runtime_inspector.py`, `runtime_turn_log.py`, `legacy/memory_store.py`
- `scripts/test-planner-promotion-gate-e2e.py` (신규)
- `docs/REFACTOR.md`, `scripts/verify-router.sh`

### 완료
- `evaluate_promotion()` — read/grep/glob 승격 가능 여부 판정 (shadow-only, hot path 미변경)
- dry_run_tool_call 생성 + trace `planner.promotion.*` 이벤트
- Inspector Promotion Gate 섹션, turn_log `planner_promotion_decision`
- metrics: eligible_rate, blocked_by_*, would_change_hot_path_rate

### env
- `PLANNER_PROMOTION_SHADOW_ONLY=1` (default)
- `PLANNER_PROMOTION_MIN_CONFIDENCE=0.75`
- `PLANNER_PROMOTION_MIN_TARGET_OVERLAP=0.5`

### 검증
- `test-planner-promotion-gate-e2e.py` 13/13 PASS
- `benchmark-runtime-score.py --tasks 30` 30/30 PASS (재실행)

### 다음 작업
- **Phase 2.2b**: read/grep/glob 실제 hot path 부분 승격 (`PLANNER_PROMOTION_SHADOW_ONLY=0` + guard)

---

## 2026-06-22 — Phase 2.1 LLM Planner Shadow (pushed `81c4c4ae`)

### 변경 파일
- `router/agent_brain/llm_planner.py` (신규)
- `router/agent_brain/planner_shadow.py` — `compare_triple_decisions`, LLM shadow hook
- `router/explorer_trace.py` — `EXPLORER_TRACE_PATH` env 우선, `planner.llm.proposed` / `planner.triple_compared`
- `router/runtime_inspector.py` — Rule / Heuristic / LLM 3자 비교
- `router/runtime_turn_log.py` — `planner_llm_shadow`
- `router/legacy/memory_store.py` — `last_planner_llm_shadow`
- `scripts/test-llm-planner-shadow-e2e.py` (신규)
- `docs/REFACTOR.md`

### 완료
- LLM 기반 `PlannerDecision` shadow (`propose_llm_shadow_decision`) — hot path 미변경
- rule / heuristic / LLM 3자 비교 (action_match, target/evidence overlap, confidence_delta, risk_flags, would_change_hot_path)
- trace: `planner.llm.proposed`, `planner.triple_compared`
- 실패 시 recover/ask_user fallback — hot path 영향 없음

### env (default off)
- `LLM_PLANNER_SHADOW_ENABLED=0`
- `LLM_PLANNER_TIMEOUT_SEC=15`
- `LLM_PLANNER_MAX_TOKENS=512`

### 검증
- `test-llm-planner-shadow-e2e.py` 7/7 PASS
- `test-planner-runtime-state-e2e.py` 7/7 PASS
- `test-explorer-trace-e2e.py` 6/6 PASS
- `benchmark-recovery-e2e.py` PASS
- `test-ping-pong-gate.py` PASS
- `benchmark-runtime-score.py --tasks 30` **30/30**

### 다음 작업
- **Phase 2.2**: `read/grep/glob`만 부분 승격, `edit/shell/final`은 hard guard 유지
- live shadow: `LLM_PLANNER_SHADOW_ENABLED=1 docker compose up -d router`

---


### 변경
- OS/GPU Runtime 표현 약화 → Runtime Middleware · Prompt Residency Policy
- §1 문제 정의: Local LLM + Agent IDE Runtime 격차
- Implemented / In Progress / Planned 전역 표기
- §7 기존 기술 한계 (LiteLLM · LlamaIndex · LangGraph · Cursor)
- Roadmap v1–v4 현실화 · Build vs Buy 명확화

### 검증
- 문서만 변경 (코드 무관)

---

### 증상
- read_only 구조 질문이 `"요청하신 검증 결과를 artifact 분석 기준으로… (차단된 Read 대신 Shell 검증)"` 로 끝남
- validation HTML 템플릿이 read_only에 leak · partial_final로 조기 종료

### 원인
- `build_evidence_answer` / `build_final_answer_from_plan`이 `file_analyses`만 있어도 validation prose 생성
- `agent_exec` blocked Read → validation stub + Shell hint 주입
- read_only → `partial_final_answer` 탈출 경로

### 변경
- read_only final = **LLM + PromptPack evidence** only (`build_evidence_answer` read_only → "")
- `build_final_answer_from_plan` = validation task_kind 전용
- read_only **never** `partial_final_answer` — coverage 미완이면 `tool_planning` + explorer inject
- blocked Read 후 tool 없음 → explorer synthetic tool (stub prose 제거)

### 검증
- regression 26/26

---

## 2026-06-18 — LLM-first read-only exploration (playbook 제거)

### 방향
- 하드코딩 playbook(tier_grep → glob → boundary 순서 강제) **제거**
- **fast LLM explorer**가 Cursor처럼 다음 tool 선택 (Read/Glob/Grep, pattern, source_id)
- final 게이트만 evidence depth 유지: summary docs hit + dir별 **content** 이상 (`READ_ONLY_MIN_DIR_STAGE=content`)
- LLM `allow_final`은 `exploration_depth_sufficient`일 때만 수용; 아니면 LLM 제안 유지·탐색 계속
- LLM 502 시 `_minimal_fallback_decision` — 첫 gap(doc unread / dir inventory / wide grep)만, 고정 playbook 없음
- `wide_grep_pattern`: LLM이 준 pattern 그대로 사용 (빈 값만 `.`)

### env
- `READ_ONLY_EXPLORER_ENABLED=1` · `READ_ONLY_EXPLORER_OVERRIDE=1` · `READ_ONLY_EXPLORER_MAX_TOKENS=1536`
- `READ_ONLY_MIN_DIR_STAGE=content`

### 검증
- regression 26/26 · verify-router ALL PASS (재실행)

---

## 2026-06-18 — Cursor형 read-only playbook (docs→tier grep→glob→content→boundary) [superseded]

### 증상
- 로컬 LLM read_only가 `runtime_core/adapters/legacy/integrations` **Glob 4회** 후 즉시 final → 수박껍질 답변
- Cursor 에이전트는 MODULE_MAP/ARCHITECTURE Read → tier Grep → dir별 Glob/Grep/import 경계 → 21 files / 12 searches

### 변경
- `read_only_explorer.py` — **deterministic playbook** + LLM planner 프롬프트 동기화
  - 순서: summary docs Read → `dir.router` tier Grep (`runtime_core|adapters|legacy|integrations`) → dir별 Glob `*.py` → content Grep (`class|def|"""`) → boundary Grep (`import|from`) → optional init Grep
  - `exploration_milestones`: `doc_read:*`, `tier_grep`, `dir_content:*`, `dir_boundary:*`, `dir_init:*`
  - `exploration_checklist_passes` / `can_final_answer`: **boundary stage** (`READ_ONLY_MIN_DIR_STAGE=boundary`) + tier_grep 필수
- `source_registry.wide_grep_pattern` — playbook 패턴만 통과 (`is_playbook_grep_pattern`); 임의 LLM 패턴은 `.`로 강제
- `note_exploration_from_hit` — pattern별 milestone/stage 갱신 (tier/content/boundary/init 분리)
- `planner.py` — read_only relpaths에 `router` 부모 dir 주입 (tier grep 대상)
- regression helpers — `_complete_playbook_coverage` 등 full checklist 시뮬레이션

### env
- `READ_ONLY_MIN_DIR_STAGE=boundary` (default)
- `READ_ONLY_EXPLORER_ENABLED=1` · `READ_ONLY_EXPLORER_OVERRIDE=1` · `READ_ONLY_STATIC_FALLBACK=1`

### 검증
- regression **26/26** · `test-final-evidence-pack.py` 4/4 · `./scripts/verify-router.sh` ALL PASS

### 남은 gap
- playbook step 4는 Grep `__init__` 위주 — Cursor처럼 anchor file **ReadSource** 자동 선택은 미구현 (LLM planner가 선택 가능)
- E2E 동일 프롬프트로 ~15–20 tool turn · final evidence 수천 토큰 주입은 **라이브 LLM/compose 재기동 후** 재확인 필요

---

## 2026-06-21 — LLM read-only exploration planner (Glob→Grep→Read)

### 증상
- read_only가 `pick_next_read_only_tool` 정적 Glob만 반복 → cov 1.0 = 파일명 목록 → final 품질 C+
- LLM planning/judge 미사용 (read_only에서 evidence_judge bypass)

### 변경
- `reference/read_only_explorer.py` — **fast LLM** 탐색 planning (`thinking`, `next_tool`, `allow_final`)
- `source_exploration_stage`: none → inventory(Glob) → content(Grep) → anchor(Read)
- `source_coverage_passes` / `can_final_answer`: dir는 **content** 이상만 통과 (Glob alone 불가)
- `agent_exec`: 정적 `pick_next_read_only_tool` 제거 → explorer synthetic tool (override 기본 on)
- `plan_state`: 매 턴 `refresh_read_only_exploration_plan` → next_action 갱신
- static fallback ladder: Glob → Grep docstrings → Grep imports

### env
- `READ_ONLY_EXPLORER_ENABLED=1` (default)
- `READ_ONLY_EXPLORER_OVERRIDE=1` — fast planner tool 주입
- `READ_ONLY_STATIC_FALLBACK=1` — LLM 실패 시 ladder

### 검증
- regression 26/26 · verify-router ALL PASS

---

## 2026-06-21 — final_answer 예산 풀링 + 세션 artifact 전량 주입

### 증상
- `retrieved budget=4495`인데 `retrieval_total_tokens=290`, `prompt_pack_tokens=1577` — 예산 90% 미사용
- final_answer 품질 C+ (파일명 추측), tool_planning 잡음(plan/state) 혼입

### 원인 (설계 버그)
1. **`_pack_final_answer_evidence`가 현재 delta `artifacts`만 사용** — final_answer 턴에는 delta artifact=0 → evidence 블록 비어 있음 → 290토큰 retrieval만 주입
2. **`artifact_prompt_text` rebuild 조건** `raw_len>1500` — Glob 282 chars는 ingest excerpt 재사용, final LLM 1-pass 미실행
3. **예산 슬롯 분산** — retrieved 4495만 evidence cap; plan/state/delta가 sys_cap과 경쟁해 `[collected_evidence]` 잘림

### 변경
- `prompt_builder.py`: `_load_session_evidence_artifacts` — `state.artifacts` 전량 로드
- `_final_answer_evidence_budget` — retrieved+artifact+session_tail+plan+state+delta 풀 (~75% input window)
- final_answer 프롬프트: plan/state/legacy/tool tail 제거 → system+task+collected_evidence만
- `artifact_excerpt.py`: final phase면 raw 있으면 **항상** `rebuild_prompt_excerpt_for_budget` (LLM force)
- `context_budget.py`: final_answer evidence-first 비율 (retrieved+session_tail+artifact ≈ 82%)
- `dynamic_context_scheduler.py` / `retriever.py`: final retrieval·per-target floor 확대

### 검증
- `scripts/test-final-evidence-pack.py` 4/4 OK
- read-only regression 26/26 · `./scripts/verify-router.sh` ALL PASS

### 남은 gap
- Glob-only 수집(282 chars/dir)은 LLM summarize 성공 시 품질↑ — **GrepSource `.` 또는 ReadSource** 턴 추가 시 근거 깊이 확보

---

## 2026-06-21 — bad_ping_pong + Grep→Glob 디렉터리 수집

### 증상
- `GrepSource pattern=.` 3회 반복 → shallow(1 file) → cov 0.62 고정 → `partial_final_answer blocked:bad_ping_pong`

### 원인
- Cursor Grep은 dir+`.` 해도 **한 파일의 매칭 줄만** 반환 (adapters gateway.py 15KB/1file)
- shallow gate(min=5) 거부 → source_hit 없음 → 같은 action 반복 → `SAME_ACTION_REPEAT_LIMIT=2` ping-pong
- coverage 미완인데 evidence>0 이면 partial_final로 탈출 (잘못된 종료)

### 변경
- dir 수집: runtime **`GlobSource *.py` 우선** (Grep은 파일 내용용)
- `glob_workspace_file_count` + Glob shallow gate
- read_only + coverage incomplete + ping_pong → **tool_planning 유지** (partial_final 금지)
- `build_glob_excerpt` + ingest LLM chunk 요약

### 검증
- regression 21/21 · router 재배포

---

## 2026-06-21 — wide Grep 강제 + shallow hit 거부 + ingest LLM chunk 요약

### 증상
- LLM이 docstring Grep만 실행 → adapters 1/8 파일만 수집
- cov 1.0인데 final_answer 빈약 (디렉 hit 1회 = 수집 완료로 처리)

### 변경
- `expand_source_tool_call`: dir `GrepSource` → pattern `.` **강제** (LLM narrow pattern 무시)
- `register_source_hit`: dir grep 파일 수 < min → hit 미등록 (`source_grep_depth`)
- `pending_source_ids_for_plan`: shallow dir도 pending → runtime 1턴 wide grep 재시도
- `source_coverage_passes`: pending 비어야 통과
- ingest: grep raw ≥1.5KB → chunk + LLM 1-pass 요약 즉시 저장

### 검증
- read-only regression 20/20 · router 재배포

---

## 2026-06-21 — final_answer 예산 미사용 + 수집 범위 한계

### 증상
- cov 1.0 · final_answer 도달했지만 답변이 4디렉 × 1~3파일 수준으로 빈약
- 로그: `retrieved budget: 4495` vs `retrieval_total_tokens: 492`, `pack_tokens: 2294`

### 원인 (2가지)
1. **수집 범위**: cov 1.0 = 디렉터리당 Grep 1회 성공 (source_hit). LLM이 docstring Grep만 실행 → adapters 1/8파일, integrations 3/3, legacy 1/3, runtime_core 3/N
2. **예산 미사용**: final_answer에서 `artifact` tier 미포함, `prompt_excerpt` ingest 크기(227~522 chars)만 전달, raw 15KB 재빌드·LLM 1-pass 미실행

### 변경
- `artifact_excerpt.py`: `rebuild_prompt_excerpt_for_budget` — final_answer 시 raw 재빌드 + LLM 1-pass (force)
- `retriever.py`: final_answer coverage target 우선, per-target budget floor 512+
- `prompt_builder.py`: `_pack_final_answer_evidence` — retrieved+artifact+session_tail 예산으로 `[collected_evidence]` system 주입

### 검증
- read-only regression 18/18 · router 재배포
- container LLM summarize ok at final_answer rebuild

### 남은 gap
- **파일 전체 수집**은 GlobSource(`*.py`) 추가 턴 필요 — cov는 디렉터리 hit 기준

---

## 2026-06-21 — artifact_excerpt Docker 누락 + LLM 1-pass 요약

### 증상
- 1턴: `tool_planning · evidence 0/1 · cov 0.50 · ~1.46s` 후 종료
- 2턴 ingest: `ModuleNotFoundError: No module named 'artifact_excerpt'`

### 원인
- `artifact_excerpt.py` Dockerfile COPY 누락 → Grep 결과 저장 시 크래시
- LLM 1-pass chunk 요약 미구현 (rule-only excerpt만 있었음)

### 변경
- `router/Dockerfile` — `artifact_excerpt.py` COPY
- `artifact_excerpt.py` — `apply_llm_one_pass` (fast backend, chunk별 1-pass, default on)

### 검증
- `test-artifact-excerpt.py` ALL OK · container import OK · router 재배포 (~23:35 KST)

---

## 2026-06-18 — Build vs Buy + adapters/ + legacy 격리

### 완료

1. ✅ **코드 구조**
   - `legacy/memory_store.py` · `legacy/retriever.py` · `legacy/agent_runs.py` 이동
   - top-level shim **삭제** — import는 `adapters/` 또는 `legacy/`만
   - `adapters/` — memory · retrieval · gateway · trace · observe · langgraph · mcp
   - `runtime_core/scheduler_contract.py` — SchedulerInputs/Outputs
   - `dynamic_context_scheduler.py` → adapters import + scheduler I/O 기록
2. ✅ **문서**
   - VISION — Build vs Buy 계층 정렬 · Reference Architecture · Why chain · Scheduler I/O · IP ★
   - INTEGRATIONS — Layer Build/Buy 표 · anti-patterns · target stack
   - ARCHITECTURE · MODULE_MAP — adapters tier 반영
3. ✅ 검증: matrix 25/25 · recovery E2E · gateway mock+live · OTel 9-event wire · boundary 0

### 미완료

- ▶ MCP v2
- ▶ Phoenix export (2순위)

### LangGraph memory backend (2026-06-18)

- ✅ `integrations/langgraph_memory.py` — SqliteStore + SqliteSaver · `MEMORY_BACKEND=langgraph`
- ✅ `adapters.memory` API 유지 · legacy ingest + LangGraph persistence wire
- ✅ `scripts/benchmark-memory-backend-swap.py` — legacy/langgraph 동일 gate PASS

### Memory Hierarchy quality gate (2026-06-18)

- ✅ `scripts/benchmark-memory-hierarchy.py --quality-gate` — 5 cases · fail breakdown · gates PASS
- ✅ `scripts/benchmark-repeated-read-avoidance.py` — live 1.00 · stress 0.80 · CI 편입
- ✅ `runtime_core/evidence_keys.py` · `evidence_cluster.py` — canonical keys · cluster dedup · recovery skip full Read
- ✅ `context_need.refine_coverage_targets` — query + known_files → working-set targets
- ✅ `coverage_checker.analyze_coverage_fail_reasons` — 6 reason codes
- ✅ docs: VISION · ARCHITECTURE §5.0 · BENCHMARK · `assets/context-runtime-1page.mmd`

**다음**: MCP v2 · Agent Runtime v2 · Phoenix export

```bash
python3 scripts/benchmark-memory-hierarchy.py --quality-gate
python3 scripts/benchmark-repeated-read-avoidance.py
python3 scripts/benchmark-memory-backend-swap.py
bash scripts/verify-architecture.sh
```

**Gates PASS**: ratio ≤ 0.018 · coverage 1.00 · task/recovery 100% · re-read live 1.00 · stress 0.80 · backend swap OK

| tier | API (`adapters.memory`) |
|------|-------------------------|
| session | `load_session_state` · `save_turn_delta` |
| artifact | `save_artifact` · `save_tool_result` |
| vector | via `adapters.retrieval` |
| policy | `query_memory(tier=policy)` · `compact_memory` |
| working set | `build_working_set` · `collect_hierarchy_snapshot` |

Policy: `runtime_core/memory_policy.py` · event: `memory.hierarchy.snapshot` (10-event wire)

| 지표 | 의미 |
|------|------|
| raw_history_tokens | Cursor full history |
| stored_memory_* | Runtime DB/artifact |
| retrieved_memory_tokens | 이번 turn 검색 |
| prompt_pack_tokens | LLM 입력 |
| gpu_context_tokens | working set cap |
| memory_hit_rate | need target hit |
| repeated_read_avoidance | re-read 감소 |

### Dependency graph (2026-06-18)

```bash
python3 scripts/generate-dependency-graph.py --verify
bash scripts/verify-architecture.sh   # boundary + bench + graph
```

| 산출물 | 경로 |
|--------|------|
| Before/After doc | `docs/dependency-before-after.md` |
| After graph | `docs/assets/dependency-after.mmd` |
| Memory funnel | `docs/assets/memory-hierarchy.mmd` |
| CI | `.github/workflows/architecture.yml` |

**다음**: LangGraph checkpointer wire (`LANGGRAPH_ENABLED=1`)

### Langfuse OTel export (live 검증 완료 2026-06-18)

```bash
./scripts/start-langfuse-local.sh                         # self-hosted :3100
source configs/langfuse-local.env
LANGFUSE_LIVE=1 python3 scripts/test-langfuse-export.py   # dashboard API verify
./scripts/benchmark-observability-live.sh                 # boundary + Langfuse live
```

경로: `runtime_core/runtime_events` → `adapters/trace` → `integrations/flow_tracing` → `integrations/otel` (OTLP) → Langfuse

| 항목 | 상태 |
|------|------|
| OTel 9 events wire | ✅ in-memory |
| Langfuse OTel export | ✅ `LANGFUSE_LIVE=1` PASS |
| flow_id/run_id/turn_index 추적 | ✅ trace metadata |
| llm.completed fields | ✅ gateway_backend · latency · tokens |
| coverage / recovery fields | ✅ |
| boundary | ✅ 0 violations |

로컬 Langfuse: `docker-compose.langfuse.yml` + `configs/langfuse-local.env`  
`LANGFUSE_LIVE=1` 시 Langfuse 없으면 **FAIL** (in-memory PASS 금지)

Cloud 사용 시 `.env`에 `LANGFUSE_HOST`, `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY` 설정.

### Gateway swap (live 검증 완료 2026-06-18)

```bash
./scripts/start-gateway-live.sh                                    # llama-long :8082 + litellm :4000
GATEWAY_LIVE=1 LONG_URL=http://127.0.0.1:8082 BACKEND=llama_cpp python3 scripts/benchmark-gateway-swap.py
GATEWAY_LIVE=1 LITELLM_URL=http://127.0.0.1:4000 BACKEND=litellm python3 scripts/benchmark-gateway-swap.py
./scripts/benchmark-gateway-live.sh                                # full A/B + boundary
BACKEND=mock python3 scripts/benchmark-gateway-swap.py             # offline CI 5/5
```

`main.py` → `adapters.gateway.chat_completion()` only.

| backend | pass | stream | usage | tool_call | connection 502 | OTel llm.completed |
|---------|------|--------|-------|-----------|----------------|---------------------|
| mock | 5/5 ✅ | ✅ | ✅ | ✅ | — | ✅ |
| llama_cpp (live) | 6/6 ✅ | ✅ | ✅ | ✅ | ✅ | ✅ backend·latency·tokens |
| litellm (live) | 6/6 ✅ | ✅ | ✅ | ✅ | ✅ | ✅ backend·latency·tokens |

Live infra: `docker-compose.gateway-live.yml` + `configs/litellm-gateway-live.yaml`  
`GATEWAY_LIVE=1` 시 서버 없으면 **FAIL** (SKIP 아님). litellm만 `LITELLM_PROVIDER_SKIP=1` 시 provider SKIP.

| 항목 | 상태 |
|------|------|
| Gateway swap | ✅ wired · mock + live pass |
| Live llama_cpp / litellm | ✅ `GATEWAY_LIVE=1` 6/6 |
| Langfuse/Phoenix export | ✅ Langfuse OTel live · Phoenix ▶ |

### OTel 9종 wire (2026-06-18)

```bash
python3 scripts/test-observability-export.py   # 9 events + runtime_core isolation
```

| event | emit 위치 |
|-------|-----------|
| runtime.turn.start … prompt.built | `dynamic_context_scheduler` → `runtime_core/runtime_events` |
| llm.completed | `adapters/gateway` |
| runtime.turn.end | `main.py` |

경로: `runtime_core/runtime_events.py` (dict) → `adapters/trace.emit_runtime_event` → `integrations/flow_tracing` (OTel span + capture buffer)

---

```bash
LITELLM_ENABLED=0|1
LANGGRAPH_ENABLED=0|1
VECTOR_RETRIEVAL=1
OTEL_FLOW_TRACE=1
```

### 구조 검증 (2026-06-18)

```bash
python3 scripts/check-architecture-boundary.py   # import layer rules
python3 scripts/test-architecture-boundary.py    # pytest wrapper
python3 scripts/benchmark-retriever-swap.py      # legacy BM25 vs LlamaIndex A/B
python3 scripts/test-observability-export.py     # OTel/turn log smoke
```

| 검사 | 기준 |
|------|------|
| boundary | runtime_core → legacy/adapters 금지 · app → legacy 금지 (prompt_builder format 예외) |
| retriever swap | 동일 `adapters.retrieval` API · matrix 25/25 both backends |
| observability | turn log fields · trace adapter import |

---

## 2026-06-18 — 문서 3분할 + Flow 복원

### 완료

1. ✅ **VISION.md** — Business + **핵심 Flow** (Master · Internal · Budget IP · Recovery · Memory · Closed Loop)
2. ✅ **ARCHITECTURE.md** — 기술 심화 11장 (Pipeline · Need · Budget · Recovery · Sequence · Module Map)
3. ✅ **BENCHMARK.md** — Context Funnel · Success Funnel · Recovery E2E 그래프
4. ✅ VISION.html / ARCHITECTURE.html 재생성

### 문서 역할

| 문서 | 독자 |
|------|------|
| VISION | 투자자 · PM (Flow 40% + Text 30%) |
| ARCHITECTURE | 개발자 · 기술 심사 |
| BENCHMARK | 객관 근거 |

---

### 완료

1. ✅ **LlamaIndex adapter** — `integrations/llamaindex.py`
   - `VECTOR_RETRIEVAL=1` → `retrieve_for_need`에 vector merge
   - `LLAMAINDEX_ENABLED=0` → builtin BM25 (의존성 없음)
   - `LLAMAINDEX_ENABLED=1` → llama-index 설치 시 VectorStoreIndex
2. ✅ **reference/ relative import** — `from .planner import ...`
3. ✅ **Dynamic budget 벤치 25케이스** — `scripts/benchmark-dynamic-budget-matrix.py`
4. ✅ **flow_trace → OTel** — `integrations/flow_tracing.py`, `flow_trace.py` facade
   - `OTEL_FLOW_TRACE=1` + optional `FLOW_TRACE=1` JSON backup

### 검증

```bash
bash scripts/run-vector-e2e.sh                    # BM25 + LlamaIndex (115 artifacts)
python3 scripts/benchmark-vector-retrieval-e2e.py # single backend (see LLAMAINDEX_ENABLED)
python3 scripts/test-vector-retrieval.py          # unit
```

### 환경 변수 (신규)

```bash
VECTOR_RETRIEVAL=1
LLAMAINDEX_ENABLED=0        # builtin BM25 default
OTEL_FLOW_TRACE=1
FLOW_TRACE=0                # 1 = .flow.json backup
```

### 선택 의존성

```bash
python3 -m venv .venv-llamaindex
. .venv-llamaindex/bin/activate
pip install -r router/requirements-integrations.txt
bash scripts/run-vector-e2e.sh
```

---

## 2026-06-18 — 2차 리팩토링

(reference/ 이동, legacy optimizer 제거, runtime_core, integrations OTel/Langfuse)

---

## 2026-06-18 — read_only_analysis 루프 정책 패치

### 변경 파일
- `router/reference/planner.py` — Shell next_action 강제 제거, constraints-only planner, coverage gate
- `router/reference/target_coverage.py`, `router/reference/project_root.py` — 신규
- `router/reference/plan_state.py` — answer→final coverage 필수
- `router/context_need.py` — 구조 분석 시 architecture (요약 키워드 오분류 수정)
- `router/intent_router.py` — router_intent 전달, filter_tools_by_plan
- `router/legacy/memory_store.py` — workspace → repo root resolve
- `scripts/benchmark-read-only-analysis-regression.py` — 신규 회귀 5건
- `scripts/benchmark-agent-deadend-regression.py` — answer coverage 테스트 갱신

### 완료
- read_only_analysis: Shell 기본 금지, Read/Grep/Glob만
- rule planner: allowed_tools / preferred_sources / coverage_targets만 생성
- workspace root: `/home/yunahe` 홈 디렉터리 fallback 금지
- evidence: Shell 문자열 매칭 → target coverage 기반
- final_answer: next_action=answer + coverage 통과 시에만

### 검증
- `python3 scripts/benchmark-read-only-analysis-regression.py` — 5/5 pass
- `python3 scripts/benchmark-agent-deadend-regression.py` — 5/5 pass

### 다음 작업
- live chat에서 `tool_planning_keep_warm_long` 로그 확인 (long→fast 34s switch 제거)
- partial_final_answer hang (flow end 누락) 별도 추적

---

## 2026-06-21 — default fast + compressed pack always fast

### 변경 파일
- `.env` / `.env.example` / `docker-compose.yml` — `DEFAULT_BACKEND=fast`, `ROUTER_MAIN_BACKEND=fast`
- `router/intent_router.py` — pack ≤ threshold → always fast (keep_warm_long 제거)
- `scripts/benchmark-route-backend-regression.py` — 4 tests

### 완료
- router 부팅 시 long 43s cold start 제거 → fast 기동
- read_only tool_planning: warm long이어도 fast (`tool_planning_read_only_fast`)
- long은 pack > 20k 또는 needs_full_raw_context 일 때만

### 검증
- route-backend regression 4/4 pass
- router 재시작 (~12:53 KST)

---

## 2026-06-21 — source registry (path hallucination 근본 수정)

### 변경 파일
- `router/reference/source_registry.py` — RootMapping, SourceRegistry, resolve/expand
- `router/reference/source_tools.py` — LLM tool inject + ReadSource→Read 변환
- `router/reference/planner.py` — source_registry/candidates, ReadSource policy
- `router/reference/target_coverage.py` — success-only hits, error 문자열 hit 금지
- `router/reference/agent_exec.py` — expand before guard
- `scripts/benchmark-source-registry-regression.py` — 신규 5건

### 완료
- LLM은 source_id만 선택 (ReadSource/GrepSource/GlobSource)
- Runtime이 host/container root resolve 후 path 변환
- path 기반 Read/Grep/Glob read_only에서 차단
- 에러 tool result는 coverage hit 불가

### 검증
- source-registry regression 5/5
- read-only regression 5/5
- deadend regression 5/5

### env (optional)
- `PROJECT_ROOT` — explicit override only when auto-detect fails
- `CONTAINER_PROJECT_ROOT` — container-side repo root if not auto-detected

---

## 2026-06-21 — main.py 200 응답 미전송 버그 (3분 hang)

### 원인
- `postprocess` + `send_response`가 `if resp_status == 400` 블록 안에 잘못 indent
- LLM 200 완료 후 클라이언트에 응답 미전송 → Cursor 120s timeout (`timeout_or_disconnect`)

### 변경
- `router/main.py` 790–1070 dedent

### 검증
- E2E: llm_done 1.6s → source_expand → HTTP 200 in 1.8s
- router 재배포 (~12:57 KST)

---

## 2026-06-21 — tool_planning 루프 → partial_final_answer / bad_ping_pong

### 증상
- read_only 구조 분석: 12+ turn `tool_planning` 루프, cov 0.27↔1.00↔0.57 진동
- 최종 `partial_final_answer · blocked:bad_ping_pong` — Runtime inspector만 표시, 본문 없음

### 원인
1. `ReadSource` on `dir.*` → `Read(/path/to/dir)` (디렉터리 Read 실패, source_hits 미등록)
2. `coverage_targets` 중복 (MODULE_MAP vs docs/MODULE_MAP, bare `runtime_core`)
3. `agent_plan_evidence_incomplete`가 cov 1.00이어도 phase를 tool_planning에 고정
4. `bad_ping_pong` 시 coverage 완료여도 `partial_final_answer`로 강등

### 변경
- `source_registry.py` — dir ReadSource → Glob(`*`), GrepSource 빈 pattern → `"."`
- `context_need.py` — `preferred_sources` 있으면 coverage_targets = preferred만
- `loop_guard.py` — progress에 source_hits 추적, BAD_PING_PONG_TURNS 3→6
- `plan_state.py` — tool result 후 can_final → final_answer; bad_ping_pong+coverage → final_answer
- `planner.py` — partial_final_answer tools 항상 strip

### 검증
- source-registry 9/9 · read-only 8/8 · route-backend 4/4 · deadend 5/5 · ping-pong gate OK
- router 재배포 (~22:08 KST)

### 사용자 액션
- **같은 채팅(70+ msg, 97% 압축)은 poisoned state** — 새 채팅에서 재시도 권장
- 질문: `runtime_core, adapters, legacy, integrations 역할을 요약해줘. 코드 수정 말고 읽어서 근거와 함께 답해.`

---

## 2026-06-21 — 반복 루프 근본 원인 (evidence gate + prompt blind)

### 증상
- 매 turn 동일 4 tool (MODULE_MAP, ARCHITECTURE, Grep runtime_core/adapters)
- `agent_plan_evidence_incomplete` + cov 0.50 고정
- LLM thinking에 "뭐가 부족한지" 안 보임

### 원인
1. **evidence_types_satisfied 버그** — `target_coverage`가 문자열 `"target_coverage"`만 찾음 → `source_hit:*` 무시 → 영원히 incomplete
2. **tool_planning prompt 2-msg** — tool result/session_tail 미포함 → LLM이 이미 읽은 파일 모름
3. **ingest source_id 누락** — artifact path만 저장 → coverage hit 미등록
4. **cursor_reasoning** — missing source_id 미표시 (503자 generic)

### 변경
- `plan_state.py` — read_only는 `can_final_answer(source_coverage)` 로 gate
- `evidence_extractors.py` — target_coverage + source_hit 인정
- `prompt_builder.py` — read_only tool_planning에 최근 tool tail 4건
- `planner.py` — missing_source_ids in Saved Agent Plan; replan 완화
- `memory_store.py` — ingest 시 path→source_id resolve
- `cursor_reasoning.py` — missing source_id 한국어 표시

### 검증
- read-only regression 10/10 · source-registry 9/9 · ping-pong OK
- router 재배포 (~22:25 KST)

---

## 2026-06-21 — discovery registry (하드코딩 프리셋 제거)

### 변경
- `STRUCTURE_MODULES` / `DOC_TARGETS` / fixed defaults **삭제**
- `discover_read_only_relpaths()` — query token + repo filesystem → registry candidates
- `summary_source_ids` / `required_source_ids` — registry에서만 derive
- `filter_redundant_source_tool_calls` — hit된 source_id 재호출 차단 (Cursor repeat-loop)
- `read_only_docs_sufficient` — discovery summary docs hit 시 final (고정 doc id 쌍 제거)

### 검증
- read-only 11/11 · source-registry 10/10
- router 재배포 (~22:37 KST)

---

## 2026-06-21 — read_only 일반화 (프로젝트 특화 힌트 제거)

### 방향
- Runtime = discovery + coverage gate; LLM = source_id로 ReadSource/GlobSource/GrepSource만
- 프롬프트/게이트에 MODULE_MAP·VISION·runtime_core 등 **고정 doc/모듈 이름 없음**

### 변경
- `agent_exec.py` — `SYSTEM_FINAL_ANSWER` 쿼리·tool 결과 기반 일반 지침 (tier 표·고정 모듈 리스트 제거)
- `source_registry.py` — `discover_read_only_relpaths`: README/docs/*.md + query token + 일반 code root (`src`, `lib`, `pkg` …)
  - `DOC_NAME_HINT_RE`: readme|architecture|module|structure|design … (vision/handoff/benchmark 제외)
  - venv/site-packages discovery 제외
  - `source_id_for_relpath`: `router/` 특화 제거 → `docs/`·2-segment dir 일반 규칙
- `context_need.py` — `STRUCTURE_KW` 일반화; architecture preset `coverage_targets=[]`
- `planner.py` — read_only `done_when` / tool_planning 힌트 일반화
- Coverage: `summary_source_ids` 전부 hit **또는** query 언급 dir `source_id` hit → final; docs-only escape는 discovery summary doc

### 검증
- read-only 11/11 · source-registry 10/10

### 남은 프로젝트 특화 (read_only 핫패스 밖)
- `planner.py` benchmark_analysis → BENCHMARK.md
- `read_guard.py` · `failed_action.py` · `answer_tokens.py` — BENCHMARK/handoff
- `evidence_extractors.py` — PROJECT_TREE_DIRS

---

## 2026-06-21 — Cursor repeating pattern + cov UI stuck (P0)

### 증상
- `Runtime · tool_planning · evidence 0/1 · cov 0.50 · blocked:coverage_incomplete` ~1.7s 후 Cursor가 **"repeating response pattern"** 로 턴 중단
- Glob 3/4 dir hit (`source_hits`)인데 UI cov 0.50 고정

### 원인
1. `completion_json_to_sse`가 **tool_planning마다** `_runtime_inspector` `<details>` (~3KB)를 assistant content로 주입 → 매 턴 동일 패턴
2. Glob artifact `path`가 `__init__.py`(file ref)로 저장 → `coverage_checker`가 `dir.*` target과 불일치
3. `check_coverage`가 `source_hits`/`coverage_hits` 미반영

### 변경
- `reference/agent_exec.py` — **tool_planning SSE에서 inspector content 주입 금지** (final_answer/partial만)
- `legacy/memory_store.py` — Glob `target_directory` / `Result of search in '...'` 우선 path 저장
- `coverage_checker.py` — `source_hits` + `coverage_hits`로 dir target 충족 판정
- `dynamic_context_scheduler.py` — agent_plan hits를 check_coverage에 전달
- `scripts/test-runtime-inspector.py` — tool_planning inspector 미주입 테스트
- regression +1 (`test_coverage_checker_honors_source_hits`) → **22/22**

### 검증
- benchmark-read-only 22/22 · runtime-inspector · agent-exec PASS
- router 재배포 (~23:55 KST)

### 미완료
- 4 dir 전부 Glob → `cov 1.00` → `final_answer` E2E (사용자 재시도 필요)
- `blocked_read_loop` (plan.blocked Read) read_only Glob-only 경로 확인

---

## 2026-06-21 — artifact excerpt (chunk+merge, summary≠prompt)

### 문제
- Grep 15K → `format_analysis_compact` 900자 head preview를 `art.summary`로 저장
- retriever/prompt이 summary만 사용 → LLM이 파일 목록·docstring 대부분 못 봄
- “요약” 이름이지만 실제는 앞 N줄 truncate

### 변경
- `artifact_excerpt.py` — Grep workspace_result 파싱 → 파일별 docstring 추출 → chunk(35 files) → merge
- `Artifact.prompt_excerpt` + `excerpt_chunks` (KV) ingest 시 저장; `summary`는 index/UI 전용
- `retriever._prompt_content` / `artifact_prompt_text` — prompt 경로에서 `summary`·`format_analysis_compact` 차단
- `prompt_builder` — tool_context/session_tail/compact 모두 excerpt 사용

### 검증
- `scripts/test-artifact-excerpt.py` — 80파일 grep 전부 목록화
- read-only regression 18/18

---

## 2026-06-21 — 채팅 세션 리셋 (new_chat 오염 상태)

### 증상
- Cursor에서 새 채팅을 열어도 `msgs=98`, `stored_memory_items=100`, `blocked:coverage_incomplete` 반복
- `memory new chat` 로그는 간헐적으로 뜨지만 **artifacts/agent_plan/loop_guard가 안 비워짐**

### 원인
1. `_fresh_chat_state`가 `session_id/chat_id`만 바꾸고 artifacts·plan·phase 유지
2. `count_shrink` / `cursor_summary`는 delta baseline만 조정, **아티팩트·플랜 유지**
3. Cursor가 새 composer를 열어도 body가 138 msgs 그대로면 `is_fresh_chat=False` → 같은 세션 계속
4. stuck loop에서 같은 질문 재전송 시 리셋 없음

### 변경 (`legacy/memory_store.py`)
- `_blank_chat_state` — chat-scoped 전체 초기화 (artifacts, agent_plan, loop_guard, phase_state 등)
- `resolve_session` — dramatic shrink (138→3) 시 `new_chat` + full reset
- `ingest_request` — stuck loop escape (turns≥6, artifacts≥8, 동일 query 재전송) → reset + message rebaseline
- `cursor_summary` — ingest **전** full reset (과거 tool delta 재수집 방지)
- `ensure_agent_plan(..., force_replan=True)` on reset transitions
- `Artifact.chat_id` + retriever chat_id 필터

### 검증
- `scripts/test-memory-store.py` — dramatic shrink / loop escape / cursor summary **artifacts=0** PASS
- read-only regression 18/18
- router 재배포 (~23:12 KST)

---

## 2026-06-21 — read_only tools=0 즉시 종료 (cov 1.00 · evidence 0/1)

### 증상
- `tool_planning · evidence 0/1 · cov 1.00 · ~5.8s` 후 Runtime inspector만 표시되고 종료
- 로그: `proxy_body tools=0`, LLM content에 JSON `tool_calls`, `empty_outgoing`

### 원인
1. Cursor compressed pack이 `tools=[]` → inject 없이 strip
2. LLM이 본문 JSON으로 GrepSource 제안 → 파서 미지원 → content 삭제
3. UI `cov 1.00`은 `coverage_targets=[]` 기본값 (source hit 무관)
4. **(근본)** Docker에서 `mapping.host`(`/home/...`)가 컨테이너에 마운트 안 됨 → `discover_read_only_relpaths` 빈 배열 → registry 0 → tools inject 실패

### 변경
- `source_registry._discovery_scan_root` — host 없으면 **container `/app`에서 discovery**
- `resolve_source_id` — `runtime_core` → `dir.runtime_core` 정규화
- `intent_router._apply_tools_policy` — needs_tools + tool_planning 시 inject 강제
- `response_guard` — `tool_name` 필드 JSON 파싱

### 검증
- read-only 14/14 · source-registry 12/12
- **docker exec CONTAINER_VERIFY_OK**: discovery 4 dirs, tools `ReadSource/GrepSource/GlobSource`
- router 재배포 (~22:52 KST)

### 사용자 액션
- router 재시작 후 동일 질문 재시도 — stuck 상태면 **loop escape**가 자동 reset
- 로그에서 `memory chat reset reason=...` 확인

---

## 2026-06-18 — 1차 Product Definition

(VISION.md 재작성)
