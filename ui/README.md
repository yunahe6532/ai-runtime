# Agent Run Progress UI

Read-only progress panel for `/router/agent/runs` SSE events.

## Dev

```bash
cd ui
npm install
npm run dev
```

Open http://localhost:5173 — Vite proxies `/router` → `http://localhost:8080`.

## Test

```bash
npm test              # normalize + reconnect unit tests
npm run test:reconnect
```

Live SSE reconnect (router must be running):

```bash
curl -N "http://localhost:8080/router/agent/runs/1781760132_0006/events?after_seq=3"
```

## Structure

```text
src/run-events/
  types.ts              SDK-aligned event types
  normalizeRunEvent.ts  raw → NormalizedRunEvent
  connectRunEvents.ts   SSE + after_seq reconnect
  evidenceBadges.ts     runtime_score / agent_benchmark / flow_phase
src/components/
  RunProgressPanel.tsx  read-only panel
```
