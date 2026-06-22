# Phase 2.2b Live Validation Report

> 2026-06-22 — promotion enable (`SHADOW_ONLY=0`, `ENABLE_READONLY=1`)

## Env

```bash
PLANNER_PROMOTION_SHADOW_ONLY=0
PLANNER_PROMOTION_ENABLE_READONLY=1
PLANNER_PROMOTION_MAX_PER_TURN=1
LLM_PLANNER_SHADOW_ENABLED=1   # router live benchmark only
```

## 1. Apply E2E (mock LLM)

```bash
PLANNER_PROMOTION_SHADOW_ONLY=0 \
PLANNER_PROMOTION_ENABLE_READONLY=1 \
PLANNER_PROMOTION_MAX_PER_TURN=1 \
python3 scripts/test-planner-promotion-apply-e2e.py
```

**Result:** 11/11 PASS — read/grep/glob applied, edit/shell/final/vendor/repeat blocked.

## 2. Synthetic live harness (11 scenarios)

```bash
python3 scripts/benchmark-planner-promotion-live.py
```

| Metric | Value |
|--------|------:|
| scenarios | 11 |
| eligible | 5 (45.5%) |
| applied | 5 (45.5%) |
| blocked | 6 |
| skipped | 0 |
| **apply / eligible** | **100%** |

### Intent별 applied (harness)

| intent | applied | next_tool |
|--------|---------|-----------|
| `read_only_analysis` | yes | ReadSource |
| `architecture` | yes | GrepSource |
| `exploration` | yes | GlobSource |
| `project_inspection` | yes | ReadSource |
| `doc_analysis` | yes | ReadSource |
| `code_edit` | no (intent) | — |
| edit/shell/final/vendor/low_conf | no (guard) | — |

## 3. Trace aggregate (mixed: e2e + harness + router turns)

Source: `~/.local/share/ai-runtime/traces/explorer-trace.ndjson`

```bash
python3 scripts/audit-planner-promotion-metrics.py \
  --trace ~/.local/share/ai-runtime/traces/explorer-trace.ndjson
```

| Metric | Count | Rate (of evaluated) |
|--------|------:|--------------------:|
| evaluated | 44 | — |
| eligible | 24 | 54.5% |
| applied | 13 | 29.5% |
| blocked (events) | 43 | — |
| skipped | 12 | 27.3% |
| **apply / eligible** | — | **54.2%** |

### Skip reasons (trace)

| reason | count |
|--------|------:|
| `shadow_only` | 4 |
| `readonly_disabled` | 4 |
| `no_promotion_decision` | 4 |

Skip counts include default-off test runs in the same trace file.

### Block reasons (top)

- `not_eligible` — edit/shell/final/intent/vendor/confidence
- `blocked_by_intent:not_read_only_analysis` — code_edit
- `blocked_by_action:*` — non-promotable actions

## 4. Router benchmark (promotion ON)

Router recreated with `LLM_PLANNER_SHADOW_ENABLED=1` + promotion env.

| Metric | Default (prior) | Promotion ON |
|--------|----------------:|-------------:|
| Tasks passed | 29–30/30 | **30/30** |
| Agent success | ~96.7% | **100%** |
| Avg tool calls | ~0.83–0.87 | 0.97 |
| Task time (ms) | ~750–1000 | ~3250 (LLM shadow overhead) |

`shell_logs` flaky: see `benchmark-known-flaky.md`.

## 5. Final quality (proxy)

| Signal | Observation |
|--------|-------------|
| memory_recall tasks | 30/30 PASS with promotion on |
| redundant reads | 0 |
| tool call reduction | 68.1% vs naive |
| Cursor 실세션 A/B | 별도 검증 필요 |

## 6. 2.2c 권장

| 우선순위 | 작업 | 근거 |
|----------|------|------|
| 1 | `audit-planner-promotion-metrics.py` | trace 집계 |
| 2 | Inspector rate 표시 | eligible/applied counts |
| 3 | trace에 `router_intent` 전파 | intent별 breakdown 정확도 |
| 4 | intent allowlist 튜닝 | doc_analysis OK |
| 5 | REFACTOR.md 갱신 | |
| 6 | default `SHADOW_ONLY=1` 유지 | Cursor A/B 전 |

**보류:** edit/shell/final 승격, promotion default-on.
