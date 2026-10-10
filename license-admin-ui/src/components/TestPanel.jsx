import { useRef, useState } from "react";
import { inspectOfflineFile, testActivate, testValidate } from "../api/client.js";
import { fmtDate } from "../lib/format.js";
import { fmtAssets, planLabel } from "../lib/plans.js";

// Sanity-check a license against the live server using the public client
// endpoints (no signature required for these two). Typical flow:
//   1. Activate with the license_key + an arbitrary fingerprint -> returns the
//      organization_token and binds the license to that fingerprint.
//   2. Validate with key + token + the SAME fingerprint -> valid.
// A different fingerprint will be rejected, demonstrating the VM binding.
// Check an offline file before it goes out or after it comes back: a customer's request
// (.ngcreq) - is it well formed, and would a license file be issued for it? - or a license file
// (.ngclic) - is it signed by this server, still valid, bound to the right machine, and in the
// issue record? Read-only on the server.
function OfflineFileCheck() {
  const fileRef = useRef(null);
  const [text, setText] = useState("");
  const [fileName, setFileName] = useState("");
  const [result, setResult] = useState(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  async function check(content) {
    setError("");
    setResult(null);
    setBusy(true);
    try {
      setResult(await inspectOfflineFile(content));
    } catch (err) {
      setError(err.message || "Check failed");
    } finally {
      setBusy(false);
    }
  }

  async function onFile(ev) {
    const file = ev.target.files?.[0];
    ev.target.value = "";
    if (!file) return;
    const content = await file.text();
    setText(content);
    setFileName(file.name);
    check(content);
  }

  const isRequest = result?.kind === "request";
  const rows = result
    ? [
        ["License key", result.license_key, "mono"],
        ["Machine fingerprint", result.vm_fingerprint, "mono"],
        isRequest ? ["Assets reported", result.asset_count ?? "—"] : null,
        result.plan_code ? ["Plan", planLabel(result.plan_code) || result.plan_code] : null,
        result.plan_code ? ["Asset limit", `${fmtAssets(result.max_assets)}${result.addon_packs ? ` (incl. ${result.addon_packs} add-on packs)` : ""}`] : null,
        [isRequest ? "Request made" : "File issued", result.created_at ? fmtDate(result.created_at) : "—"],
        !isRequest ? ["File valid until", result.valid_until ? fmtDate(result.valid_until) : "—"] : null,
        ["License expires", result.license_expires_at ? fmtDate(result.license_expires_at) : "—"],
        !isRequest ? ["Signing key", result.key_id || "—", "mono"] : null,
      ].filter(Boolean)
    : [];

  return (
    <div className="card offline-check">
      <h2>Check an offline file</h2>
      <p className="card-sub">
        A request file from a customer (.ngcreq) or a license file (.ngclic). Nothing is issued or
        changed — this only reads the file and compares it with this server.
      </p>
      {error && <div className="banner error">{error}</div>}

      <div className="field">
        <label htmlFor="t_off">File contents</label>
        <textarea
          id="t_off"
          value={text}
          onChange={(e) => { setText(e.target.value); setFileName(""); }}
          placeholder={"NGCORION-OFFLINE-REQUEST-1 …  or  NGCORION-OFFLINE-LICENSE-1 …"}
          spellCheck={false}
        />
        <div className="hint">Choose the file, or paste its two lines.{fileName && ` Loaded: ${fileName}`}</div>
      </div>
      <input ref={fileRef} type="file" accept=".ngcreq,.ngclic,.txt" onChange={onFile} style={{ display: "none" }} />
      <div style={{ display: "flex", gap: 10 }}>
        <button disabled={busy} onClick={() => fileRef.current?.click()}>Choose file…</button>
        <button className="primary" disabled={busy || !text.trim()} onClick={() => check(text)}>
          {busy ? "Checking…" : "Check"}
        </button>
      </div>

      {result && (
        <div style={{ borderTop: "1px solid var(--border)", marginTop: 22, paddingTop: 18 }}>
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", gap: 12 }}>
            <h2 style={{ margin: 0 }}>{isRequest ? "Offline request" : "Offline license file"}</h2>
            <span className={`badge ${result.ok ? "active" : "revoked"}`}>
              {result.ok ? (isRequest ? "Can be issued" : "Valid") : (isRequest ? "Cannot be issued" : "Problem found")}
            </span>
          </div>
          <dl className="kv">
            {rows.map(([k, v, cls]) => (
              <div key={k} style={{ display: "contents" }}>
                <dt>{k}</dt>
                <dd className={cls}>{v ?? "—"}</dd>
              </div>
            ))}
          </dl>
          <ul className="checks">
            {result.checks.map((c, i) => (
              <li key={i} className={c.ok ? "ok" : "bad"}>
                <span className="mark" aria-label={c.ok ? "passed" : "failed"}>{c.ok ? "✓" : "✕"}</span>
                <span>{c.text}</span>
              </li>
            ))}
          </ul>
          {result.claims && (
            <details>
              <summary>All signed claims</summary>
              <pre className="codeblock">{JSON.stringify(result.claims, null, 2)}</pre>
            </details>
          )}
        </div>
      )}
    </div>
  );
}

export default function TestPanel() {
  const [licenseKey, setLicenseKey] = useState("");
  const [orgToken, setOrgToken] = useState("");
  const [fingerprint, setFingerprint] = useState("");
  const [result, setResult] = useState(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  function randomFingerprint() {
    const hex = Array.from({ length: 64 }, () =>
      Math.floor(Math.random() * 16).toString(16)
    ).join("");
    setFingerprint(hex);
  }

  async function run(kind) {
    setError("");
    setResult(null);
    setBusy(true);
    try {
      if (kind === "activate") {
        const r = await testActivate(licenseKey.trim(), fingerprint.trim());
        if (r?.organization_token) setOrgToken(r.organization_token);
        setResult({ kind, data: r });
      } else {
        const r = await testValidate(licenseKey.trim(), orgToken.trim(), fingerprint.trim());
        setResult({ kind, data: r });
      }
    } catch (err) {
      setError(err.message || "Request failed");
    } finally {
      setBusy(false);
    }
  }

  const canActivate = licenseKey.trim() && fingerprint.trim() && !busy;
  const canValidate = licenseKey.trim() && orgToken.trim() && fingerprint.trim() && !busy;

  return (
    <>
      <div className="page-head">
        <h1>Test a license</h1>
        <p>Exercise the public activate / validate endpoints to verify a generated license, and check offline files.</p>
      </div>

      <div className="banner info">
        Activate first with any fingerprint to bind the license and receive its organization token,
        then validate with the <strong>same</strong> fingerprint. A different fingerprint is
        rejected — that's the one-VM binding at work.
      </div>

      <div className="card">
        {error && <div className="banner error">{error}</div>}

        <div className="field">
          <label htmlFor="t_key">License key</label>
          <input
            id="t_key"
            className="mono"
            value={licenseKey}
            onChange={(e) => setLicenseKey(e.target.value)}
            placeholder="XXXX-XXXX-XXXX-XXXX"
          />
        </div>

        <div className="field">
          <label htmlFor="t_fp">VM fingerprint</label>
          <div style={{ display: "flex", gap: 8 }}>
            <input
              id="t_fp"
              className="mono"
              value={fingerprint}
              onChange={(e) => setFingerprint(e.target.value)}
              placeholder="64-char hex (any value for testing)"
            />
            <button type="button" onClick={randomFingerprint} style={{ whiteSpace: "nowrap" }}>
              Random
            </button>
          </div>
          <div className="hint">Real clients derive this from hardware; for testing any value works.</div>
        </div>

        <div className="field">
          <label htmlFor="t_tok">Organization token</label>
          <input
            id="t_tok"
            className="mono"
            value={orgToken}
            onChange={(e) => setOrgToken(e.target.value)}
            placeholder="Returned by activate, or paste a known token"
          />
          <div className="hint">Required for validate. Auto-filled after a successful activate.</div>
        </div>

        <div style={{ display: "flex", gap: 10, marginTop: 8 }}>
          <button className="primary" disabled={!canActivate} onClick={() => run("activate")}>
            Activate
          </button>
          <button disabled={!canValidate} onClick={() => run("validate")}>
            Validate
          </button>
        </div>
      </div>

      {result && (
        <div className="card">
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 10 }}>
            <h2 style={{ margin: 0, textTransform: "capitalize" }}>{result.kind} result</h2>
            <span className={`badge ${result.data?.valid === false ? "revoked" : "active"}`}>
              {result.data?.valid === false ? "Invalid" : "OK"}
            </span>
          </div>
          {result.data?.message && <p className="note">{result.data.message}</p>}
          <pre className="codeblock">{JSON.stringify(result.data, null, 2)}</pre>
        </div>
      )}

      <OfflineFileCheck />
    </>
  );
}
