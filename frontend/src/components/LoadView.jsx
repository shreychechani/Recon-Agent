import { useState } from "react";
import { api } from "../api.js";
import { Panel, Spinner } from "./ui.jsx";

const SAMPLES = [
  { name: "sample", title: "Seed sample", desc: "61 credits — quick end-to-end look" },
  { name: "train", title: "Train set", desc: "800 credits — the tuning set" },
  { name: "holdout", title: "Holdout set", desc: "800 credits — never tuned on" },
  {
    name: "learn_demo",
    title: "Learning-loop demo",
    desc: "22 credits — a novel reference format; resolve one, learn the rest",
    accent: true,
  },
  {
    name: "razorpay",
    title: "Razorpay API data",
    desc: "pulled from the Settlement Recon Report API — real settlement batches",
    accent: true,
  },
];

export default function LoadView({ onRun }) {
  const [busy, setBusy] = useState(null);
  const [err, setErr] = useState(null);

  const loadSample = async (name) => {
    setErr(null);
    setBusy(name);
    try {
      onRun(await api.reconcileSample(name));
    } catch (e) {
      setErr(String(e.message || e));
    } finally {
      setBusy(null);
    }
  };

  return (
    <div className="grid gap-5 md:grid-cols-[1.4fr_1fr]">
      <Panel title="Reconcile a bundled dataset">
        <div className="grid gap-3 sm:grid-cols-2">
          {SAMPLES.map((s) => (
            <button
              key={s.name}
              disabled={!!busy}
              onClick={() => loadSample(s.name)}
              className={`group rounded-xl border p-4 text-left transition ${
                s.accent
                  ? "border-emerald-500/25 bg-emerald-500/[0.04] hover:border-emerald-500/50"
                  : "border-white/5 bg-[#0b111a] hover:border-white/15"
              } ${busy ? "opacity-60" : ""}`}
            >
              <div className="flex items-center justify-between">
                <div className="font-medium text-slate-100">{s.title}</div>
                {busy === s.name ? (
                  <Spinner label="" />
                ) : (
                  <span className="text-slate-600 transition group-hover:text-slate-300">
                    →
                  </span>
                )}
              </div>
              <div className="mt-1 text-xs text-slate-500">{s.desc}</div>
            </button>
          ))}
        </div>
        {err && (
          <div className="mt-4 rounded-lg border border-rose-500/20 bg-rose-500/5 px-3 py-2 text-sm text-rose-300">
            {err}
          </div>
        )}
      </Panel>

      <UploadPanel onRun={onRun} setErr={setErr} />
    </div>
  );
}

function UploadPanel({ onRun, setErr }) {
  const [busy, setBusy] = useState(false);
  const [files, setFiles] = useState({});

  const submit = async (e) => {
    e.preventDefault();
    setErr(null);
    if (!files.settlements || !files.bank) {
      setErr("settlements.csv and bank.json are required");
      return;
    }
    const form = new FormData();
    form.append("settlements", files.settlements);
    form.append("bank", files.bank);
    if (files.orders) form.append("orders", files.orders);
    if (files.ground_truth) form.append("ground_truth", files.ground_truth);
    setBusy(true);
    try {
      onRun(await api.reconcileUpload(form));
    } catch (e2) {
      setErr(String(e2.message || e2));
    } finally {
      setBusy(false);
    }
  };

  const Field = ({ name, label, required }) => (
    <label className="block">
      <div className="mb-1 text-xs text-slate-400">
        {label} {required && <span className="text-rose-400">*</span>}
      </div>
      <input
        type="file"
        onChange={(e) => setFiles((f) => ({ ...f, [name]: e.target.files[0] }))}
        className="block w-full text-xs text-slate-400 file:mr-3 file:rounded-md file:border-0 file:bg-white/5 file:px-3 file:py-1.5 file:text-slate-200 hover:file:bg-white/10"
      />
    </label>
  );

  return (
    <Panel title="…or upload your own files">
      <form onSubmit={submit} className="space-y-3">
        <Field name="settlements" label="settlements.csv" required />
        <Field name="bank" label="bank.json" required />
        <Field name="orders" label="orders.xlsx (optional)" />
        <Field name="ground_truth" label="ground_truth.json (optional — enables precision)" />
        <button
          disabled={busy}
          className="mt-1 w-full rounded-lg bg-emerald-500/90 px-3 py-2 text-sm font-medium text-emerald-950 transition hover:bg-emerald-400 disabled:opacity-60"
        >
          {busy ? "Reconciling…" : "Reconcile"}
        </button>
      </form>
    </Panel>
  );
}
