import { useState } from "react";
import { testActivate, testValidate } from "../api/client.js";

// Sanity-check a license against the live server using the public client
// endpoints (no signature required for these two). Typical flow:
//   1. Activate with the license_key + an arbitrary fingerprint -> returns the
//      organization_token and binds the license to that fingerprint.
//   2. Validate with key + token + the SAME fingerprint -> valid.
// A different fingerprint will be rejected, demonstrating the VM binding.
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
        <p>Exercise the public activate / validate endpoints to verify a generated license.</p>
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
    </>
  );
}
