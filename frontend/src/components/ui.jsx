// Small shared primitives + formatting helpers for the console.

export const pct = (x) => (x == null ? "—" : `${(x * 100).toFixed(1)}%`);
export const rupee = (s) => (s == null ? "—" : `₹${s}`);

export function Panel({ title, right, children, className = "" }) {
  return (
    <div className={`rounded-xl border border-white/5 bg-[#0e141d] ${className}`}>
      {(title || right) && (
        <div className="flex items-center justify-between border-b border-white/5 px-4 py-2.5">
          <h3 className="text-[11px] font-semibold uppercase tracking-[0.14em] text-slate-400">
            {title}
          </h3>
          {right}
        </div>
      )}
      <div className="p-4">{children}</div>
    </div>
  );
}

const TONES = {
  emerald: "text-emerald-400",
  amber: "text-amber-400",
  rose: "text-rose-400",
  sky: "text-sky-400",
  slate: "text-slate-200",
};

export function Stat({ label, value, sub, tone = "slate" }) {
  return (
    <div className="rounded-xl border border-white/5 bg-[#0e141d] px-4 py-3.5">
      <div className="text-[11px] font-medium uppercase tracking-[0.12em] text-slate-500">
        {label}
      </div>
      <div className={`mt-1 font-mono text-3xl font-semibold tabular-nums ${TONES[tone]}`}>
        {value}
      </div>
      {sub && <div className="mt-0.5 text-xs text-slate-500">{sub}</div>}
    </div>
  );
}

export function Pill({ children, tone = "slate" }) {
  const map = {
    emerald: "bg-emerald-500/10 text-emerald-300 ring-emerald-500/20",
    amber: "bg-amber-500/10 text-amber-300 ring-amber-500/20",
    rose: "bg-rose-500/10 text-rose-300 ring-rose-500/20",
    sky: "bg-sky-500/10 text-sky-300 ring-sky-500/20",
    slate: "bg-slate-500/10 text-slate-300 ring-slate-500/20",
  };
  return (
    <span
      className={`inline-flex items-center gap-1 rounded-md px-2 py-0.5 font-mono text-[11px] ring-1 ${map[tone]}`}
    >
      {children}
    </span>
  );
}

// A horizontal labelled bar for the resolution-source distribution.
export function Bar({ label, value, total, tone = "sky" }) {
  const w = total ? Math.max(2, (value / total) * 100) : 0;
  const map = {
    emerald: "bg-emerald-500/70",
    amber: "bg-amber-500/70",
    sky: "bg-sky-500/70",
    slate: "bg-slate-500/60",
    violet: "bg-violet-500/70",
  };
  return (
    <div className="flex items-center gap-3 py-1">
      <div className="w-28 shrink-0 truncate text-xs text-slate-400">{label}</div>
      <div className="h-2 flex-1 overflow-hidden rounded bg-white/5">
        <div className={`h-full rounded ${map[tone]}`} style={{ width: `${w}%` }} />
      </div>
      <div className="w-10 shrink-0 text-right font-mono text-xs tabular-nums text-slate-300">
        {value}
      </div>
    </div>
  );
}

export function Spinner({ label = "Working…" }) {
  return (
    <div className="flex items-center gap-3 text-sm text-slate-400">
      <span className="h-3 w-3 animate-spin rounded-full border-2 border-slate-600 border-t-emerald-400" />
      {label}
    </div>
  );
}
