import { useState } from "react";
import { createLicense } from "../api/client.js";
import { PLANS, PLAN_BY_VALUE, OPERATIONS } from "../lib/plans.js";
import CredentialCard from "./CredentialCard.jsx";

const EMPTY = {
  customer_name: "",
  customer_email: "",
  organization_name: "",
  plan_type: "pilot",
};

const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;

export default function GenerateLicense() {
  const [form, setForm] = useState(EMPTY);
  const [errors, setErrors] = useState({});
  const [serverError, setServerError] = useState("");
  const [busy, setBusy] = useState(false);
  const [created, setCreated] = useState(null);

  const plan = PLAN_BY_VALUE[form.plan_type];

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
        plan_type: form.plan_type,
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

          <div className="field">
            <label htmlFor="plan_type">Plan</label>
            <select
              id="plan_type"
              value={form.plan_type}
              onChange={(e) => set("plan_type", e.target.value)}
            >
              {PLANS.map((p) => (
                <option key={p.value} value={p.value}>
                  {p.label} — {p.blurb}
                </option>
              ))}
            </select>
          </div>

          <PlanPreview plan={plan} />

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

function PlanPreview({ plan }) {
  if (!plan) return null;
  return (
    <div className="card" style={{ background: "var(--bg)", padding: 16 }}>
      <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 12 }}>
        <strong>{plan.label}</strong>
        <span className="note">Valid for {plan.durationDays} days</span>
      </div>
      <div className="usage" style={{ gridTemplateColumns: "repeat(auto-fit,minmax(120px,1fr))", display: "grid" }}>
        {OPERATIONS.map((op) => (
          <div key={op}>
            <div className="note" style={{ textTransform: "capitalize" }}>{op}</div>
            <div style={{ fontWeight: 600, fontSize: 16 }}>
              {plan.limits[op] === null ? "Unlimited" : plan.limits[op]}
            </div>
          </div>
        ))}
      </div>
      <p className="note" style={{ margin: "12px 0 0" }}>
        Limits mirror the server plan catalog and are applied automatically on creation.
      </p>
    </div>
  );
}
