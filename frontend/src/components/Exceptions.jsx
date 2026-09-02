import { useEffect, useState } from "react";
import { api } from "../api.js";
import { Panel, Pill, Spinner, rupee } from "./ui.jsx";

export default function Exceptions({ runId, onRunUpdated }) {
  const [data, setData] = useState(null);
  const [sel, setSel] = useState(null);
  const [busy, setBusy] = useState(false);
  const [toast, setToast] = useState(null);
  const [banner, setBanner] = useState(null);
  const [learned, setLearned] = useState(false);

  const load = async (keepSel) => {
    const d = await api.exceptions(runId);
    setData(d);
    setSel((prev) => {
      const want = keepSel ?? prev;
      const found = d.exceptions.find((e) => e.bank_txn_id === want);
      return found ? want : d.exceptions[0]?.bank_txn_id ?? null;
    });
  };

  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [runId]);

  if (!data) return <Spinner label="Loading exception queue…" />;

  const current = data.exceptions.find((e) => e.bank_txn_id === sel);

  const resolve = async (body) => {
    setBusy(true);
    setToast(null);
    try {
      const res = await api.resolve(runId, sel, body);
      if (res.induced_rule) {
        setLearned(true);
        setToast({
          tone: "emerald",
          text: `Learned a reusable rule from this resolution — pattern `,
          code: res.induced_rule.pattern,
        });
      } else {
        setToast({ tone: "slate", text: res.message });
      }
      await load();
    } catch (e) {
      setToast({ tone: "rose", text: String(e.message || e) });
    } finally {
      setBusy(false);
    }
  };

  const rerun = async () => {
    setBusy(true);
    try {
      const m = await api.rerun(runId);
      onRunUpdated?.(m);
      setBanner(
        `Re-ran with learned rules — ${m.coverage_delta_records} more record(s) auto-resolved. Coverage now ${(m.coverage * 100).toFixed(1)}% at ${(m.precision * 100 || 100).toFixed(1)}% precision.`
      );
      setLearned(false);
      await load();
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="space-y-3">
      {banner && (
        <div className="rounded-lg border border-emerald-500/25 bg-emerald-500/[0.06] px-4 py-2.5 text-sm text-emerald-200">
          {banner}
        </div>
      )}
      {learned && (
        <div className="flex items-center justify-between rounded-lg border border-emerald-500/25 bg-emerald-500/[0.05] px-4 py-2.5">
          <span className="text-sm text-emerald-200">
            A rule was learned. Re-run to apply it across the whole batch.
          </span>
          <button
            onClick={rerun}
            disabled={busy}
            className="rounded-md bg-emerald-500/90 px-3 py-1.5 text-sm font-medium text-emerald-950 hover:bg-emerald-400 disabled:opacity-60"
          >
            Re-run with learned rules
          </button>
        </div>
      )}

      <div className="grid gap-4 lg:grid-cols-[320px_1fr]">
        <Panel
          title={`Exception queue`}
          right={
            <span className="font-mono text-[11px] text-amber-300">{data.count} open</span>
          }
          className="max-h-[70vh] overflow-hidden"
        >
          <div className="-m-1 max-h-[62vh] space-y-1 overflow-y-auto p-1">
            {data.exceptions.length === 0 && (
              <p className="px-2 py-6 text-center text-sm text-slate-500">
                Nothing to triage — every credit is decided. ✓
              </p>
            )}
            {data.exceptions.map((e) => (
              <button
                key={e.bank_txn_id}
                onClick={() => setSel(e.bank_txn_id)}
                className={`w-full rounded-lg border px-3 py-2 text-left transition ${
                  e.bank_txn_id === sel
                    ? "border-white/15 bg-white/[0.06]"
                    : "border-transparent hover:bg-white/[0.03]"
                }`}
              >
                <div className="flex items-center justify-between">
                  <span className="font-mono text-xs text-slate-300">{e.bank_txn_id}</span>
                  <span className="font-mono text-sm tabular-nums text-slate-100">
                    {rupee(e.amount_rupees)}
                  </span>
                </div>
                <div className="mt-1 truncate text-[11px] text-slate-500">{e.layer}</div>
              </button>
            ))}
          </div>
        </Panel>

        {current ? (
          <Detail ex={current} busy={busy} onResolve={resolve} toast={toast} />
        ) : (
          <Panel title="Drill-down">
            <p className="text-sm text-slate-500">Select an exception to inspect it.</p>
          </Panel>
        )}
      </div>
    </div>
  );
}

