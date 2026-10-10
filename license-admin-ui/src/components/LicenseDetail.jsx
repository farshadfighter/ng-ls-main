import { useEffect, useRef, useState } from "react";
import {
  revokeLicense, deleteLicense, resetBinding, changePlan, setOffline, issueOfflineFile, listOfflineIssues,
} from "../api/client.js";
import { downloadBlob } from "../lib/clipboard.js";
import { ADDON, PLANS, PLAN_BY_CODE, fmtAssets, licensePlanLabel, maxAssets, planLabel } from "../lib/plans.js";
import { fmtDate, relativeTime, isExpired } from "../lib/format.js";
import UsageBars from "./UsageBars.jsx";
import StatusBadge from "./StatusBadge.jsx";
import { CopyRow } from "./CredentialCard.jsx";

// Detail modal for a single license: identity, status, usage bars, expiry
// countdown, timestamps and the bound VM fingerprint. Revoke lives here too.
// Reactivation is intentionally disabled — the admin API exposes no such
// endpoint (DELETE only sets is_active=False).
export default function LicenseDetail({ license, onClose, onRevoked, onDeleted, onUpdated }) {
  const [revoking, setRevoking] = useState(false);
  const [releasing, setReleasing] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [error, setError] = useState("");
  const expired = isExpired(license);

  async function handleRevoke() {
    if (!window.confirm(`Revoke license for ${license.organization_name}?`)) return;
    setRevoking(true);
    setError("");
    try {
      await revokeLicense(license.license_key);
      onRevoked?.(license.license_key);
    } catch (err) {
      setError(err.message || "Failed to revoke");
    } finally {
      setRevoking(false);
    }
  }

  // The license moves to another machine on the customer's next activation;
  // the machine it was bound to is refused from its next validation on.
  async function handleRelease() {
    if (
      !window.confirm(
        `Release ${license.license_key} from its machine?\n\n` +
          `The current server stops being licensed at its next check, and the license ` +
          `can then be activated on another (or a reinstalled) server.`
      )
    )
      return;
    setReleasing(true);
    setError("");
    try {
      const updated = await resetBinding(license.license_key);
      onUpdated?.(updated);
    } catch (err) {
      setError(err.message || "Failed to release the machine binding");
    } finally {
      setReleasing(false);
    }
  }

  async function handleDelete() {
    if (
      !window.confirm(
        `Permanently delete the license for ${license.organization_name}?\n\n` +
          `This removes ${license.license_key} from the database and cannot be undone.`
      )
    )
      return;
    setDeleting(true);
    setError("");
    try {
      await deleteLicense(license.license_key);
      onDeleted?.(license.license_key);
    } catch (err) {
      setError(err.message || "Failed to delete");
      setDeleting(false);
    }
  }

  return (
    <div className="overlay" onClick={onClose}>
      <div className="modal" onClick={(e) => e.stopPropagation()}>
        <div className="modal-head">
          <h3>{license.organization_name}</h3>
          <button className="ghost" onClick={onClose}>
            ✕
          </button>
        </div>

        <div className="modal-body">
          {error && <div className="banner error">{error}</div>}

          <div style={{ display: "flex", gap: 8, marginBottom: 16, flexWrap: "wrap" }}>
            <StatusBadge license={license} />
            <span className="badge plan">{licensePlanLabel(license)}</span>
            {license.is_pilot_mode && <span className="badge pilot">Pilot mode</span>}
            {license.offline_allowed && <span className="badge offline">Offline</span>}
          </div>

          <CopyRow label="License Key" value={license.license_key} />
          <div style={{ height: 10 }} />
          <CopyRow label="Org Token" value={license.organization_token} />

          <h4 style={{ margin: "22px 0 12px" }}>Assets</h4>
          <UsageBars license={license} />

          <PlanEditor license={license} onUpdated={onUpdated} />

          <OfflineSection license={license} onUpdated={onUpdated} />

          <h4 style={{ margin: "22px 0 10px" }}>Details</h4>
          <dl className="kv">
            <dt>Customer</dt>
            <dd>
              {license.customer_name} &lt;{license.customer_email}&gt;
            </dd>
            <dt>Created</dt>
            <dd>{fmtDate(license.created_at)}</dd>
            <dt>Activated</dt>
            <dd>{license.activated_at ? fmtDate(license.activated_at) : "Not activated"}</dd>
            <dt>Expires</dt>
            <dd>
              {fmtDate(license.expires_at)}{" "}
              <span className={expired ? "badge expired" : "note"} style={{ marginLeft: 6 }}>
                {relativeTime(license.expires_at)}
              </span>
            </dd>
            <dt>Last validated</dt>
            <dd>{license.last_validated_at ? fmtDate(license.last_validated_at) : "—"}</dd>
            <dt>Last heartbeat</dt>
            <dd>
              {license.last_heartbeat_at ? fmtDate(license.last_heartbeat_at) : "—"}
              {license.last_heartbeat_at && (
                <span className="note"> ({relativeTime(license.last_heartbeat_at)})</span>
              )}
            </dd>
            <dt>Bound VM fingerprint</dt>
            <dd className="mono" style={{ fontSize: 12 }}>
              {license.vm_fingerprint || "Not bound yet"}
            </dd>
          </dl>
        </div>

        <div className="modal-foot">
          <button
            disabled
            title="The license server API has no reactivate endpoint (DELETE only deactivates). Reactivation is not supported."
          >
            Reactivate
          </button>
          <button
            onClick={handleRelease}
            disabled={!license.vm_fingerprint || releasing || revoking || deleting}
            title={license.vm_fingerprint ? "Release the license from its machine" : "Not bound to a machine"}
          >
            {releasing ? "Releasing…" : "Release machine"}
          </button>
          <button
            className="danger"
            onClick={handleRevoke}
            disabled={!license.is_active || revoking || deleting}
            title={license.is_active ? "Revoke (deactivate)" : "Already revoked"}
          >
            {revoking ? "Revoking…" : license.is_active ? "Revoke" : "Revoked"}
          </button>
          <button
            className="danger"
            onClick={handleDelete}
            disabled={revoking || deleting}
            title="Delete permanently (removes from database)"
          >
            {deleting ? "Deleting…" : "Delete"}
          </button>
        </div>
      </div>
    </div>
  );
}

