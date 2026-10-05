import { useEffect, useMemo, useState } from "react";
import { listLicenses, revokeLicense, deleteLicense } from "../api/client.js";
import { planLabel } from "../lib/plans.js";
import {
  fmtDateShort,
  licenseStatus,
  toCsv,
} from "../lib/format.js";
import { downloadBlob } from "../lib/clipboard.js";
import StatusBadge from "./StatusBadge.jsx";
import LicenseDetail from "./LicenseDetail.jsx";

export default function LicenseList() {
  const [licenses, setLicenses] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [query, setQuery] = useState("");
  const [selected, setSelected] = useState(null);
  const [revoking, setRevoking] = useState(null);
  const [deleting, setDeleting] = useState(null);

  async function load() {
    setLoading(true);
    setError("");
    try {
      const data = await listLicenses();
      setLicenses(Array.isArray(data) ? data : []);
    } catch (err) {
      setError(err.message || "Failed to load licenses");
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    load();
  }, []);

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    const rows = q
      ? licenses.filter((l) =>
          [
            l.license_key,
            l.customer_name,
            l.customer_email,
            l.organization_name,
            l.plan_type,
          ]
            .filter(Boolean)
            .some((v) => String(v).toLowerCase().includes(q))
        )
      : licenses;
    // Newest first.
    return [...rows].sort((a, b) => new Date(b.created_at) - new Date(a.created_at));
  }, [licenses, query]);

  async function handleRevoke(license, ev) {
    ev.stopPropagation();
    if (!window.confirm(`Revoke license for ${license.organization_name}? This deactivates it.`))
      return;
    setRevoking(license.license_key);
    try {
      await revokeLicense(license.license_key);
      setLicenses((prev) =>
        prev.map((l) =>
          l.license_key === license.license_key ? { ...l, is_active: false } : l
        )
      );
    } catch (err) {
      setError(err.message || "Failed to revoke");
    } finally {
      setRevoking(null);
    }
  }

  async function handleDelete(license, ev) {
    ev.stopPropagation();
    if (
      !window.confirm(
        `Permanently delete the license for ${license.organization_name}?\n\n` +
          `This removes ${license.license_key} from the database and cannot be undone.`
      )
    )
      return;
    setDeleting(license.license_key);
    try {
      await deleteLicense(license.license_key);
      setLicenses((prev) => prev.filter((l) => l.license_key !== license.license_key));
    } catch (err) {
      setError(err.message || "Failed to delete");
    } finally {
      setDeleting(null);
    }
  }

  function exportCsv() {
    const csv = toCsv(filtered, [
      { header: "License Key", value: (l) => l.license_key },
      { header: "Organization", value: (l) => l.organization_name },
      { header: "Customer", value: (l) => l.customer_name },
      { header: "Email", value: (l) => l.customer_email },
      { header: "Plan", value: (l) => l.plan_type },
      { header: "Status", value: (l) => licenseStatus(l) },
      { header: "Created", value: (l) => l.created_at },
      { header: "Expires", value: (l) => l.expires_at },
      { header: "Activated", value: (l) => l.activated_at || "" },
    ]);
    const stamp = new Date().toISOString().slice(0, 10);
    downloadBlob(`licenses_${stamp}.csv`, csv, "text/csv");
  }

  return (
    <>
      <div className="page-head">
        <h1>Licenses</h1>
        <p>View, search, export, and revoke issued licenses.</p>
      </div>

      {error && <div className="banner error">{error}</div>}

      <div className="toolbar">
        <input
          className="search"
          placeholder="Search key, customer, email, org, plan…"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
        />
        <span className="count">
          {filtered.length} of {licenses.length}
        </span>
        <div className="spacer" style={{ flex: 1 }} />
        <button onClick={load} disabled={loading}>
          Refresh
        </button>
        <button onClick={exportCsv} disabled={filtered.length === 0}>
          Export CSV
        </button>
      </div>

      {loading ? (
        <div className="spin">Loading licenses…</div>
      ) : filtered.length === 0 ? (
        <div className="empty">{licenses.length === 0 ? "No licenses yet." : "No matches."}</div>
      ) : (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>License Key</th>
                <th>Organization</th>
                <th>Customer</th>
                <th>Plan</th>
                <th>Status</th>
                <th>Expires</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {filtered.map((l) => {
                const status = licenseStatus(l);
                return (
                  <tr
                    key={l.license_key}
                    className={`clickable${status === "revoked" ? " revoked-row" : ""}`}
                    onClick={() => setSelected(l)}
                  >
                    <td className="mono">{l.license_key}</td>
                    <td>{l.organization_name}</td>
                    <td>
                      {l.customer_name}
                      <div className="note">{l.customer_email}</div>
                    </td>
                    <td>
                      <span className="badge plan">{planLabel(l.plan_type)}</span>
                    </td>
                    <td>
                      <StatusBadge license={l} />
                    </td>
                    <td>{fmtDateShort(l.expires_at)}</td>
                    <td style={{ textAlign: "right", whiteSpace: "nowrap" }}>
                      <button
                        className="danger sm"
                        disabled={!l.is_active || revoking === l.license_key}
                        title={l.is_active ? "Revoke (deactivate)" : "Already revoked"}
                        onClick={(e) => handleRevoke(l, e)}
                      >
                        {revoking === l.license_key ? "…" : "Revoke"}
                      </button>
                      <button
                        className="danger sm"
                        style={{ marginLeft: 6 }}
                        disabled={deleting === l.license_key}
                        title="Delete permanently (removes from database)"
                        onClick={(e) => handleDelete(l, e)}
                      >
                        {deleting === l.license_key ? "…" : "Delete"}
                      </button>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}

      {selected && (
        <LicenseDetail
          license={selected}
          onClose={() => setSelected(null)}
          onRevoked={(key) => {
            setLicenses((prev) =>
              prev.map((l) => (l.license_key === key ? { ...l, is_active: false } : l))
            );
            setSelected((s) => (s ? { ...s, is_active: false } : s));
          }}
          onDeleted={(key) => {
            setLicenses((prev) => prev.filter((l) => l.license_key !== key));
            setSelected(null);
          }}
        />
      )}
    </>
  );
}
