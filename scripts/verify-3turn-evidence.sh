#!/usr/bin/env bash
# 3-turn benchmark evidence E2E
set -euo pipefail
ROOT="/home/yunahe/ai-runtime/cursor-local-llm"
API="http://localhost:8080/v1/chat/completions"
RUNS="http://localhost:8080/router/agent/runs"
QUERY="벤치마크 분석: runtime score, agent benchmark, flow phase를 순서대로 확인하고 요약해줘"
SCORE="$ROOT/tmp/benchmark-runtime-score.json"
AGENT="$ROOT/tmp/benchmark-cursor-agent.json"
FLOW="$ROOT/tmp/cursor-captures/1781758111_0037.flow.json"

show_run() {
  local label="$1"
  echo "=== $label ==="
  local rid
  rid=$(curl -s "$RUNS" | jq -r '.runs[0].run_id')
  curl -s "$RUNS/$rid" | jq -c \
    '{run_id, turn_index, parent_run_id, evidence_events: [.events[]|select(.status=="evidence.collected")|{seq,text,data}], final_ready: [.events[]|select(.status=="final.ready")], status: .status}'
  echo
}

post() {
  curl -s --max-time 120 "$API" -H "Content-Type: application/json" -d "$1" | jq -c \
    '{finish: .choices[0].finish_reason, tools: [.choices[0].message.tool_calls[]?.function.name], content_len: (.choices[0].message.content|length), stripped_tools: (.tools|length // 0)}'
}

T1=$(python3 - <<PY
import json
score = open("$SCORE").read()
print(json.dumps({
  "model": "model.gguf", "stream": False,
  "tools": [{"type": "function", "function": {"name": "Read", "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}}}],
  "messages": [
    {"role": "user", "content": """$QUERY"""},
    {"role": "assistant", "content": "", "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "Read", "arguments": json.dumps({"path": "$SCORE"})}}]},
    {"role": "tool", "tool_call_id": "c1", "name": "Read", "content": score}
  ]
}))
PY
)
echo "--- TURN 1: runtime score ---"
post "$T1"
show_run "after turn 1"

T2=$(python3 - <<PY
import json
score = open("$SCORE").read()
agent = open("$AGENT").read()
print(json.dumps({
  "model": "model.gguf", "stream": False,
  "tools": [{"type": "function", "function": {"name": "Read", "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}}}],
  "messages": [
    {"role": "user", "content": """$QUERY"""},
    {"role": "assistant", "content": "", "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "Read", "arguments": json.dumps({"path": "$SCORE"})}}]},
    {"role": "tool", "tool_call_id": "c1", "name": "Read", "content": score},
    {"role": "assistant", "content": "", "tool_calls": [{"id": "c2", "type": "function", "function": {"name": "Read", "arguments": json.dumps({"path": "$AGENT"})}}]},
    {"role": "tool", "tool_call_id": "c2", "name": "Read", "content": agent}
  ]
}))
PY
)
echo "--- TURN 2: + agent benchmark ---"
post "$T2"
show_run "after turn 2"

T3=$(python3 - <<PY
import json
score = open("$SCORE").read()
agent = open("$AGENT").read()
flow = open("$FLOW").read()
print(json.dumps({
  "model": "model.gguf", "stream": False,
  "tools": [{"type": "function", "function": {"name": "Read", "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}}}],
  "messages": [
    {"role": "user", "content": """$QUERY"""},
    {"role": "assistant", "content": "", "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "Read", "arguments": json.dumps({"path": "$SCORE"})}}]},
    {"role": "tool", "tool_call_id": "c1", "name": "Read", "content": score},
    {"role": "assistant", "content": "", "tool_calls": [{"id": "c2", "type": "function", "function": {"name": "Read", "arguments": json.dumps({"path": "$AGENT"})}}]},
    {"role": "tool", "tool_call_id": "c2", "name": "Read", "content": agent},
    {"role": "assistant", "content": "", "tool_calls": [{"id": "c3", "type": "function", "function": {"name": "Read", "arguments": json.dumps({"path": "$FLOW"})}}]},
    {"role": "tool", "tool_call_id": "c3", "name": "Read", "content": flow}
  ]
}))
PY
)
echo "--- TURN 3: + flow → final.ready ---"
post "$T3"
show_run "after turn 3"

RID=$(curl -s "$RUNS" | jq -r '.runs[0].run_id')
echo "=== ALL EVENTS (turn 3 run) ==="
curl -s "$RUNS/$RID" | jq '[.events[]|{seq,type,status:(.status//null),name:(.name//null),text:(.text//null)}]'
