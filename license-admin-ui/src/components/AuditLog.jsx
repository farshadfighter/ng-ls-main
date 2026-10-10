import { useCallback, useEffect, useState } from "react";
import { exportAudit, searchAudit } from "../api/client.js";
import { downloadBlob } from "../lib/clipboard.js";
import { fmtDate } from "../lib/format.js";

const AREAS = [
  { id: "", label: "All actions" },
  { id: "auth", label: "Sign-in" },
  { id: "user", label: "Accounts" },
  { id: "license", label: "Licenses" },
  { id: "client", label: "NGCorion installations" },
  { id: "portal", label: "Portal" },
  { id: "audit", label: "Audit exports" },
  { id: "system", label: "System" },
];
const PAGE = 100;

// The audit trail (auditor and admin roles): read-only - entries cannot be changed or
// deleted, not even in the database.
export default function AuditLog() {
  const [filters, setFilters] = useState({ actor: "", action: "", outcome: "", target: "", since: "", until: "" });
  const [applied, setApplied] = useState(filters);
  const [page, setPage] = useState(0);
  const [data, setData] = useState({ total: 0, items: [] });
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);

  const params = useCallback((f) => ({
    ...f,
    since: f.since ? new Date(f.since).toISOString() : "",
    until: f.until ? new Date(f.until).toISOString() : "",
  }), []);

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      setData(await searchAudit({ ...params(applied), limit: PAGE, offset: page * PAGE }));
    } catch (err) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  }, [applied, page, params]);

  useEffect(() => {
    load();
  }, [load]);

  async function download() {
    try {
      downloadBlob("audit.csv", await exportAudit(params(applied)), "text/csv");
    } catch (err) {
      setError(err.message);
    }
  }

  const set = (k) => (e) => setFilters({ ...filters, [k]: e.target.value });
  const pages = Math.max(1, Math.ceil(data.total / PAGE));

  return (
    <>
      <div className="page-head">
        <h1>Audit trail</h1>
        <p>Who did what, to what, from where and when. Entries cannot be changed or deleted.</p>
      </div>
      {error && <div className="banner error">{error}</div>}

      <div className="card">
        <form className="audit-filters" onSubmit={(e) => { e.preventDefault(); setPage(0); setApplied(filters); }}>
          <select value={filters.action} onChange={set("action")} aria-label="Action">
            {AREAS.map((a) => <option key={a.id} value={a.id}>{a.label}</option>)}
          </select>
          <select value={filters.outcome} onChange={set("outcome")} aria-label="Outcome">
            <option value="">Any outcome</option>
            <option value="success">Success</option>
            <option value="failure">Failure</option>
          </select>
          <input placeholder="Actor (user name)" value={filters.actor} onChange={set("actor")} />
          <input placeholder="Target (license key, user)" value={filters.target} onChange={set("target")} />
          <input type="datetime-local" value={filters.since} onChange={set("since")} aria-label="From" />
          <input type="datetime-local" value={filters.until} onChange={set("until")} aria-label="Until" />
          <button className="primary" type="submit">Search</button>
          <button type="button" onClick={download} disabled={!data.total}>Export CSV</button>
        </form>
      </div>

      <div className="card">
        <div className="toolbar">
          <span className="count">{loading ? "Loading…" : `${data.total.toLocaleString("en-US")} entries`}</span>
        </div>
        <div className="table-wrap">
          <table>
            <thead>
              <tr><th>When</th><th>Actor</th><th>Action</th><th>Target</th><th>Outcome</th><th>From</th><th>Detail</th></tr>
            </thead>
            <tbody>
              {data.items.map((e) => (
                <tr key={e.id}>
                  <td style={{ whiteSpace: "nowrap" }}>{fmtDate(e.at)}</td>
                  <td>
                    <span className="mono">{e.actor}</span>
                    {e.actor_role && <div className="note">{e.actor_role}</div>}
                  </td>
                  <td className="mono">{e.action}</td>
                  <td className="mono" style={{ fontSize: 12 }}>{e.target || "—"}</td>
                  <td>
                    <span className={`badge ${e.outcome === "success" ? "active" : "revoked"}`}>{e.outcome}</span>
                  </td>
                  <td className="mono" style={{ fontSize: 12 }}>{e.ip || "—"}</td>
                  <td className="mono audit-detail">{e.detail ? JSON.stringify(e.detail) : ""}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        {pages > 1 && (
          <div style={{ display: "flex", gap: 10, alignItems: "center", marginTop: 14 }}>
            <button className="sm" disabled={page === 0} onClick={() => setPage(page - 1)}>Newer</button>
            <span className="note">Page {page + 1} of {pages}</span>
            <button className="sm" disabled={page + 1 >= pages} onClick={() => setPage(page + 1)}>Older</button>
          </div>
        )}
      </div>
    </>
  );
}