function Detail({ ex, busy, onResolve, toast }) {
  const [pick, setPick] = useState(null);

  return (
    <Panel
      title="Drill-down"
      right={
        <span className="font-mono text-[11px] text-slate-500">{ex.bank_txn_id}</span>
      }
    >
      <div className="space-y-4">
        <div className="flex flex-wrap items-center gap-3">
          <div className="font-mono text-2xl font-semibold tabular-nums text-slate-100">
            {rupee(ex.amount_rupees)}
          </div>
          <Pill tone="amber">abstained</Pill>
          <span className="text-xs text-slate-500">value date {ex.value_date}</span>
        </div>

        <div className="rounded-lg border border-amber-500/15 bg-amber-500/[0.04] px-3 py-2 text-sm text-amber-200/90">
          Why it’s parked: {ex.layer}
        </div>

        <div className="grid gap-3 sm:grid-cols-2">
          <Field label="Bank narration">
            <div className="rounded-md bg-black/30 px-2.5 py-2 font-mono text-xs text-slate-300">
              {ex.narration || "—"}
            </div>
          </Field>
          <Field label="Extracted references">
            {ex.extracted_utrs?.length ? (
              <div className="flex flex-wrap gap-1">
                {ex.extracted_utrs.map((u) => (
                  <Pill key={u} tone="sky">{u}</Pill>
                ))}
              </div>
            ) : (
              <span className="text-xs text-slate-500">none recoverable from narration</span>
            )}
          </Field>
        </div>

        {ex.llm?.reasoning && (
          <Field label="Adjudicator reasoning">
            <div className="rounded-md border border-violet-500/15 bg-violet-500/[0.04] px-3 py-2 text-sm text-slate-300">
              {ex.llm.reasoning}
            </div>
          </Field>
        )}
        {ex.llm?.notes?.length > 0 && (
          <Field label="Adjudicator notes">
            <ul className="list-disc space-y-1 pl-5 text-xs text-slate-400">
              {ex.llm.notes.map((n, i) => <li key={i}>{n}</li>)}
            </ul>
          </Field>
        )}

        <Field label={`Candidates considered (${ex.candidates.length})`}>
          {ex.candidates.length === 0 ? (
            <span className="text-xs text-slate-500">
              no candidate batch survived filtering — confidently unmatchable
            </span>
          ) : (
            <div className="overflow-hidden rounded-lg border border-white/5">
              <table className="w-full text-xs">
                <thead className="bg-white/[0.03] text-[10px] uppercase tracking-wider text-slate-500">
                  <tr>
                    <th className="px-2 py-1.5 text-left font-medium">Batch</th>
                    <th className="px-2 py-1.5 text-right font-medium">Net total</th>
                    <th className="px-2 py-1.5 text-right font-medium">Δ amount</th>
                    <th className="px-2 py-1.5 text-right font-medium">Date off.</th>
                    <th className="px-2 py-1.5 text-right font-medium">Ref</th>
                    <th className="px-2 py-1.5 text-right font-medium">Lines</th>
                    <th className="px-2 py-1.5" />
                  </tr>
                </thead>
                <tbody className="font-mono tabular-nums">
                  {ex.candidates.map((c) => {
                    const active = pick === c.batch_id;
                    return (
                      <tr
                        key={c.batch_id}
                        className={`border-t border-white/5 ${active ? "bg-emerald-500/[0.06]" : ""}`}
                      >
                        <td className="px-2 py-1.5 text-slate-300">{c.batch_id}</td>
                        <td className="px-2 py-1.5 text-right text-slate-300">{rupee(c.net_total_rupees)}</td>
                        <td className={`px-2 py-1.5 text-right ${c.amount_delta_paise === 0 ? "text-emerald-400" : "text-slate-400"}`}>
                          {c.amount_delta_paise > 0 ? "+" : ""}{c.amount_delta_paise}p
                        </td>
                        <td className="px-2 py-1.5 text-right text-slate-400">T+{c.date_offset_days}</td>
                        <td className="px-2 py-1.5 text-right">
                          <span className={c.ref_strength >= 1 ? "text-emerald-400" : c.ref_strength > 0 ? "text-amber-400" : "text-slate-600"}>
                            {c.ref_strength.toFixed(1)}
                          </span>
                        </td>
                        <td className="px-2 py-1.5 text-right text-slate-400">{c.n_lines}</td>
                        <td className="px-2 py-1.5 text-right">
                          <button
                            onClick={() => setPick(active ? null : c.batch_id)}
                            className={`rounded px-2 py-0.5 text-[11px] ${
                              active
                                ? "bg-emerald-500/20 text-emerald-200"
                                : "bg-white/5 text-slate-300 hover:bg-white/10"
                            }`}
                          >
                            {active ? "selected" : "select"}
                          </button>
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          )}
        </Field>

        {toast && (
          <div
            className={`rounded-lg border px-3 py-2 text-sm ${
              toast.tone === "emerald"
                ? "border-emerald-500/25 bg-emerald-500/[0.06] text-emerald-200"
                : toast.tone === "rose"
                ? "border-rose-500/25 bg-rose-500/[0.06] text-rose-200"
                : "border-white/10 bg-white/[0.04] text-slate-300"
            }`}
          >
            {toast.text}
            {toast.code && <code className="ml-1 rounded bg-black/30 px-1.5 py-0.5 font-mono text-xs">{toast.code}</code>}
          </div>
        )}

        <div className="flex items-center gap-2 border-t border-white/5 pt-3">
          <button
            disabled={!pick || busy}
            onClick={() => onResolve({ batch_id: pick, analyst: "analyst@ops" })}
            className="rounded-lg bg-emerald-500/90 px-3.5 py-2 text-sm font-medium text-emerald-950 transition hover:bg-emerald-400 disabled:cursor-not-allowed disabled:opacity-40"
          >
            {busy ? "Resolving…" : pick ? `Match to ${pick}` : "Select a batch to match"}
          </button>
          <button
            disabled={busy}
            onClick={() => onResolve({ decision: "no_match", analyst: "analyst@ops" })}
            className="rounded-lg border border-white/10 px-3.5 py-2 text-sm text-slate-300 transition hover:bg-white/5 disabled:opacity-40"
          >
            Mark as no-match
          </button>
          <span className="ml-auto text-[11px] text-slate-600">
            every manual match is re-verified before it counts
          </span>
        </div>
      </div>
    </Panel>
  );
}

function Field({ label, children }) {
  return (
    <div>
      <div className="mb-1 text-[11px] font-medium uppercase tracking-wider text-slate-500">
        {label}
      </div>
      {children}
    </div>
  );
}
