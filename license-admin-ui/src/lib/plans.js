// Display-only mirror of the backend plan catalog.
//
// SOURCE OF TRUTH is the license server: license_server/app/crud.py
// (get_plan_limits) and license_server/app/models.py (PlanType enum). This file
// is a hand-copied mirror so the Generate form can preview limits/duration
// without a round trip. It is NOT shared code — if the backend plans change,
// update this list to match. Limits of `null` mean unlimited.
//
// Asset Management has no license entitlement — only audits and hardens are
// quota dimensions.

export const PLANS = [
  {
    value: "pilot",
    label: "Pilot",
    blurb: "30-day trial",
    durationDays: 30,
    limits: { audits: 2, hardens: 2 },
  },
  {
    value: "plan_100",
    label: "100 Audit / 100 Hardening",
    blurb: "Up to 100 audits and 100 hardenings",
    durationDays: 365,
    limits: { audits: 100, hardens: 100 },
  },
  {
    value: "plan_250",
    label: "250 Audit / 250 Hardening",
    blurb: "Up to 250 audits and 250 hardenings",
    durationDays: 365,
    limits: { audits: 250, hardens: 250 },
  },
  {
    value: "plan_500",
    label: "500 Audit / 500 Hardening",
    blurb: "Up to 500 audits and 500 hardenings",
    durationDays: 365,
    limits: { audits: 500, hardens: 500 },
  },
  {
    value: "unlimited",
    label: "Unlimited",
    blurb: "Unlimited audits and hardenings",
    durationDays: 365,
    limits: { audits: null, hardens: null },
  },
];

export const PLAN_BY_VALUE = Object.fromEntries(PLANS.map((p) => [p.value, p]));

export function planLabel(value) {
  return PLAN_BY_VALUE[value]?.label || value;
}

// The two quota dimensions, in display order. Field names match the API
// LicenseResponse (max_<x> / used_<x>).
export const OPERATIONS = ["audits", "hardens"];
