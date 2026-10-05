import { useState } from "react";
import { revokeLicense, deleteLicense } from "../api/client.js";
import { planLabel } from "../lib/plans.js";
import { fmtDate, relativeTime, isExpired } from "../lib/format.js";
import UsageBars from "./UsageBars.jsx";
import StatusBadge from "./StatusBadge.jsx";
import { CopyRow } from "./CredentialCard.jsx";

// Detail modal for a single license: identity, status, usage bars, expiry
// countdown, timestamps and the bound VM fingerprint. Revoke lives here too.
// Reactivation is intentionally disabled — the admin API exposes no such
// endpoint (DELETE only sets is_active=False).
export default function LicenseDetail({ license, onClose, onRevoked, onDeleted }) {
  const [revoking, setRevoking] = useState(false);
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
            <span className="badge plan">{planLabel(license.plan_type)}</span>
            {license.is_pilot_mode && <span className="badge pilot">Pilot mode</span>}
          </div>

          <CopyRow label="License Key" value={license.license_key} />
          <div style={{ height: 10 }} />
          <CopyRow label="Org Token" value={license.organization_token} />

          <h4 style={{ margin: "22px 0 12px" }}>Usage</h4>
          <UsageBars license={license} />

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
