// Display-only mirror of the backend plan catalog.
//
// SOURCE OF TRUTH is the license server: license_server/app/plans.py (also
// served at GET /api/admin/plans). This file is a hand-copied mirror so the
// Generate form and the Guide can show the plans without a round trip. If the
// catalog changes, update this list to match.
//
// A license is sold per number of managed assets. 50-asset add-on packs raise
// a finite paid plan's limit: limit = base + 50 x packs. `assets: null` means
// unlimited. Audits and hardening runs are not counted.

export const ADDON = { code: "NGC-ADD-50-1Y", assets: 50, maxPacks: 200 };

export const PLANS = [
  { code: "NGC-PILOT-5-1M", name: "NGCorion Pilot", assets: 5, durationDays: 30, pilot: true },
  { code: "NGC-ESS-25-1Y", name: "NGCorion Essential", assets: 25, durationDays: 365 },
  { code: "NGC-PRO-100-1Y", name: "NGCorion Professional", assets: 100, durationDays: 365 },
  { code: "NGC-BUS-250-1Y", name: "NGCorion Business", assets: 250, durationDays: 365 },
  { code: "NGC-ENT-500-1Y", name: "NGCorion Enterprise", assets: 500, durationDays: 365 },
  { code: "NGC-ENT-1000-1Y", name: "NGCorion Enterprise", assets: 1000, durationDays: 365 },
  { code: "NGC-ENT-2500-1Y", name: "NGCorion Enterprise", assets: 2500, durationDays: 365 },
  { code: "NGC-ENT-ULT-1Y", name: "NGCorion Enterprise Unlimited", assets: null, durationDays: 365 },
].map((p) => ({ ...p, addonsAllowed: p.assets !== null && !p.pilot }));

export const PLAN_BY_CODE = Object.fromEntries(PLANS.map((p) => [p.code, p]));

// The asset limit a plan with `packs` add-ons gives; null = unlimited.
export function maxAssets(plan, packs = 0) {
  if (!plan || plan.assets === null) return null;
  return plan.assets + ADDON.assets * (Number(packs) || 0);
}

export function fmtAssets(n) {
  return n === null || n === undefined ? "Unlimited" : Number(n).toLocaleString("en-US");
}

// "NGCorion Business (250)" — the name alone is ambiguous for the three
// Enterprise sizes.
export function planLabel(code) {
  const plan = PLAN_BY_CODE[code];
  if (!plan) return code || "—";
  return plan.assets === null || plan.pilot ? plan.name : `${plan.name} ${fmtAssets(plan.assets)}`;
}

// A license row's plan as a short badge text, add-ons included.
export function licensePlanLabel(license) {
  const base = planLabel(license.plan_code);
  return license.addon_packs ? `${base} + ${license.addon_packs} add-on${license.addon_packs > 1 ? "s" : ""}` : base;
}
