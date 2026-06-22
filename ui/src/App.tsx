import { useEffect, useState } from "react";
import { RunProgressPanel } from "./components/RunProgressPanel";
import "./App.css";

type RunListItem = { run_id: string; status: string; turn_index?: number };

export default function App() {
  const [runs, setRuns] = useState<RunListItem[]>([]);
  const [runId, setRunId] = useState("");
  const [inputId, setInputId] = useState("");

  useEffect(() => {
    fetch("/router/agent/runs")
      .then((r) => r.json())
      .then((data) => {
        const list = (data.runs ?? []) as RunListItem[];
        setRuns(list);
        if (list[0]?.run_id) {
          setRunId(list[0].run_id);
          setInputId(list[0].run_id);
        }
      })
      .catch(() => {});
  }, []);

  return (
    <div className="app">
      <header className="app-header">
        <h1>Agent Run Progress</h1>
        <form
          className="run-picker"
          onSubmit={(e) => {
            e.preventDefault();
            if (inputId.trim()) setRunId(inputId.trim());
          }}
        >
          <input
            value={inputId}
            onChange={(e) => setInputId(e.target.value)}
            placeholder="run_id"
            spellCheck={false}
          />
          <button type="submit">Load</button>
          <select
            value={runId}
            onChange={(e) => {
              setRunId(e.target.value);
              setInputId(e.target.value);
            }}
          >
            {runs.map((r) => (
              <option key={r.run_id} value={r.run_id}>
                {r.run_id} ({r.status})
              </option>
            ))}
          </select>
        </form>
      </header>
      {runId ? <RunProgressPanel runId={runId} /> : <p className="hint">run_id를 선택하세요.</p>}
    </div>
  );
}
