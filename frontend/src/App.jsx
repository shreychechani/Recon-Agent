import { useEffect, useState } from "react";
import { api } from "./api.js";
import LoadView from "./components/LoadView.jsx";
import Dashboard from "./components/Dashboard.jsx";
import Exceptions from "./components/Exceptions.jsx";
import { Pill } from "./components/ui.jsx";

const TABS = [
  { id: "load", label: "Load" },
  { id: "dashboard", label: "Dashboard" },
  { id: "exceptions", label: "Exception queue" },
];

export default function App() {
  const [run, setRun] = useState(null); // metrics payload incl. run_id
  const [tab, setTab] = useState("load");
  const [health, setHealth] = useState(null);

  useEffect(() => {
    api.health().then(setHealth).catch(() => {});
  }, []);

  const onRun = (payload) => {
    setRun(payload);
    setTab("dashboard");
  };

  return (
    <div className="mx-auto flex min-h-full max-w-[1240px] flex-col px-6">
      <header className="flex items-end justify-between border-b border-white/5 py-5">
        <div>
          <div className="flex items-center gap-3">
            <div className="grid h-8 w-8 place-items-center rounded-lg bg-emerald-500/15 font-mono text-sm font-bold text-emerald-400 ring-1 ring-emerald-500/25">
              ₹
            </div>
            <h1 className="text-lg font-semibold tracking-tight text-slate-100">
              Reconciliation Console
            </h1>
          </div>
          <p className="mt-1 pl-11 text-xs text-slate-500">
            Three-way settlement reconciliation · auto-resolve at 100% precision,
            willing to say “I don’t know.”
          </p>
        </div>
        <div className="flex items-center gap-2">
          {health && (
            <Pill tone={health.llm_available ? "emerald" : "slate"}>
              {health.llm_available ? "LLM online" : "LLM off (deterministic)"}
            </Pill>
          )}
        </div>
      </header>

      <nav className="flex gap-1 pt-4">
        {TABS.map((t) => {
          const disabled = t.id !== "load" && !run;
          const active = tab === t.id;
          return (
            <button
              key={t.id}
              disabled={disabled}
              onClick={() => setTab(t.id)}
              className={`rounded-lg px-3.5 py-1.5 text-sm transition ${
                active
                  ? "bg-white/10 text-slate-100"
                  : disabled
                  ? "cursor-not-allowed text-slate-600"
                  : "text-slate-400 hover:bg-white/5 hover:text-slate-200"
              }`}
            >
              {t.label}
              {t.id === "exceptions" && run?.abstained ? (
                <span className="ml-2 rounded bg-amber-500/15 px-1.5 py-0.5 font-mono text-[10px] text-amber-300">
                  {run.abstained}
                </span>
              ) : null}
            </button>
          );
        })}
      </nav>

      <main className="flex-1 py-5">
        {tab === "load" && <LoadView onRun={onRun} />}
        {tab === "dashboard" && run && <Dashboard run={run} />}
        {tab === "exceptions" && run && (
          <Exceptions runId={run.run_id} onRunUpdated={setRun} />
        )}
      </main>

      <footer className="border-t border-white/5 py-4 text-center text-[11px] text-slate-600">
        Deterministic first · global assignment · LLM adjudicator · learned rules —
        every match re-verified before it counts.
      </footer>
    </div>
  );
}
