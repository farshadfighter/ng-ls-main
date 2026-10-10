import { useState } from "react";
import { createLicense } from "../api/client.js";
import { ADDON, PLANS, PLAN_BY_CODE, fmtAssets, maxAssets, planLabel } from "../lib/plans.js";
import CredentialCard from "./CredentialCard.jsx";

const EMPTY = {
  customer_name: "",
  customer_email: "",
  organization_name: "",
  plan_code: "NGC-PILOT-5-1M",
  addon_packs: 0,
  offline_allowed: false,
};

const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;

export default function GenerateLicense() {
  const [form, setForm] = useState(EMPTY);
  const [errors, setErrors] = useState({});
  const [serverError, setServerError] = useState("");
  const [busy, setBusy] = useState(false);
  const [created, setCreated] = useState(null);

  const plan = PLAN_BY_CODE[form.plan_code];

  function set(field, value) {
    setForm((f) => ({ ...f, [field]: value }));
    setErrors((e) => ({ ...e, [field]: undefined }));
  }

  function validate() {
    const e = {};
    if (!form.customer_name.trim()) e.customer_name = "Required";
    if (!form.organization_name.trim()) e.organization_name = "Required";
    if (!form.customer_email.trim()) e.customer_email = "Required";
    else if (!EMAIL_RE.test(form.customer_email.trim())) e.customer_email = "Enter a valid email";
    const packs = Number(form.addon_packs);
    if (!Number.isInteger(packs) || packs < 0 || packs > ADDON.maxPacks)
      e.addon_packs = `A whole number from 0 to ${ADDON.maxPacks}`;
    setErrors(e);
    return Object.keys(e).length === 0;
  }

  async function handleSubmit(ev) {
    ev.preventDefault();
    setServerError("");
    if (!validate()) return;
    setBusy(true);
    try {
      const license = await createLicense({
        customer_name: form.customer_name.trim(),
        customer_email: form.customer_email.trim(),
        organization_name: form.organization_name.trim(),
        plan_code: form.plan_code,
        addon_packs: plan?.addonsAllowed ? Number(form.addon_packs) : 0,
        offline_allowed: form.offline_allowed,
      });
      setCreated(license);
    } catch (err) {
      setServerError(err.message || "Failed to create license");
    } finally {
      setBusy(false);
    }
  }

  function reset() {
    setForm(EMPTY);
    setErrors({});
    setServerError("");
    setCreated(null);
  }

  if (created) {
    return (
      <>
        <div className="page-head">
          <h1>License created</h1>
          <p>Hand the credentials below to the customer. The organization token is shown once here.</p>
        </div>
        <CredentialCard license={created} />
        <div style={{ marginTop: 18 }}>
          <button className="primary" onClick={reset}>
            Generate another
          </button>
        </div>
      </>
    );
  }

  return (
    <>
      <div className="page-head">
        <h1>Generate license</h1>
        <p>Create a new license token for a customer. Limits and duration are set by the plan.</p>
      </div>

      <div className="card">
        {serverError && <div className="banner error">{serverError}</div>}
        <form onSubmit={handleSubmit}>
          <div className="grid-2">
            <div className="field">
              <label htmlFor="customer_name">Customer name</label>
              <input
                id="customer_name"
                value={form.customer_name}
                onChange={(e) => set("customer_name", e.target.value)}
                placeholder="Jane Doe"
              />
              {errors.customer_name && <div className="err">{errors.customer_name}</div>}
            </div>
            <div className="field">
              <label htmlFor="customer_email">Customer email</label>
              <input
                id="customer_email"
                type="email"
                value={form.customer_email}
                onChange={(e) => set("customer_email", e.target.value)}
                placeholder="jane@acme.com"
              />
              {errors.customer_email && <div className="err">{errors.customer_email}</div>}
            </div>
          </div>

          <div className="field">
            <label htmlFor="organization_name">Organization name</label>
            <input
              id="organization_name"
              value={form.organization_name}
              onChange={(e) => set("organization_name", e.target.value)}
              placeholder="Acme Corp"
            />
            {errors.organization_name && <div className="err">{errors.organization_name}</div>}
          </div>

          <div className="grid-2">
            <div className="field">
              <label htmlFor="plan_code">Plan</label>
              <select
                id="plan_code"
                value={form.plan_code}
                onChange={(e) => set("plan_code", e.target.value)}
              >
                {PLANS.map((p) => (
                  <option key={p.code} value={p.code}>
                    {planLabel(p.code)} — {p.code}
                  </option>
                ))}
              </select>
            </div>
            <div className="field">
              <label htmlFor="addon_packs">
                Add-on packs ({ADDON.code}, {ADDON.assets} assets each)
              </label>
              <input
                id="addon_packs"
                type="number"
                min="0"
                max={ADDON.maxPacks}
                step="1"
                value={plan?.addonsAllowed ? form.addon_packs : 0}
                disabled={!plan?.addonsAllowed}
                onChange={(e) => set("addon_packs", e.target.value)}
              />
              {errors.addon_packs && <div className="err">{errors.addon_packs}</div>}
              {!plan?.addonsAllowed && (
                <div className="note">{plan?.pilot ? "The pilot" : "Unlimited"} takes no add-ons.</div>
              )}
            </div>
          </div>

          <label className="checkline">
            <input
              type="checkbox"
              checked={form.offline_allowed}
              onChange={(e) => set("offline_allowed", e.target.checked)}
            />
            <span>
              <strong>Offline license</strong> — the customer's NGCorion has no internet access. It runs on
              a signed license file (valid one year) issued from this console, instead of activation and
              heartbeats.
            </span>
          </label>

          <PlanPreview plan={plan} packs={plan?.addonsAllowed ? Number(form.addon_packs) || 0 : 0} />

          <div style={{ marginTop: 20 }}>
            <button className="primary" type="submit" disabled={busy}>
              {busy ? "Creating…" : "Create license"}
            </button>
          </div>
        </form>
      </div>
    </>
  );
}

function PlanPreview({ plan, packs }) {
  if (!plan) return null;
  const total = maxAssets(plan, packs);
  return (
    <div className="card" style={{ background: "var(--bg)", padding: 16 }}>
      <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 12, gap: 12, flexWrap: "wrap" }}>
        <strong>{planLabel(plan.code)}</strong>
        <span className="note">Valid for {plan.durationDays} days</span>
      </div>
      <div className="usage" style={{ gridTemplateColumns: "repeat(auto-fit,minmax(120px,1fr))", display: "grid" }}>
        <div>
          <div className="note">Plan assets</div>
          <div style={{ fontWeight: 600, fontSize: 16 }}>{fmtAssets(plan.assets)}</div>
        </div>
        <div>
          <div className="note">Add-ons</div>
          <div style={{ fontWeight: 600, fontSize: 16 }}>
            {packs ? `${packs} × ${ADDON.assets} = ${fmtAssets(packs * ADDON.assets)}` : "—"}
          </div>
        </div>
        <div>
          <div className="note">Asset limit</div>
          <div style={{ fontWeight: 600, fontSize: 16 }}>{fmtAssets(total)}</div>
        </div>
      </div>
      <p className="note" style={{ margin: "12px 0 0" }}>
        The limit is stored with the license and signed into every answer NGCorion receives. Audits
        and hardening runs are not counted.
      </p>
    </div>
  );
}
