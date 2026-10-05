import { useState } from "react";
import { copyText, downloadBlob } from "../lib/clipboard.js";
import { planLabel } from "../lib/plans.js";
import { fmtDate } from "../lib/format.js";

// Shows the two secrets the customer needs (license_key + organization_token)
// with one-click copy and a download. The organization_token is only returned
// at creation time, so this card is the moment to capture it.
export default function CredentialCard({ license }) {
  const credentials = {
    license_key: license.license_key,
    organization_token: license.organization_token,
    customer_name: license.customer_name,
    customer_email: license.customer_email,
    organization_name: license.organization_name,
    plan_type: license.plan_type,
    expires_at: license.expires_at,
  };

  function downloadJson() {
    const name = (license.organization_name || "license").replace(/[^a-z0-9]+/gi, "_").toLowerCase();
    downloadBlob(`${name}_license.json`, JSON.stringify(credentials, null, 2), "application/json");
  }

  function downloadTxt() {
    const name = (license.organization_name || "license").replace(/[^a-z0-9]+/gi, "_").toLowerCase();
    const txt = [
      "LICENSE CREDENTIALS",
      "===================",
      `Organization : ${license.organization_name}`,
      `Customer     : ${license.customer_name} <${license.customer_email}>`,
      `Plan         : ${planLabel(license.plan_type)}`,
      `Expires      : ${fmtDate(license.expires_at)}`,
      "",
      `License Key        : ${license.license_key}`,
      `Organization Token : ${license.organization_token}`,
      "",
      "Keep the organization token secret — it is the HMAC signing key for this license.",
    ].join("\n");
    downloadBlob(`${name}_license.txt`, txt, "text/plain");
  }

  function copyAll() {
    copyText(
      `License Key: ${license.license_key}\nOrganization Token: ${license.organization_token}`
    );
  }

  return (
    <div className="card">
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 4 }}>
        <h2 style={{ margin: 0 }}>Credentials</h2>
        <span className="badge plan">{planLabel(license.plan_type)}</span>
      </div>
      <p className="card-sub">
        For {license.organization_name} · {license.customer_email} · expires {fmtDate(license.expires_at)}
      </p>

      <div className="banner warn">
        Store the <strong>organization token</strong> now — it is the license's signing secret and is
        not shown again after you leave this screen.
      </div>

      <CopyRow label="License Key" value={license.license_key} />
      <div style={{ height: 10 }} />
      <CopyRow label="Org Token" value={license.organization_token} />

      <div style={{ display: "flex", gap: 10, marginTop: 18, flexWrap: "wrap" }}>
        <button onClick={copyAll}>Copy both</button>
        <button onClick={downloadJson}>Download .json</button>
        <button onClick={downloadTxt}>Download .txt</button>
      </div>
    </div>
  );
}

export function CopyRow({ label, value }) {
  const [copied, setCopied] = useState(false);
  async function copy() {
    const ok = await copyText(value);
    if (ok) {
      setCopied(true);
      setTimeout(() => setCopied(false), 1400);
    }
  }
  return (
    <div className="copyrow">
      {label && <span className="label">{label}</span>}
      <code>{value}</code>
      <button className="sm" onClick={copy}>
        {copied ? "Copied" : "Copy"}
      </button>
    </div>
  );
}
