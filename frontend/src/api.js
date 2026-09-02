// Thin fetch wrapper. Same-origin in production (FastAPI serves the build);
// proxied to :8000 in dev via vite.config.js.

async function j(res) {
  if (!res.ok) throw new Error((await res.text()) || res.statusText);
  return res.json();
}

export const api = {
  reconcileSample: (name) =>
    fetch("/api/reconcile/sample", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name }),
    }).then(j),

  reconcileUpload: (form) =>
    fetch("/api/reconcile", { method: "POST", body: form }).then(j),

  metrics: (runId) => fetch(`/api/run/${runId}/metrics`).then(j),

  exceptions: (runId) => fetch(`/api/run/${runId}/exceptions`).then(j),

  resolve: (runId, txnId, body) =>
    fetch(`/api/run/${runId}/exception/${txnId}/resolve`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }).then(j),

  rerun: (runId) => fetch(`/api/run/${runId}/rerun`, { method: "POST" }).then(j),

  rules: () => fetch("/api/rules").then(j),
  clearRules: () => fetch("/api/rules", { method: "DELETE" }).then(j),
  health: () => fetch("/api/health").then(j),
};
