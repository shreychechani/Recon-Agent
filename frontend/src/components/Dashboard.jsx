import { Panel, Stat, Bar, pct } from "./ui.jsx";

const SOURCE_META = {
  exact_ref: { label: "Exact reference", tone: "emerald" },
  fee_adjusted: { label: "Fee-adjusted", tone: "emerald" },
  subset_sum: { label: "Subset-sum", tone: "sky" },
  assignment: { label: "Global assignment", tone: "sky" },
  learned_rule: { label: "Learned rule", tone: "emerald" },
  llm: { label: "LLM adjudicator", tone: "violet" },
  analyst: { label: "Analyst (manual)", tone: "amber" },
  none: { label: "Abstained", tone: "slate" },
};
const SOURCE_ORDER = [
  "exact_ref", "fee_adjusted", "subset_sum", "assignment",
  "learned_rule", "llm", "analyst", "none",
];

export default function Dashboard({ run }) {
  const truth = run.has_truth;
  const halluc = run.hallucinated_matches;

  return (
    <div className="space-y-5">
      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        <Stat
          label="Auto-resolve rate"
          value={pct(run.coverage)}
          sub={`${run.decided}/${run.total} decided`}
          tone="emerald"
        />
        <Stat
          label="Precision"
          value={truth ? pct(run.precision) : "—"}
          sub={truth ? "on decided records" : "upload ground truth to score"}
          tone={truth ? (run.precision >= 1 ? "emerald" : "rose") : "slate"}
        />
        <Stat
          label="Hallucinated traps"
          value={truth ? `${halluc}` : "—"}
          sub={truth ? `of ${run.traps_total} traps` : "—"}
          tone={truth ? (halluc === 0 ? "emerald" : "rose") : "slate"}
        />
        <Stat
          label="Cost / record"
          value={`$${(run.cost_per_record_usd ?? 0).toFixed(6)}`}
          sub={`${run.llm_calls_total} LLM calls · ${run.records_with_llm} records`}
          tone="sky"
        />
      </div>

      <div className="grid gap-5 lg:grid-cols-2">
        <Panel title="Resolution sources">
          <div className="space-y-0.5">
            {SOURCE_ORDER.filter((k) => run.resolution_sources?.[k]).map((k) => (
              <Bar
                key={k}
                label={SOURCE_META[k].label}
                tone={SOURCE_META[k].tone}
                value={run.resolution_sources[k]}
                total={run.total}
              />
            ))}
          </div>
          <p className="mt-3 border-t border-white/5 pt-3 text-xs text-slate-500">
            Deterministic layers resolve the bulk with zero model calls; the LLM
            adjudicator and learned rules only touch the contended remainder.
          </p>
        </Panel>

        {truth ? (
          <Panel
            title="Coverage / precision tradeoff"
            right={
              <span className="font-mono text-[11px] text-slate-400">
                τ={run.chosen_threshold?.threshold?.toFixed(2)} · {pct(run.chosen_threshold?.coverage)} @ 100%
              </span>
            }
          >
            <TradeoffCurve
              points={run.tradeoff_curve}
              chosen={run.chosen_threshold?.threshold}
            />
          </Panel>
        ) : (
          <Panel title="Coverage / precision tradeoff">
            <p className="text-sm text-slate-500">
              Ground truth not provided for this run, so precision can’t be scored.
            </p>
          </Panel>
        )}
      </div>

      {truth && (
        <Panel title="Per-difficulty breakdown">
          <table className="w-full text-sm">
            <thead>
              <tr className="text-left text-[11px] uppercase tracking-wider text-slate-500">
                <th className="pb-2 font-medium">Tier</th>
                <th className="pb-2 text-right font-medium">Total</th>
                <th className="pb-2 text-right font-medium">Decided</th>
                <th className="pb-2 text-right font-medium">Coverage</th>
                <th className="pb-2 text-right font-medium">Precision</th>
              </tr>
            </thead>
            <tbody className="font-mono tabular-nums">
              {run.per_tier?.map((t) => (
                <tr key={t.tier} className="border-t border-white/5">
                  <td className="py-1.5 font-sans capitalize text-slate-300">{t.tier}</td>
                  <td className="py-1.5 text-right text-slate-400">{t.total}</td>
                  <td className="py-1.5 text-right text-slate-400">{t.decided}</td>
                  <td className="py-1.5 text-right text-slate-200">{pct(t.coverage)}</td>
                  <td
                    className={`py-1.5 text-right ${
                      t.precision >= 1 ? "text-emerald-400" : "text-rose-400"
                    }`}
                  >
                    {pct(t.precision)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          <div className="mt-3 flex gap-6 border-t border-white/5 pt-3 text-xs text-slate-500">
            <span>latency p50 {run.latency_p50_ms?.toFixed(2)}ms · p99 {run.latency_p99_ms?.toFixed(2)}ms</span>
            <span>total cost ${run.total_cost_usd?.toFixed(4)}</span>
          </div>
        </Panel>
      )}
    </div>
  );
}

function TradeoffCurve({ points = [], chosen }) {
  if (!points.length) return null;
  const W = 100, H = 40, pad = 1;
  const x = (t) => pad + t * (W - 2 * pad);
  const y = (v) => H - pad - v * (H - 2 * pad);
  const cov = points.map((p) => `${x(p.threshold)},${y(p.coverage)}`).join(" ");
  const prec = points.map((p) => `${x(p.threshold)},${y(p.precision)}`).join(" ");

  return (
    <div>
      <svg viewBox={`0 0 ${W} ${H}`} className="h-40 w-full" preserveAspectRatio="none">
        {[0, 0.25, 0.5, 0.75, 1].map((g) => (
          <line key={g} x1={x(g)} y1={pad} x2={x(g)} y2={H - pad} stroke="#ffffff10" strokeWidth="0.15" />
        ))}
        {chosen != null && (
          <line x1={x(chosen)} y1={pad} x2={x(chosen)} y2={H - pad} stroke="#34d39955" strokeWidth="0.4" strokeDasharray="1 1" />
        )}
        <polyline points={prec} fill="none" stroke="#f59e0b" strokeWidth="0.5" opacity="0.8" />
        <polyline points={cov} fill="none" stroke="#34d399" strokeWidth="0.6" />
      </svg>
      <div className="mt-2 flex justify-between text-[11px] text-slate-500">
        <span className="flex items-center gap-3">
          <span className="flex items-center gap-1"><i className="inline-block h-2 w-2 rounded-full bg-emerald-400" />coverage</span>
          <span className="flex items-center gap-1"><i className="inline-block h-2 w-2 rounded-full bg-amber-500" />precision</span>
        </span>
        <span>confidence threshold →</span>
      </div>
    </div>
  );
}