// Upgrade, add-ons bought later, or putting back the plan of a license the old
// server downgraded for being offline. NGCorion receives the new limit with its
// next validation or heartbeat; the term does not change.
function PlanEditor({ license, onUpdated }) {
  const [planCode, setPlanCode] = useState(license.plan_code);
  const [packs, setPacks] = useState(String(license.addon_packs ?? 0));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const plan = PLAN_BY_CODE[planCode];
  const packCount = plan?.addonsAllowed ? Number(packs) : 0;
  const validPacks = Number.isInteger(packCount) && packCount >= 0 && packCount <= ADDON.maxPacks;
  const changed = planCode !== license.plan_code || packCount !== (license.addon_packs ?? 0);

  async function save() {
    if (!window.confirm(
      `Set ${license.license_key} to ${planLabel(planCode)}` +
        (packCount ? ` + ${packCount} add-on pack(s)` : "") +
        ` (asset limit ${fmtAssets(maxAssets(plan, packCount))})?`
    )) return;
    setBusy(true);
    setError("");
    try {
      onUpdated?.(await changePlan(license.license_key, { plan_code: planCode, addon_packs: packCount }));
    } catch (err) {
      setError(err.message || "Failed to change the plan");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="plan-editor">
      <h4 style={{ margin: "22px 0 10px" }}>Plan &amp; add-ons</h4>
      {error && <div className="banner error">{error}</div>}
      <div className="grid-2">
        <div className="field">
          <label htmlFor="edit_plan">Plan</label>
          <select id="edit_plan" value={planCode} onChange={(e) => setPlanCode(e.target.value)}>
            {PLANS.map((p) => (
              <option key={p.code} value={p.code}>
                {planLabel(p.code)} — {p.code}
              </option>
            ))}
            {!PLAN_BY_CODE[license.plan_code] && (
              <option value={license.plan_code}>{license.plan_code} (no longer sold)</option>
            )}
          </select>
        </div>
        <div className="field">
          <label htmlFor="edit_packs">Add-on packs ({ADDON.assets} assets each)</label>
          <input
            id="edit_packs"
            type="number"
            min="0"
            max={ADDON.maxPacks}
            value={plan?.addonsAllowed ? packs : 0}
            disabled={!plan?.addonsAllowed}
            onChange={(e) => setPacks(e.target.value)}
          />
        </div>
      </div>
      <div style={{ display: "flex", alignItems: "center", gap: 12, flexWrap: "wrap" }}>
        <button className="primary" onClick={save} disabled={!changed || !validPacks || busy}>
          {busy ? "Saving…" : "Save plan"}
        </button>
        <span className="note">
          New asset limit: <strong>{validPacks ? fmtAssets(maxAssets(plan, packCount)) : "—"}</strong>
        </span>
      </div>
    </div>
  );
}

// Offline (air-gapped) license: the customer's NGCorion never reaches this server. It makes a
// request file (License page → offline activation); this signs a license file for it, bound to
// that machine and valid one year (never past the license's expiry). A file cannot be revoked
// before it ends, so the list of issued files is the record of what is out there.
function OfflineSection({ license, onUpdated }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [requestText, setRequestText] = useState("");
  const [issued, setIssued] = useState(null);
  const [issues, setIssues] = useState([]);
  const fileRef = useRef(null);

  useEffect(() => {
    if (!license.offline_allowed) return;
    listOfflineIssues(license.license_key).then(setIssues).catch(() => setIssues([]));
  }, [license.license_key, license.offline_allowed, issued]);

  async function toggle() {
    const next = !license.offline_allowed;
    if (!next && !window.confirm(
      "Turn the offline option off?\n\nNo new license files can be issued. Files already issued keep " +
        "working until their end date."
    )) return;
    setBusy(true);
    setError("");
    try {
      onUpdated?.(await setOffline(license.license_key, next));
    } catch (err) {
      setError(err.message || "Failed to change the offline option");
    } finally {
      setBusy(false);
    }
  }

  async function readFile(ev) {
    const file = ev.target.files?.[0];
    if (file) setRequestText(await file.text());
    ev.target.value = "";
  }

  async function issue() {
    setBusy(true);
    setError("");
    setIssued(null);
    try {
      const result = await issueOfflineFile(license.license_key, requestText.trim());
      setIssued(result);
      setRequestText("");
      downloadBlob(result.filename, result.content, "text/plain");
      onUpdated?.(result.license);
    } catch (err) {
      setError(err.message || "Failed to issue the license file");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="offline-box">
      <h4 style={{ margin: "22px 0 10px" }}>Offline license</h4>
      {error && <div className="banner error">{error}</div>}
      <label className="checkline">
        <input type="checkbox" checked={!!license.offline_allowed} disabled={busy} onChange={toggle} />
        <span>
          <strong>Offline (air-gapped)</strong> — runs on a signed license file instead of activation and
          heartbeats.
        </span>
      </label>

      {license.offline_allowed && (
        <>
          {license.offline_valid_until && (
            <p className="note" style={{ margin: "0 0 12px" }}>
              Current file valid until <strong>{fmtDate(license.offline_valid_until)}</strong>{" "}
              ({relativeTime(license.offline_valid_until)}).
            </p>
          )}
          <p className="note" style={{ margin: "0 0 8px" }}>
            Upload or paste the request file the customer made on NGCorion's License page. The license
            file you get back is bound to that machine and valid one year (never past the license's
            expiry).
          </p>
          <textarea
            value={requestText}
            onChange={(e) => setRequestText(e.target.value)}
            placeholder="NGCORION-OFFLINE-REQUEST-1 …"
            spellCheck={false}
          />
          <div style={{ display: "flex", gap: 10, alignItems: "center", flexWrap: "wrap", marginTop: 10 }}>
            <input ref={fileRef} type="file" accept=".ngcreq,.txt" onChange={readFile} style={{ display: "none" }} />
            <button onClick={() => fileRef.current?.click()} disabled={busy}>Upload request file…</button>
            <button className="primary" onClick={issue} disabled={busy || !requestText.trim() || !license.is_active}>
              {busy ? "Issuing…" : "Issue license file"}
            </button>
          </div>
          {issued && (
            <div className="banner success" style={{ marginTop: 12 }}>
              {issued.filename} downloaded — valid until {fmtDate(issued.valid_until)}. Give it to the customer
              to upload on NGCorion's License page.
              <button className="sm" style={{ marginInlineStart: 10 }}
                      onClick={() => downloadBlob(issued.filename, issued.content, "text/plain")}>
                Download again
              </button>
            </div>
          )}
          {issues.length > 0 && (
            <div className="table-wrap" style={{ marginTop: 14 }}>
              <table>
                <thead>
                  <tr><th>Issued</th><th>Valid until</th><th>By</th><th className="num">Assets</th><th>Machine</th></tr>
                </thead>
                <tbody>
                  {issues.map((i) => (
                    <tr key={i.issued_at}>
                      <td>{fmtDate(i.issued_at)}</td>
                      <td>{fmtDate(i.valid_until)}</td>
                      <td>{i.issued_by || "—"}</td>
                      <td className="num">{i.reported_assets ?? "—"}</td>
                      <td className="mono" style={{ fontSize: 11 }}>{i.vm_fingerprint.slice(0, 16)}…</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </>
      )}
    </div>
  );
}
