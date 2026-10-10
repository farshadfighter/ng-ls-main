# License Plans (licensing v3)

NGCorion is licensed **per number of managed assets**. A license names a plan
(its SKU), the plan sets the base asset limit and the term, and **add-on packs**
of 50 assets raise the limit. Audits and hardening runs are **not** counted.

The catalog lives in code: `app/plans.py` (served to the admin UI at
`GET /api/admin/plans`). The limit is stored with each license
(`licenses.max_assets`) when it is created or changed, so a later catalog change
never alters a license already sold.

## Plans

| SKU | Plan | Assets | Add-ons | Term |
|---|---|---:|---|---|
| `NGC-PILOT-5-1M` | NGCorion Pilot (trial) | 5 | no | 30 days |
| `NGC-ESS-25-1Y` | NGCorion Essential | 25 | yes | 1 year |
| `NGC-PRO-100-1Y` | NGCorion Professional | 100 | yes | 1 year |
| `NGC-BUS-250-1Y` | NGCorion Business | 250 | yes | 1 year |
| `NGC-ENT-500-1Y` | NGCorion Enterprise | 500 | yes | 1 year |
| `NGC-ENT-1000-1Y` | NGCorion Enterprise | 1,000 | yes | 1 year |
| `NGC-ENT-2500-1Y` | NGCorion Enterprise | 2,500 | yes | 1 year |
| `NGC-ENT-ULT-1Y` | NGCorion Enterprise Unlimited | unlimited | no | 1 year |

**Add-on:** `NGC-ADD-50-1Y`, 50 assets per pack, up to 200 packs.

    asset limit = plan assets + 50 × add-on packs

Example: an organisation with 600 assets buys `NGC-ENT-500-1Y` with 2 add-ons
(500 + 2 × 50 = 600) instead of the 1,000 plan.

## What NGCorion does with it

* Every signed answer (activate, validate, heartbeat) carries
  `plan`, `plan_name`, `pilot`, `limits = {"max_assets": N | null, "addon_packs": k}`
  and the expiry.
* NGCorion counts **all assets in its inventory** against `max_assets`. Adding
  an asset beyond the limit (by hand, by import or from discovery) is refused.
  If the inventory is already larger than the limit (for example after falling
  back to the pilot), audits and hardening run only on the first N assets
  (the oldest ones); the others stay in the inventory.
* Each hourly heartbeat reports the current asset count; the admin UI shows it
  next to the limit (`reported_assets`), so a customer outgrowing the plan is
  visible.

## Offline: the pilot fallback

If NGCorion receives **no signed answer for 48 hours**, it falls back on its own
to the pilot entitlement: **5 assets**, for at most **30 days** from the
fallback; after that it is locked until it reaches the server. As soon as it
gets a signed answer again it returns to the license's own plan.

The license server never changes a license because it was offline. (Up to
licensing v2, validate and heartbeat overwrote the plan with the pilot plan for
good. Migration 006 logs the licenses that look like they were downgraded that
way: a pilot plan with a one-year term. Give each its real plan back in the
admin UI.)

## Changing a plan

Admin UI → Licenses → a license → **Plan & add-ons**, or

    POST /api/admin/licenses/{license_key}/plan
    {"plan_code": "NGC-ENT-1000-1Y", "addon_packs": 3}

Either field may be left out. The term does not change. Moving to a plan
without add-ons (Unlimited, Pilot) drops the packs. NGCorion picks up the new
limit with its next validation or heartbeat (within the hour).

## The portal

`POST /internal/portal/licenses` takes `plan_code` (a SKU) and `addon_packs`.
A portal still on the old vocabulary may send `plan_type` instead; the old
names mean the plan with the same number:

| old `plan_type` | now |
|---|---|
| `pilot` | `NGC-PILOT-5-1M` |
| `plan_100` | `NGC-PRO-100-1Y` |
| `plan_250` | `NGC-BUS-250-1Y` |
| `plan_500` | `NGC-ENT-500-1Y` |
| `unlimited` | `NGC-ENT-ULT-1Y` |

License events sent to the portal carry `plan_code`, `max_assets` and
`addon_packs` in place of the old `plan_type`, and
`GET /internal/portal/licenses/{id}` reports `plan_code`, `plan_name`,
`max_assets`, `addon_packs` and `reported_assets` in place of the audit and
hardening quotas.

## Removed in v3

* `POST /api/licenses/consume` and the audit/hardening quotas
  (`max_audits`, `max_hardens`, `used_audits`, `used_hardens`).
* `plan_type` (and its Postgres enum type `plantype`).
* The server-side downgrade to pilot after 48 hours offline, and the
  `should_downgrade` / `down` fields of the heartbeat answer.
