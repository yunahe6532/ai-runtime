# Planner Promotion Metrics

> Generated: 2026-06-22 03:49:59 UTC

Trace: `/tmp/tmpoe3fvx7b/promotion-live.ndjson`

## Summary

| Metric | Count | Rate (of evaluated) |
|--------|------:|--------------------:|
| evaluated | 11 | — |
| eligible | 5 | 45.5% |
| applied | 5 | 45.5% |
| blocked (events) | 12 | 109.1% |
| skipped | 0 | 0.0% |

**apply / eligible:** 100.0%

## Skip reasons

- (none)

## Block reasons (top)

- `not_eligible`: 6
- `blocked_by_intent:not_read_only_analysis`: 1
- `blocked_by_action:edit`: 1
- `blocked_by_action:not_promotable:edit`: 1
- `blocked_by_action:shell`: 1
- `blocked_by_action:not_promotable:shell`: 1
- `blocked_by_action:final`: 1
- `blocked_by_action:not_promotable:final`: 1
- `promotion blocked: invalid target`: 1
- `blocked_by_confidence:0.4<0.75`: 1

## Applied actions

- `read`: 3
- `grep`: 1
- `glob`: 1

## Intent breakdown

| intent | eligible | applied |
|--------|--------:|--------:|
| `unknown` | 5 | 5 |
